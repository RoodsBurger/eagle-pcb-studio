#!/usr/bin/env python3
"""Analyze a Fusion Electronics / EAGLE 9.x Gerber + Excellon drill export directory.

Scans recursively for RS-274X gerbers (*.gbr) and Excellon drills (*.xln/*.drl),
identifies layers, and runs design-export sanity checks:

  - completeness         (copper >=2, both masks, outline, >=1 drill)
  - solder-mask dam widths (smallest mask sliver between adjacent openings)
  - drill report          (tools, hit counts, smallest drill, routed-slot mode)
  - plated-slot heuristic (round drill diameter coinciding with slot geometry)
  - alignment / extent    (shared origin across copper + outline; true board size)

Stdlib only. Run:  python3 analyze_gerbers.py "<export dir>" [--text] [-o out.json]
"""

import argparse
import json
import math
import os
import re
import sys


# --------------------------------------------------------------------------- #
# Severity / findings
# --------------------------------------------------------------------------- #

SEV_ERROR = "ERROR"
SEV_WARNING = "WARNING"
SEV_INFO = "INFO"

_SEV_ORDER = {SEV_ERROR: 0, SEV_WARNING: 1, SEV_INFO: 2}

# Default minimum acceptable solder-mask dam (sliver) width in millimetres.
DEFAULT_MASK_DAM_MM = 0.22
# Two openings are considered "neighbours" worth measuring when their centres
# are within this distance (mm); keeps the pairwise scan cheap and relevant.
NEIGHBOUR_RADIUS_MM = 1.2
# Drill diameters below this (mm) are flagged as risky / specialty.
MIN_DRILL_MM = 0.2


def finding(severity, fid, message, recommendation=""):
    return {
        "severity": severity,
        "id": fid,
        "message": message,
        "recommendation": recommendation,
    }


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

GERBER_EXTS = (".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo",
               ".gm1", ".gko", ".gbp", ".gtp", ".g2", ".g3", ".gp1")
DRILL_EXTS = (".xln", ".drl", ".txt")  # .txt only accepted if Excellon-shaped


def scan_dir(root):
    """Walk root recursively, return (gerbers, drills) absolute path lists."""
    gerbers, drills = [], []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            low = name.lower()
            if low.endswith(".gbr"):
                gerbers.append(path)
            elif low.endswith((".xln", ".drl")):
                drills.append(path)
            elif low.endswith(GERBER_EXTS) and not low.endswith(".gbrjob"):
                gerbers.append(path)
            elif low.endswith(".txt"):
                # Assembly P&P files also end .txt; sniff for Excellon header.
                if _looks_like_excellon(path):
                    drills.append(path)
    return sorted(set(gerbers)), sorted(set(drills))


def _looks_like_excellon(path):
    try:
        with open(path, "r", errors="replace") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return ("M48" in head) or re.search(r"^T\d+C[\d.]", head, re.M) is not None


# --------------------------------------------------------------------------- #
# Layer identification
# --------------------------------------------------------------------------- #

def identify_layer(path, text):
    """Return a canonical layer role for a gerber file.

    Order: (1) X2 %TF.FileFunction; (2) Fusion copper_*_lN / role filenames;
    (3) KiCad-style names + classic extensions as fallback.
    """
    name = os.path.basename(path)
    low = name.lower()

    # (1) X2 file-function attribute -------------------------------------- #
    m = re.search(r"%TF\.FileFunction,([^*%]+)", text)
    if m:
        ff = m.group(1)
        parts = ff.split(",")
        kind = parts[0].strip().lower()
        if kind == "copper":
            # Copper,L1,Top  /  Copper,L2,Inr ...
            side = parts[2].strip().lower() if len(parts) > 2 else ""
            if side.startswith("top"):
                return "F.Cu", "copper", "X2"
            if side.startswith("bot"):
                return "B.Cu", "copper", "X2"
            return "Inner", "copper", "X2"
        if kind == "soldermask":
            side = parts[1].strip().lower() if len(parts) > 1 else ""
            return ("F.Mask" if side.startswith("top") else "B.Mask",
                    "soldermask", "X2")
        if kind == "legend":
            side = parts[1].strip().lower() if len(parts) > 1 else ""
            return ("F.SilkS" if side.startswith("top") else "B.SilkS",
                    "silkscreen", "X2")
        if kind == "paste":
            side = parts[1].strip().lower() if len(parts) > 1 else ""
            return ("F.Paste" if side.startswith("top") else "B.Paste",
                    "solderpaste", "X2")
        if kind in ("profile", "outline"):
            return "Edge.Cuts", "outline", "X2"

    # (2) Fusion filename conventions ------------------------------------- #
    m = re.match(r"copper_top_l(\d+)", low)
    if m:
        return "F.Cu", "copper", "fusion-name"
    m = re.match(r"copper_bottom_l(\d+)", low)
    if m:
        return "B.Cu", "copper", "fusion-name"
    m = re.match(r"copper_inner_l(\d+)", low)
    if m:
        return "Inner.L%s" % m.group(1), "copper", "fusion-name"
    if low.startswith("soldermask_top"):
        return "F.Mask", "soldermask", "fusion-name"
    if low.startswith("soldermask_bottom"):
        return "B.Mask", "soldermask", "fusion-name"
    if low.startswith("silkscreen_top"):
        return "F.SilkS", "silkscreen", "fusion-name"
    if low.startswith("silkscreen_bottom"):
        return "B.SilkS", "silkscreen", "fusion-name"
    if low.startswith("solderpaste_top"):
        return "F.Paste", "solderpaste", "fusion-name"
    if low.startswith("solderpaste_bottom"):
        return "B.Paste", "solderpaste", "fusion-name"
    if low.startswith("profile") or "outline" in low or "boardoutline" in low:
        return "Edge.Cuts", "outline", "fusion-name"

    # (3) KiCad names + classic extensions -------------------------------- #
    kicad = {
        "f_cu": ("F.Cu", "copper"), "b_cu": ("B.Cu", "copper"),
        "f_mask": ("F.Mask", "soldermask"), "b_mask": ("B.Mask", "soldermask"),
        "f_silks": ("F.SilkS", "silkscreen"), "b_silks": ("B.SilkS", "silkscreen"),
        "f_paste": ("F.Paste", "solderpaste"), "b_paste": ("B.Paste", "solderpaste"),
        "edge_cuts": ("Edge.Cuts", "outline"),
    }
    for key, (lyr, role) in kicad.items():
        if key in low:
            return lyr, role, "kicad-name"
    m = re.search(r"in(\d+)_cu", low)
    if m:
        return "Inner.L%s" % m.group(1), "copper", "kicad-name"

    ext_map = {
        ".gtl": ("F.Cu", "copper"), ".gbl": ("B.Cu", "copper"),
        ".gts": ("F.Mask", "soldermask"), ".gbs": ("B.Mask", "soldermask"),
        ".gto": ("F.SilkS", "silkscreen"), ".gbo": ("B.SilkS", "silkscreen"),
        ".gtp": ("F.Paste", "solderpaste"), ".gbp": ("B.Paste", "solderpaste"),
        ".gm1": ("Edge.Cuts", "outline"), ".gko": ("Edge.Cuts", "outline"),
    }
    for ext, (lyr, role) in ext_map.items():
        if low.endswith(ext):
            return lyr, role, "extension"

    return "Unknown(%s)" % name, "unknown", "unmatched"


# --------------------------------------------------------------------------- #
# RS-274X gerber parsing
# --------------------------------------------------------------------------- #

_FS_RE = re.compile(r"%FSLA?X(\d)(\d)Y(\d)(\d)\*?%")
_AD_RE = re.compile(r"%ADD(\d+)([A-Za-z_][\w]*)(?:,([^*%]*))?\*?%")
_DSEL_RE = re.compile(r"^D(\d+)\*\s*$")
_OP_RE = re.compile(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:D0?([0-9]))?\*")


def _parse_format(text):
    """Return (scale, unit_mm) from %FS / %MO. Default 10^4, mm."""
    m = _FS_RE.search(text)
    if m:
        # Decimal digit count of X coordinate sets the scale.
        dec = int(m.group(2))
    else:
        dec = 4
    scale = 10 ** dec
    unit_mm = "%MOIN" not in text  # default metric unless inch declared
    return scale, unit_mm


def _parse_apertures(text):
    """Map D-code -> dict(type, params[mm-ish raw]). Macros stored as type='macro'."""
    aps = {}
    for m in _AD_RE.finditer(text):
        dcode = int(m.group(1))
        atype = m.group(2)
        raw = m.group(3) or ""
        params = []
        if raw:
            for tok in raw.split("X"):
                tok = tok.strip()
                try:
                    params.append(float(tok))
                except ValueError:
                    pass
        aps[dcode] = {"type": atype, "params": params}
    return aps


def parse_gerber(path):
    """Parse one RS-274X file. Returns dict with bbox, openings, scale, unit."""
    with open(path, "r", errors="replace") as fh:
        text = fh.read()

    scale, unit_mm = _parse_format(text)
    apertures = _parse_apertures(text)

    def to_mm(v):
        return v / scale * (1.0 if unit_mm else 25.4)

    minx = miny = math.inf
    maxx = maxy = -math.inf
    openings = []          # flashed pads: dict(x,y,w,h,shape)
    regions = []           # filled regions -> bbox dict(x,y,w,h,shape='region')
    coord_count = 0

    cur_d = None
    cur_x = cur_y = 0
    in_region = False
    region_pts = []

    # Strip parameter blocks (%...%) so they don't confuse the line scan; the
    # aperture/format data is already captured above.
    body = re.sub(r"%[^%]*%", "", text)

    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("G36"):
            in_region = True
            region_pts = []
            continue
        if line.startswith("G37"):
            if region_pts:
                xs = [p[0] for p in region_pts]
                ys = [p[1] for p in region_pts]
                regions.append({
                    "x": (min(xs) + max(xs)) / 2.0,
                    "y": (min(ys) + max(ys)) / 2.0,
                    "w": max(xs) - min(xs),
                    "h": max(ys) - min(ys),
                    "shape": "region",
                })
            in_region = False
            region_pts = []
            continue

        dsel = _DSEL_RE.match(line)
        if dsel:
            cur_d = int(dsel.group(1))
            continue

        # Aperture-select can be inline with no trailing data; skip Gnn-only.
        op = _OP_RE.search(line)
        if not op or (op.group(1) is None and op.group(2) is None
                      and op.group(3) is None):
            inline = re.match(r"^D(\d+)\*?", line)
            if inline:
                cur_d = int(inline.group(1))
            continue

        if op.group(1) is not None:
            cur_x = int(op.group(1))
        if op.group(2) is not None:
            cur_y = int(op.group(2))
        opcode = op.group(3)

        mx, my = to_mm(cur_x), to_mm(cur_y)

        if opcode in ("1", "2", "3"):
            coord_count += 1
            minx, miny = min(minx, mx), min(miny, my)
            maxx, maxy = max(maxx, mx), max(maxy, my)

        if in_region:
            region_pts.append((mx, my))
            continue

        if opcode == "3":  # flash
            ap = apertures.get(cur_d)
            w = h = 0.0
            shape = "flash"
            if ap:
                t = ap["type"].upper()
                p = ap["params"]
                if t == "C" and p:
                    w = h = p[0]
                    shape = "circle"
                elif t in ("R", "O") and len(p) >= 2:
                    w, h = p[0], p[1]
                    shape = "rect" if t == "R" else "obround"
                elif p:
                    w = h = p[0]
                    shape = t.lower()
            openings.append({"x": mx, "y": my, "w": w, "h": h, "shape": shape})

    if coord_count == 0:
        minx = miny = maxx = maxy = 0.0

    return {
        "path": path,
        "scale": scale,
        "unit_mm": unit_mm,
        "bbox": {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
        "coord_count": coord_count,
        "openings": openings,
        "regions": regions,
    }


# --------------------------------------------------------------------------- #
# Solder-mask dam analysis
# --------------------------------------------------------------------------- #

def _opening_extent(o):
    """Return (minx, miny, maxx, maxy) bbox of one opening (flash or region)."""
    if o["shape"] in ("circle",):
        r = o["w"] / 2.0
        return o["x"] - r, o["y"] - r, o["x"] + r, o["y"] + r
    hw, hh = o["w"] / 2.0, o["h"] / 2.0
    return o["x"] - hw, o["y"] - hh, o["x"] + hw, o["y"] + hh


def _edge_gap(a, b):
    """Edge-to-edge gap (mm) between two axis-aligned opening bboxes.

    Negative -> overlap. Uses bbox separation on each axis (Chebyshev-style),
    which is the dam/sliver width between rectangular mask openings.
    """
    ax0, ay0, ax1, ay1 = _opening_extent(a)
    bx0, by0, bx1, by1 = _opening_extent(b)
    dx = max(bx0 - ax1, ax0 - bx1)   # >0 if separated on X
    dy = max(by0 - ay1, ay0 - by1)   # >0 if separated on Y
    if dx > 0 and dy > 0:
        return math.hypot(dx, dy)    # diagonal corner separation
    if dx > 0:
        return dx
    if dy > 0:
        return dy
    return max(dx, dy)               # overlapping -> negative


def analyze_mask_dams(mask_parsed, threshold_mm, neighbour_mm):
    """Find smallest mask dams (slivers) between nearby openings.

    Returns (sorted_dam_list, total_openings). Each dam:
    dict(gap_mm, x, y, shapes).
    """
    openings = list(mask_parsed["openings"]) + list(mask_parsed["regions"])
    n = len(openings)
    dams = []

    # Spatial bucket grid keyed by neighbour radius to avoid O(n^2) blowup.
    cell = max(neighbour_mm, 0.5)
    grid = {}
    for i, o in enumerate(openings):
        gx, gy = int(o["x"] // cell), int(o["y"] // cell)
        grid.setdefault((gx, gy), []).append(i)

    seen_pairs = set()
    for i, o in enumerate(openings):
        gx, gy = int(o["x"] // cell), int(o["y"] // cell)
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for j in grid.get((gx + ddx, gy + ddy), ()):
                    if j <= i:
                        continue
                    pair = (i, j)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    b = openings[j]
                    centre_dist = math.hypot(o["x"] - b["x"], o["y"] - b["y"])
                    if centre_dist > neighbour_mm + 2.0:
                        continue
                    gap = _edge_gap(o, b)
                    if gap < 0:
                        continue  # overlapping openings merge; not a dam
                    if gap <= max(threshold_mm * 4, neighbour_mm):
                        dams.append({
                            "gap_mm": round(gap, 4),
                            "x": round((o["x"] + b["x"]) / 2.0, 3),
                            "y": round((o["y"] + b["y"]) / 2.0, 3),
                            "shapes": "%s/%s" % (o["shape"], b["shape"]),
                        })

    dams.sort(key=lambda d: d["gap_mm"])
    return dams, n


# --------------------------------------------------------------------------- #
# Excellon drill parsing
# --------------------------------------------------------------------------- #

def parse_drill(path):
    """Parse Excellon. Returns dict(tools, hits, routed, smallest, fmt)."""
    with open(path, "r", errors="replace") as fh:
        text = fh.read()

    unit_mm = True
    if re.search(r"\bINCH\b", text):
        unit_mm = False
    elif re.search(r"\bMETRIC\b", text):
        unit_mm = True

    # Decimal handling. METRIC,TZ,000.000 -> 3 integer / 3 decimal, trailing zeros.
    dec_digits = 4 if unit_mm else 4
    fmt_m = re.search(r"(?:METRIC|INCH)\s*,\s*([LT]Z)?\s*,?\s*(\d+)\.(\d+)", text)
    leading_zero = True  # LZ = leading kept; TZ = trailing kept -> implied lead
    if fmt_m:
        if fmt_m.group(1) == "TZ":
            leading_zero = False
        if fmt_m.group(2) is not None and fmt_m.group(3) is not None:
            dec_digits = int(fmt_m.group(3))

    tools = {}            # tool# -> diameter mm
    for m in re.finditer(r"^T(\d+)C([\d.]+)", text, re.M):
        dia = float(m.group(2))
        if not unit_mm:
            dia *= 25.4
        tools[int(m.group(1))] = dia

    routed = bool(re.search(r"\bG85\b", text) or re.search(r"\bM15\b", text)
                  or re.search(r"\bG00\b", text) or re.search(r"\bG01\b", text))

    hits = {t: 0 for t in tools}
    cur_tool = None
    for line in text.splitlines():
        line = line.strip()
        tm = re.match(r"^T(\d+)\s*$", line)
        if tm:
            t = int(tm.group(1))
            if t in tools:
                cur_tool = t
            continue
        if cur_tool is not None and re.match(r"^[XY]-?\d", line):
            hits[cur_tool] = hits.get(cur_tool, 0) + 1

    smallest = min(tools.values()) if tools else None
    return {
        "path": path,
        "unit_mm": unit_mm,
        "tools": tools,
        "hits": hits,
        "routed": routed,
        "smallest": smallest,
        "total_hits": sum(hits.values()),
    }


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #

def analyze(root, threshold_mm, neighbour_mm):
    findings = []
    gerbers, drills = scan_dir(root)

    layers = []           # list of dict(path, layer, role, source, parsed)
    by_role = {}

    for path in gerbers:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        layer, role, source = identify_layer(path, text)
        parsed = parse_gerber(path)
        entry = {
            "path": path,
            "name": os.path.basename(path),
            "layer": layer,
            "role": role,
            "source": source,
            "bbox": parsed["bbox"],
            "coord_count": parsed["coord_count"],
            "_parsed": parsed,
        }
        layers.append(entry)
        by_role.setdefault(role, []).append(entry)

    drill_results = [parse_drill(d) for d in drills]

    # ---- completeness ---------------------------------------------------- #
    copper = by_role.get("copper", [])
    masks = by_role.get("soldermask", [])
    outline = by_role.get("outline", [])

    if len(copper) >= 2:
        findings.append(finding(
            SEV_INFO, "completeness.copper",
            "Found %d copper layer(s): %s." % (
                len(copper), ", ".join(sorted(c["layer"] for c in copper))),
            ""))
    else:
        findings.append(finding(
            SEV_ERROR, "completeness.copper",
            "Only %d copper layer(s) found; expected >= 2." % len(copper),
            "Re-export including top and bottom copper."))

    mask_sides = {m["layer"] for m in masks}
    if {"F.Mask", "B.Mask"} <= mask_sides:
        findings.append(finding(
            SEV_INFO, "completeness.mask",
            "Both solder masks present (F.Mask, B.Mask).", ""))
    else:
        missing = {"F.Mask", "B.Mask"} - mask_sides
        findings.append(finding(
            SEV_WARNING, "completeness.mask",
            "Missing solder mask layer(s): %s." % ", ".join(sorted(missing)),
            "Export both top and bottom soldermask gerbers."))

    if outline:
        findings.append(finding(
            SEV_INFO, "completeness.outline",
            "Board outline/profile present (%s)." % outline[0]["name"], ""))
    else:
        findings.append(finding(
            SEV_ERROR, "completeness.outline",
            "No board outline/profile gerber found.",
            "Export the profile/Edge.Cuts layer; fab needs it for routing."))

    if drill_results:
        findings.append(finding(
            SEV_INFO, "completeness.drill",
            "Found %d drill file(s): %s." % (
                len(drill_results),
                ", ".join(os.path.basename(d["path"]) for d in drill_results)),
            ""))
    else:
        findings.append(finding(
            SEV_ERROR, "completeness.drill",
            "No drill (.xln/.drl) file found.",
            "Export the Excellon drill file."))

    for e in layers:
        if e["role"] == "unknown":
            findings.append(finding(
                SEV_WARNING, "layer.unidentified",
                "Could not classify gerber '%s'." % e["name"],
                "Check the export naming or add an X2 FileFunction attribute."))

    # ---- solder-mask dam widths ----------------------------------------- #
    mask_dam_report = {}
    for m in masks:
        dams, n_open = analyze_mask_dams(m["_parsed"], threshold_mm, neighbour_mm)
        thin = [d for d in dams if d["gap_mm"] < threshold_mm]
        mask_dam_report[m["layer"]] = {
            "openings": n_open,
            "smallest_dams": dams[:10],
            "thin_count": len(thin),
        }
        if not n_open:
            continue
        if thin:
            worst = dams[0]
            findings.append(finding(
                SEV_WARNING, "mask.dam.%s" % m["layer"],
                "%s: %d mask dam(s) below %.3f mm; smallest %.3f mm at "
                "(%.2f, %.2f) mm between %s openings." % (
                    m["layer"], len(thin), threshold_mm, worst["gap_mm"],
                    worst["x"], worst["y"], worst["shapes"]),
                "PCBWay green/blue/red/purple need >= 0.19 mm; dams under "
                "~0.10 mm gang together and expose copper. Widen mask "
                "expansion or merge openings, or confirm fab can hold it."))
        else:
            smin = dams[0]["gap_mm"] if dams else float("nan")
            msg = ("%s: %d openings, smallest dam %.3f mm (>= %.3f mm)."
                   % (m["layer"], n_open, smin, threshold_mm)) if dams else \
                  "%s: %d openings, no adjacent pairs within %.2f mm." % (
                      m["layer"], n_open, neighbour_mm)
            findings.append(finding(SEV_INFO, "mask.dam.%s" % m["layer"], msg, ""))

    # ---- drill report ---------------------------------------------------- #
    drill_report = []
    for d in drill_results:
        toolinfo = sorted(
            ({"tool": t, "dia_mm": round(dia, 4), "hits": d["hits"].get(t, 0)}
             for t, dia in d["tools"].items()),
            key=lambda x: x["dia_mm"])
        drill_report.append({
            "file": os.path.basename(d["path"]),
            "tools": toolinfo,
            "smallest_mm": round(d["smallest"], 4) if d["smallest"] else None,
            "total_hits": d["total_hits"],
            "routed": d["routed"],
        })
        if d["smallest"] is not None:
            mode = "routed slots (G85/M15)" if d["routed"] else "round-hole only"
            findings.append(finding(
                SEV_INFO, "drill.summary.%s" % os.path.basename(d["path"]),
                "%s: %d tools, %d hits, smallest %.3f mm, mode = %s." % (
                    os.path.basename(d["path"]), len(d["tools"]),
                    d["total_hits"], d["smallest"], mode), ""))
            if d["smallest"] < MIN_DRILL_MM:
                findings.append(finding(
                    SEV_WARNING, "drill.tiny.%s" % os.path.basename(d["path"]),
                    "Smallest drill %.3f mm is below %.2f mm." % (
                        d["smallest"], MIN_DRILL_MM),
                    "Sub-0.2 mm holes need laser/specialty drilling; confirm "
                    "fab capability and cost, or enlarge the via/pad."))

    # ---- plated-slot / overlap heuristic -------------------------------- #
    any_routed = any(d["routed"] for d in drill_results)
    if drill_results and not any_routed:
        findings.append(finding(
            SEV_INFO, "drill.slots",
            "No routed-slot mode (G85/M15) detected; all drills are round. "
            "Plated slots, if any, would live in board milling geometry which "
            "is not in this drill file.",
            "If the design has plated slots/cutouts, verify them with "
            "check_consistency against the .brd or the milling layer."))

    # ---- alignment / extent --------------------------------------------- #
    # Use outline as the authority for board size; compare copper origins to it.
    extent_report = {}
    board_size = None
    ref = None
    if outline:
        ref = outline[0]["bbox"]
        bw = ref["maxx"] - ref["minx"]
        bh = ref["maxy"] - ref["miny"]
        board_size = {"w_mm": round(bw, 3), "h_mm": round(bh, 3),
                      "origin": [round(ref["minx"], 3), round(ref["miny"], 3)]}
        findings.append(finding(
            SEV_INFO, "extent.board",
            "Board size from outline: %.2f x %.2f mm (origin %.2f, %.2f)." % (
                bw, bh, ref["minx"], ref["miny"]),
            ""))
    elif copper:
        ref = copper[0]["bbox"]

    # Record every layer's bbox for the JSON report.
    for e in copper + outline:
        if e["coord_count"] == 0:
            continue
        bb = e["bbox"]
        extent_report[e["layer"]] = {
            "minx": round(bb["minx"], 3), "miny": round(bb["miny"], 3),
            "maxx": round(bb["maxx"], 3), "maxy": round(bb["maxy"], 3),
        }

    # Misregistration shows up as copper layers disagreeing with EACH OTHER on
    # their datum, not as copper pulling in from the board edge. Planes cover
    # more area than signal layers and every copper layer sits inside the
    # outline by the copper-to-edge clearance, so comparing copper min-corners
    # to the outline would false-positive. Instead take the consensus (median)
    # copper min-corner and flag only a copper layer that diverges grossly.
    drawn_copper = [e for e in copper if e["coord_count"] > 0]
    if len(drawn_copper) >= 2:
        def _median(vals):
            vs = sorted(vals)
            n = len(vs)
            return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0

        cx = _median([e["bbox"]["minx"] for e in drawn_copper])
        cy = _median([e["bbox"]["miny"] for e in drawn_copper])
        misaligned = []
        for e in drawn_copper:
            dox = abs(e["bbox"]["minx"] - cx)
            doy = abs(e["bbox"]["miny"] - cy)
            # 2 mm tolerance: signal vs plane coverage shifts the min-corner a
            # little; only a gross shift means a real registration error.
            if dox > 2.0 or doy > 2.0:
                misaligned.append((e["layer"], dox, doy))
        if misaligned:
            for lyr, dox, doy in misaligned:
                findings.append(finding(
                    SEV_WARNING, "extent.align.%s" % lyr,
                    "%s copper origin diverges from the other copper layers by "
                    "(%.2f, %.2f) mm." % (lyr, dox, doy),
                    "Copper layers should share one datum; a large divergence "
                    "means misregistration. Re-export with a common origin."))
        else:
            findings.append(finding(
                SEV_INFO, "extent.align",
                "All %d copper layers share a common origin; differences from "
                "the outline are copper-to-edge coverage, not misregistration."
                % len(drawn_copper), ""))

    findings.sort(key=lambda f: (_SEV_ORDER.get(f["severity"], 9), f["id"]))

    return {
        "root": root,
        "summary": {
            "gerbers": len(gerbers),
            "drills": len(drills),
            "copper_layers": len(copper),
            "board_size_mm": board_size,
            "errors": sum(1 for f in findings if f["severity"] == SEV_ERROR),
            "warnings": sum(1 for f in findings if f["severity"] == SEV_WARNING),
        },
        "layers": [
            {k: v for k, v in e.items() if k != "_parsed"} for e in layers
        ],
        "mask_dams": mask_dam_report,
        "drill": drill_report,
        "extents": extent_report,
        "findings": findings,
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def render_text(result):
    out = []
    s = result["summary"]
    out.append("=" * 70)
    out.append("Gerber/Drill export analysis")
    out.append("  dir: %s" % result["root"])
    out.append("=" * 70)
    bs = s["board_size_mm"]
    out.append("Gerbers: %d   Drill files: %d   Copper layers: %d" % (
        s["gerbers"], s["drills"], s["copper_layers"]))
    if bs:
        out.append("Board size: %.2f x %.2f mm  (origin %.2f, %.2f)" % (
            bs["w_mm"], bs["h_mm"], bs["origin"][0], bs["origin"][1]))
    out.append("Findings: %d ERROR, %d WARNING" % (s["errors"], s["warnings"]))
    out.append("")

    out.append("-- Layers " + "-" * 60)
    for e in result["layers"]:
        out.append("  %-14s %-12s [%s]  %s" % (
            e["layer"], e["role"], e["source"], e["name"]))
    out.append("")

    if result["drill"]:
        out.append("-- Drill " + "-" * 61)
        for d in result["drill"]:
            out.append("  %s  (%s, %d hits, smallest %.3f mm)" % (
                d["file"], "routed" if d["routed"] else "round-only",
                d["total_hits"],
                d["smallest_mm"] if d["smallest_mm"] is not None else 0.0))
            for t in d["tools"]:
                out.append("      T%-3d  %6.3f mm  x%d" % (
                    t["tool"], t["dia_mm"], t["hits"]))
        out.append("")

    if result["mask_dams"]:
        out.append("-- Solder-mask dams " + "-" * 50)
        for lyr, rep in result["mask_dams"].items():
            out.append("  %s: %d openings, %d below threshold" % (
                lyr, rep["openings"], rep["thin_count"]))
            for d in rep["smallest_dams"][:5]:
                out.append("      %6.3f mm  at (%7.2f, %7.2f)  %s" % (
                    d["gap_mm"], d["x"], d["y"], d["shapes"]))
        out.append("")

    out.append("-- Findings " + "-" * 58)
    for f in result["findings"]:
        out.append("  [%-7s] %s: %s" % (f["severity"], f["id"], f["message"]))
        if f["recommendation"]:
            out.append("            -> %s" % f["recommendation"])
    out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Analyze a Fusion/EAGLE Gerber + drill export directory.")
    ap.add_argument("directory", help="export directory (scanned recursively)")
    ap.add_argument("--text", action="store_true",
                    help="print human-readable summary (default if no -o)")
    ap.add_argument("-o", "--output", metavar="JSON",
                    help="write full results as JSON to this path")
    ap.add_argument("--mask-threshold", type=float, default=DEFAULT_MASK_DAM_MM,
                    help="min acceptable mask dam width in mm (default %.2f)"
                    % DEFAULT_MASK_DAM_MM)
    ap.add_argument("--neighbour", type=float, default=NEIGHBOUR_RADIUS_MM,
                    help="centre distance (mm) for adjacent-opening pairs "
                    "(default %.1f)" % NEIGHBOUR_RADIUS_MM)
    args = ap.parse_args(argv)

    root = args.directory
    if not os.path.isdir(root):
        sys.stderr.write("error: not a directory: %s\n" % root)
        return 2

    result = analyze(root, args.mask_threshold, args.neighbour)

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(result, fh, indent=2)
        sys.stderr.write("wrote %s\n" % args.output)

    if args.text or not args.output:
        sys.stdout.write(render_text(result))

    return 1 if result["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
