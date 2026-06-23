#!/usr/bin/env python3
"""Board-level DFM/quality review of an EAGLE / Fusion Electronics .brd file.

Self-contained, stdlib-only. Parses EAGLE XML with ElementTree (read-only;
never rewrites the file, so the DOCTYPE is irrelevant here) and reports
findings with severity / id / message / recommendation.

Usage:
    python3 analyze_board.py <brd> [--text] [-o out.json]

Checks: board outline & area, component count, copper stackup, placement
gaps/overlap, connectors-on-edge, per-signal connectivity (airwires, %% routed,
inner planes treated as connecting their net), plane pour status, net-class
power-trace ampacity (IPC-2221), solder-mask dam vs fine pitch, and exposed-pad
windowpane / thermal-via arrays.
"""
import sys, os, json, math, argparse, itertools
import xml.etree.ElementTree as ET

# EAGLE copper-layer numbers we treat as routable: top, two inner, bottom.
COPPER_LAYERS = ("1", "2", "15", "16")
INNER_LAYERS = ("2", "15")
EDGE_LAYERS = ("20",)            # Dimension (board outline)
TSTOP_TOP = "29"                 # tStop (top solder-mask)
TCREAM_TOP = "31"                # tCream (top solder paste)
OUTLINE_TOL = 2.6               # mm slack for "on edge"
FINE_PITCH = 0.65               # mm: <= this is a fine-pitch package
DAM_MIN = 0.22                  # mm: minimum reliable mask dam

# Connector reference-designator prefixes that ought to sit on a board edge.
# Matched against the leading-letter prefix of the refdes, not as substrings.
EDGE_CONN_PREFIXES = ("J", "CON", "P", "X", "USB", "CN")

# IPC-2221 external-trace ampacity, 1oz copper, ~10C rise. (width_mm, amps).
IPC_TABLE = [
    (0.15, 0.6), (0.25, 0.9), (0.30, 1.0),
    (0.40, 1.3), (0.50, 1.5), (0.75, 2.0), (1.00, 2.5),
]
# Net-name fragments hinting at power (higher current) nets.
POWER_HINTS = ("VBUS", "VIN", "VM", "VMOT", "5V", "24V", "12V", "VBAT", "PWR", "VCC", "P3V3", "3V3", "VDD")


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def rot_angle(rot):
    """Return (degrees, mirrored) from an EAGLE rot string like 'R90' / 'MR90'."""
    if not rot:
        return 0, False
    mir = rot.startswith("M")
    s = rot[1:] if mir else rot
    s = s[1:] if s.startswith("R") else s
    try:
        return int(float(s)), mir
    except ValueError:
        return 0, mir


def place(px, py, ang, mir):
    """Place a package-local point into board coordinates."""
    if mir:
        px = -px
    a = math.radians(ang)
    return (px * math.cos(a) - py * math.sin(a),
            px * math.sin(a) + py * math.cos(a))


def rect_gap(r1, r2):
    """Edge-to-edge gap between two axis-aligned rects; <0 means overlap."""
    if r1[0] < r2[2] and r2[0] < r1[2] and r1[1] < r2[3] and r2[1] < r1[3]:
        return -1.0
    dx = max(r1[0] - r2[2], r2[0] - r1[2], 0.0)
    dy = max(r1[1] - r2[3], r2[1] - r1[3], 0.0)
    return math.hypot(dx, dy)


def seg_pt_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def near_rect(x1, y1, x2, y2, r, tol):
    """True if a wire segment touches/enters rect r (with tolerance)."""
    ax0, ay0, ax1, ay1 = r[0] - tol, r[1] - tol, r[2] + tol, r[3] + tol
    for (x, y) in ((x1, y1), (x2, y2)):
        if ax0 <= x <= ax1 and ay0 <= y <= ay1:
            return True
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return seg_pt_dist(cx, cy, x1, y1, x2, y2) <= max(r[2] - r[0], r[3] - r[1]) / 2 + tol


def pt_in_poly(x, y, vs):
    c = False
    n = len(vs)
    j = n - 1
    for i in range(n):
        xi, yi = vs[i]
        xj, yj = vs[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            c = not c
        j = i
    return c


# --------------------------------------------------------------------------
# package geometry extraction
# --------------------------------------------------------------------------
def parse_packages(root):
    """Map (library, package) -> dict with pad geometry + mask metadata.

    Each entry:
      pads: {name: (lx, ly, dx, dy, kind)}  kind in {'smd','tht'}
      tstop: [ (cx, cy, w, h) ]  explicit tStop mask polys (local coords, bbox)
      smd_stop_default: [ (cx, cy, w, h) ] mask windows for smds w/o explicit poly
      tcream: count of tCream polys
      ep: largest smd flagged as exposed pad (stop='no') or None
      pitch: min center-to-center distance among numbered smds
    """
    out = {}
    for lib in root.iter("library"):
        ln = lib.get("name")
        for pk in lib.iter("package"):
            pads = {}
            centers = []
            smd_for_mask = []
            ep = None
            for s in pk.iter("smd"):
                x, y = float(s.get("x")), float(s.get("y"))
                dx, dy = float(s.get("dx")), float(s.get("dy"))
                ang, _ = rot_angle(s.get("rot"))
                if ang in (90, 270):
                    dx, dy = dy, dx
                pads[s.get("name")] = (x, y, dx, dy, "smd")
                centers.append((x, y, dx, dy))
                area = dx * dy
                # exposed pad heuristic: stop='no' (mask defined by windowpane)
                if s.get("stop") == "no" and (ep is None or area > ep[4]):
                    ep = (x, y, dx, dy, area)
                if s.get("stop") != "no":
                    smd_for_mask.append((x, y, dx, dy))
            for p in pk.iter("pad"):
                x, y = float(p.get("x")), float(p.get("y"))
                dia = max(float(p.get("diameter", "0") or 0),
                          float(p.get("drill", "0") or 0) * 1.5, 1.0)
                pads[p.get("name")] = (x, y, dia, dia, "tht")
            # explicit tStop polygons (mask windows)
            tstop = []
            for poly in pk.iter("polygon"):
                if poly.get("layer") != TSTOP_TOP:
                    continue
                vs = [(float(v.get("x")), float(v.get("y"))) for v in poly.findall("vertex")]
                if not vs:
                    continue
                xs = [v[0] for v in vs]
                ys = [v[1] for v in vs]
                tstop.append(((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2,
                              max(xs) - min(xs), max(ys) - min(ys)))
            # tStop rectangles too (Type-C connectors use these)
            for rc in pk.iter("rectangle"):
                if rc.get("layer") != TSTOP_TOP:
                    continue
                x1, y1 = float(rc.get("x1")), float(rc.get("y1"))
                x2, y2 = float(rc.get("x2")), float(rc.get("y2"))
                tstop.append(((x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1), abs(y2 - y1)))
            tcream = sum(1 for p in pk.iter("polygon") if p.get("layer") == TCREAM_TOP)
            # pitch = min pairwise smd center distance
            pitch = None
            for i in range(len(centers)):
                for j in range(i + 1, len(centers)):
                    d = math.hypot(centers[i][0] - centers[j][0], centers[i][1] - centers[j][1])
                    if 0.05 < d and (pitch is None or d < pitch):
                        pitch = d
            out[(ln, pk.get("name"))] = {
                "pads": pads, "tstop": tstop, "smd_mask": smd_for_mask,
                "tcream": tcream, "ep": ep, "pitch": pitch, "name": pk.get("name"),
            }
    return out


def element_pads(root, packages):
    """Return per-element placed pad rects + bounding box, and pad lookup."""
    elems = {}     # name -> list of (rect, kind)
    ebbox = {}     # name -> (x0,y0,x1,y1)
    padlook = {}   # (elem, pad) -> (rect, kind)
    emeta = {}     # name -> dict(pkg, lib, x, y, ang, mir, pitch, tstop placed, ep placed)
    for e in root.iter("element"):
        nm = e.get("name")
        ang, mir = rot_angle(e.get("rot"))
        ex, ey = float(e.get("x")), float(e.get("y"))
        pk = packages.get((e.get("library"), e.get("package")))
        if not pk:
            continue
        rects = []
        for pad, (lx, ly, dx, dy, kind) in pk["pads"].items():
            rx, ry = place(lx, ly, ang, mir)
            cx, cy = ex + rx, ey + ry
            w, h = (dy, dx) if ang in (90, 270) else (dx, dy)
            r = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            rects.append((r, kind))
            padlook[(nm, pad)] = (r, kind)
        if not rects:
            continue
        elems[nm] = rects
        xs0 = min(r[0][0] for r in rects)
        ys0 = min(r[0][1] for r in rects)
        xs1 = max(r[0][2] for r in rects)
        ys1 = max(r[0][3] for r in rects)
        ebbox[nm] = (xs0, ys0, xs1, ys1)
        # place tStop windows + EP into board coords
        placed_stop = []
        for (cx, cy, w, h) in pk["tstop"]:
            px, py = place(cx, cy, ang, mir)
            pw, ph = (h, w) if ang in (90, 270) else (w, h)
            placed_stop.append((ex + px, ey + py, pw, ph))
        ep_placed = None
        if pk["ep"]:
            cx, cy, w, h, _ = pk["ep"]
            px, py = place(cx, cy, ang, mir)
            pw, ph = (h, w) if ang in (90, 270) else (w, h)
            ep_placed = (ex + px, ey + py, pw, ph)
        emeta[nm] = {"pkg": e.get("package"), "lib": e.get("library"),
                     "x": ex, "y": ey, "ang": ang, "mir": mir,
                     "pitch": pk["pitch"], "tstop": placed_stop,
                     "smd_mask": pk["smd_mask"], "ep": ep_placed,
                     "tcream": pk["tcream"]}
    return elems, ebbox, padlook, emeta


# --------------------------------------------------------------------------
# top-level analysis
# --------------------------------------------------------------------------
def analyze(brd_path):
    root = ET.parse(brd_path).getroot()
    plain = root.find("drawing/board/plain")
    findings = []

    def add(sev, fid, msg, rec=""):
        findings.append({"severity": sev, "id": fid, "message": msg, "recommendation": rec})

    # ---- outline & area -------------------------------------------------
    xs, ys = [], []
    if plain is not None:
        for w in plain.iter("wire"):
            if w.get("layer") in EDGE_LAYERS:
                xs += [float(w.get("x1")), float(w.get("x2"))]
                ys += [float(w.get("y1")), float(w.get("y2"))]
    if xs and ys:
        W = max(xs) - min(xs)
        H = max(ys) - min(ys)
        ox, oy = min(xs), min(ys)
    else:
        W = H = ox = oy = 0.0
        add("WARN", "outline.missing",
            "No board outline wires found on Dimension layer (20).",
            "Draw a closed outline on layer 20 so fab can determine board size.")
    area_cm2 = W * H / 100.0

    # ---- component count ------------------------------------------------
    packages = parse_packages(root)
    elems, ebbox, padlook, emeta = element_pads(root, packages)
    n_elem = sum(1 for _ in root.iter("element"))
    n_placed = len(elems)

    # ---- copper stackup -------------------------------------------------
    active_copper = []
    for L in root.iter("layer"):
        if L.get("number") in COPPER_LAYERS and L.get("active") == "yes":
            active_copper.append(L.get("number"))
    # planes present on inner layers confirm a true multilayer stackup
    plane_layers = set()
    for s in root.iter("signal"):
        for p in s.findall("polygon"):
            if p.get("layer") in INNER_LAYERS:
                plane_layers.add(p.get("layer"))
    n_copper = len(set(active_copper))
    add("INFO", "stackup.copper",
        f"{n_copper} active copper layers: {sorted(set(active_copper), key=int)}; "
        f"inner plane polygons on layer(s) {sorted(plane_layers) or 'none'}.",
        "")

    # ---- placement: min pad gap + overlaps ------------------------------
    min_gap = 999.0
    min_gap_pair = None
    overlaps = []
    for (n1, a), (n2, b) in itertools.combinations(elems.items(), 2):
        # cheap bbox reject
        if rect_gap(ebbox[n1], ebbox[n2]) > min_gap and min_gap < 999.0:
            continue
        g = min(rect_gap(ra, rb) for ra, _ in a for rb, _ in b)
        if g < 0:
            overlaps.append((n1, n2))
        if g < min_gap:
            min_gap = g
            min_gap_pair = (n1, n2)
    if overlaps:
        ol = ", ".join(f"{x}/{y}" for x, y in overlaps[:8])
        add("ERROR", "place.overlap",
            f"{len(overlaps)} element pad-overlap(s): {ol}{' ...' if len(overlaps) > 8 else ''}.",
            "Separate overlapping footprints; pads must not touch across parts.")
    elif min_gap_pair:
        sev = "WARN" if min_gap < 0.2 else "INFO"
        add(sev, "place.min_gap",
            f"Smallest pad-to-pad gap between parts is {min_gap:.3f} mm "
            f"({min_gap_pair[0]}<->{min_gap_pair[1]}).",
            "Keep >=0.2 mm clearance between adjacent parts for assembly." if min_gap < 0.2 else "")

    # ---- connectors on edge --------------------------------------------
    off_edge = []
    n_conn = 0
    if W and H:
        for nm, bb in ebbox.items():
            # refdes prefix = leading letters before any digit or '_'
            prefix = ""
            for ch in nm.upper():
                if ch.isalpha():
                    prefix += ch
                else:
                    break
            if prefix not in EDGE_CONN_PREFIXES:
                continue
            n_conn += 1
            x0, y0, x1, y1 = bb
            on = (x0 <= ox + OUTLINE_TOL or y0 <= oy + OUTLINE_TOL or
                  x1 >= ox + W - OUTLINE_TOL or y1 >= oy + H - OUTLINE_TOL)
            if not on:
                off_edge.append(nm)
        if off_edge:
            add("WARN", "place.connector_edge",
                f"{len(off_edge)} connector(s) not on a board edge: {', '.join(off_edge)}.",
                "Move connectors to the board perimeter for cable access.")
        elif n_conn:
            add("INFO", "place.connector_edge",
                f"All {n_conn} detected connector(s) sit on a board edge.", "")

    # ---- connectivity (airwires) ---------------------------------------
    conn = analyze_connectivity(root, padlook)
    if conn["open_air"] == 0 and conn["total_air"] > 0:
        add("INFO", "net.connectivity",
            f"All nets routed: 0 airwires across {conn['total_air']} connections "
            f"({conn['n_signals']} signals, 100.0% routed).", "")
    elif conn["total_air"] > 0:
        sev = "ERROR" if conn["pct_routed"] < 99.5 else "WARN"
        worst = ", ".join(f"{n}({c}grp)" for n, _, c, _ in conn["open_nets"][:6])
        add(sev, "net.connectivity",
            f"{conn['open_air']} airwire(s) across {conn['n_open']} net(s): "
            f"{conn['pct_routed']:.1f}% routed. Open: {worst}.",
            "Route remaining airwires (inner planes auto-connect their net's pads/vias).")

    # ---- planes: pour status -------------------------------------------
    plane_info = []
    declared_plane_nets = set()
    for s in root.iter("signal"):
        for p in s.findall("polygon"):
            if p.get("layer") in INNER_LAYERS:
                declared_plane_nets.add((s.get("name"), p.get("layer")))
                pour = p.get("pour", "solid")
                rank = p.get("rank")
                plane_info.append((s.get("name"), p.get("layer"), pour, rank))
                if pour != "solid":
                    add("WARN", "plane.pour",
                        f"Plane '{s.get('name')}' on layer {p.get('layer')} pour='{pour}' (not solid).",
                        "Set pour=solid for a continuous reference/return plane.")
    if plane_info:
        desc = "; ".join(f"{n}@L{l} pour={po} rank={rk or '-'}" for n, l, po, rk in plane_info)
        add("INFO", "plane.present", f"Inner plane(s): {desc}.", "")
    # nets named like a plane but with no poured polygon
    for s in root.iter("signal"):
        nm = s.get("name", "")
        looks_plane = nm in ("GND", "AGND", "PGND") or nm in POWER_HINTS or nm in ("P3V3", "3V3")
        has_inner_poly = any(p.get("layer") in INNER_LAYERS for p in s.findall("polygon"))
        crefs = s.findall("contactref")
        if looks_plane and not has_inner_poly and len(crefs) >= 4 and INNER_LAYERS:
            add("WARN", "plane.missing",
                f"Net '{nm}' looks like a plane net ({len(crefs)} pads) but has no poured "
                f"polygon on an inner layer.",
                "Pour a copper plane for this net on an inner layer.")

    # ---- net classes / power traces (IPC-2221) -------------------------
    classes = {}
    for c in root.iter("class"):
        classes[c.get("number")] = (c.get("name"), float(c.get("width", "0") or 0))
    # actual trace widths used per signal (max width seen)
    sig_class = {}
    sig_maxw = {}
    for s in root.iter("signal"):
        sig_class[s.get("name")] = s.get("class", "0")
        mw = 0.0
        for w in s.iter("wire"):
            if w.get("layer") in COPPER_LAYERS:
                mw = max(mw, float(w.get("width", "0") or 0))
        sig_maxw[s.get("name")] = mw
    power_nets = []
    for nm in sig_maxw:
        if any(h in nm.upper() for h in POWER_HINTS):
            cls = classes.get(sig_class.get(nm, "0"), ("default", 0.0))
            power_nets.append((nm, cls[0], cls[1], sig_maxw[nm]))
    table_str = ", ".join(f"{w}mm~{a}A" for w, a in IPC_TABLE[:5])
    if power_nets:
        lines = []
        for nm, cname, cw, mw in sorted(power_nets):
            eff = mw if mw > 0 else cw
            amps = ipc_amps(eff)
            lines.append(f"{nm}(class {cname} {cw}mm, routed {mw or 0:.2f}mm ~ {amps:.1f}A)")
        add("INFO", "net.power_traces",
            f"Power nets vs IPC-2221 (1oz/10C: {table_str}): " + "; ".join(lines),
            "Confirm each power net's narrowest segment carries its expected current.")
    if classes:
        cdesc = "; ".join(f"{n}:{nm}={w}mm" for n, (nm, w) in sorted(classes.items()))
        add("INFO", "net.classes", f"Net classes: {cdesc}.", "")

    # ---- solder-mask dam vs pitch --------------------------------------
    dam_results = []
    for nm, m in emeta.items():
        pitch = m["pitch"]
        if pitch is None or pitch > FINE_PITCH + 1e-6:
            continue
        dam = mask_dam(m)
        if dam is not None:
            dam_results.append((nm, m["pkg"], pitch, dam))
    if dam_results:
        worst = min(dam_results, key=lambda r: r[3])
        for nm, pkg, pitch, dam in sorted(dam_results, key=lambda r: r[3]):
            if dam < DAM_MIN:
                add("WARN", "mask.dam",
                    f"{nm} ({pkg}, pitch {pitch:.2f}mm): smallest mask dam ~{dam:.3f}mm "
                    f"(< {DAM_MIN}mm).",
                    "Reduce mask expansion or use mask-defined pads so the web/dam stays >=0.22mm.")
        per = "; ".join(f"{nm} ({pkg}, {pitch:.2f}mm pitch) dam ~{dam:.3f}mm"
                        for nm, pkg, pitch, dam in sorted(dam_results, key=lambda r: r[3]))
        add("INFO", "mask.dam.summary",
            f"Fine-pitch (<= {FINE_PITCH}mm) parts: {len(dam_results)}; "
            f"smallest dam ~{worst[3]:.3f}mm. {per}.", "")

    # ---- thermal / exposed pad -----------------------------------------
    ep_parts = []
    for nm, m in emeta.items():
        if not m["ep"]:
            continue
        ex, ey, ew, eh = m["ep"]
        # count windowpane openings: tStop windows fully inside the EP bbox
        epr = (ex - ew / 2, ey - eh / 2, ex + ew / 2, ey + eh / 2)
        panes = 0
        for (tx, ty, tw, th) in m["tstop"]:
            tr = (tx - tw / 2, ty - th / 2, tx + tw / 2, ty + th / 2)
            if (tr[0] >= epr[0] - 0.1 and tr[1] >= epr[1] - 0.1 and
                    tr[2] <= epr[2] + 0.1 and tr[3] <= epr[3] + 0.1):
                panes += 1
        # thermal vias = vias from any signal sitting inside the EP footprint
        tvias = 0
        for s in root.iter("signal"):
            for v in s.findall("via"):
                if epr[0] <= float(v.get("x")) <= epr[2] and epr[1] <= float(v.get("y")) <= epr[3]:
                    tvias += 1
        ep_parts.append((nm, m["pkg"], ew, eh, panes, tvias))
    if ep_parts:
        for nm, pkg, ew, eh, panes, tvias in ep_parts:
            add("INFO", "thermal.ep",
                f"{nm} ({pkg}): exposed pad {ew:.1f}x{eh:.1f}mm, "
                f"{panes} mask/paste window(s), {tvias} thermal via(s) under EP.",
                "Use a paste windowpane (~50-70% coverage) and a thermal via array if 0." if tvias == 0 else "")

    summary = {
        "board": os.path.basename(brd_path),
        "width_mm": round(W, 2), "height_mm": round(H, 2),
        "area_cm2": round(area_cm2, 2),
        "n_elements": n_placed, "n_elements_raw": n_elem,
        "copper_layers": n_copper, "inner_planes": sorted(plane_layers),
        "min_pad_gap_mm": round(min_gap, 3) if min_gap < 999 else None,
        "overlaps": len(overlaps),
        "connectors_off_edge": off_edge,
        "n_signals": conn["n_signals"],
        "airwires": conn["open_air"], "total_connections": conn["total_air"],
        "pct_routed": round(conn["pct_routed"], 1),
        "power_nets": [n for n, *_ in power_nets],
        "fine_pitch_parts": len(dam_results),
        "smallest_mask_dam_mm": round(min(r[3] for r in dam_results), 3) if dam_results else None,
        "ep_parts": len(ep_parts),
    }
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"summary": summary, "counts": counts, "findings": findings}


def ipc_amps(width_mm):
    """Interpolate ampacity for a trace width from the IPC-2221 table."""
    if width_mm <= 0:
        return 0.0
    tbl = IPC_TABLE
    if width_mm <= tbl[0][0]:
        return tbl[0][1] * width_mm / tbl[0][0]
    if width_mm >= tbl[-1][0]:
        return tbl[-1][1]
    for (w0, a0), (w1, a1) in zip(tbl, tbl[1:]):
        if w0 <= width_mm <= w1:
            return a0 + (a1 - a0) * (width_mm - w0) / (w1 - w0)
    return tbl[-1][1]


def mask_dam(meta):
    """Smallest solder-mask dam (web) for a fine-pitch part, in mm.

    Prefer explicit tStop windows (mask-defined): dam = min center-pitch minus
    window extent along the pitch axis. Fall back to pad gap + default mask
    expansion (4 mil/side typical) when no explicit windows exist.
    """
    wins = meta["tstop"]
    if len(wins) >= 2:
        best = None
        for i in range(len(wins)):
            for j in range(i + 1, len(wins)):
                ax, ay, aw, ah = wins[i]
                bx, by, bw, bh = wins[j]
                # gap along each axis between the two window rects
                gx = abs(ax - bx) - (aw + bw) / 2
                gy = abs(ay - by) - (ah + bh) / 2
                # adjacent-along-a-row pair: one axis aligned, other is the dam
                if abs(ax - bx) < 0.05:
                    d = gy
                elif abs(ay - by) < 0.05:
                    d = gx
                else:
                    continue
                if d > 0 and (best is None or d < best):
                    best = d
        if best is not None:
            return best
    # fall back: smd pads + default mask expansion
    smds = meta["smd_mask"]
    if len(smds) >= 2:
        exp = 0.1016  # 4 mil default mask expansion per side
        best = None
        for i in range(len(smds)):
            for j in range(i + 1, len(smds)):
                ax, ay, aw, ah = smds[i]
                bx, by, bw, bh = smds[j]
                if abs(ax - bx) < 0.05:
                    d = abs(ay - by) - (ah + bh) / 2 - 2 * exp
                elif abs(ay - by) < 0.05:
                    d = abs(ax - bx) - (aw + bw) / 2 - 2 * exp
                else:
                    continue
                if d > -1 and (best is None or d < best):
                    best = d
        return best
    return None


# --------------------------------------------------------------------------
# connectivity / airwires (planes connect their net's pads + vias)
# --------------------------------------------------------------------------
def analyze_connectivity(root, padlook):
    par = {}

    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    def uni(a, b):
        par[find(a)] = find(b)

    open_air = 0
    total_air = 0
    n_signals = 0
    open_nets = []
    for sig in root.iter("signal"):
        n_signals += 1
        net = sig.get("name")
        crefs = [(c.get("element"), c.get("pad")) for c in sig.findall("contactref")]
        if len(crefs) < 2:
            continue
        par.clear()
        pads = [("P", e, p) for (e, p) in crefs]
        for pn in pads:
            find(pn)
        wires = [(float(w.get("x1")), float(w.get("y1")),
                  float(w.get("x2")), float(w.get("y2")),
                  float(w.get("width")), w.get("layer"))
                 for w in sig.findall("wire") if w.get("layer") in COPPER_LAYERS]
        vias = []
        for v in sig.findall("via"):
            ext = v.get("extent", "1-16")
            lo, hi = ext.split("-")
            vl = set(l for l in COPPER_LAYERS if int(lo) <= int(l) <= int(hi))
            vias.append((float(v.get("x")), float(v.get("y")), vl))
        polys = {}
        for pg in sig.findall("polygon"):
            polys[pg.get("layer")] = [(float(q.get("x")), float(q.get("y")))
                                      for q in pg.findall("vertex")]

        def wkey(L, x, y):
            return ("W", L, round(x, 2), round(y, 2))

        for (x1, y1, x2, y2, wd, L) in wires:
            a = wkey(L, x1, y1)
            b = wkey(L, x2, y2)
            uni(a, b)
            tol = wd / 2 + 0.13
            for pn in pads:
                info = padlook.get((pn[1], pn[2]))
                if not info:
                    continue
                r, kind = info
                if (kind == "tht" or L == "1" or L == "16") and near_rect(x1, y1, x2, y2, r, tol):
                    uni(a, pn)
            for vi, (vx, vy, vl) in enumerate(vias):
                if L in vl and seg_pt_dist(vx, vy, x1, y1, x2, y2) <= tol + 0.35:
                    uni(a, ("V", vi))
        for vi, (vx, vy, vl) in enumerate(vias):
            for pn in pads:
                info = padlook.get((pn[1], pn[2]))
                if not info:
                    continue
                r, kind = info
                if (kind == "tht" or "1" in vl or "16" in vl) and \
                        r[0] - 0.2 <= vx <= r[2] + 0.2 and r[1] - 0.2 <= vy <= r[3] + 0.2:
                    uni(("V", vi), pn)
            for pl in INNER_LAYERS:
                if pl in vl and pl in polys and pt_in_poly(vx, vy, polys[pl]):
                    uni(("V", vi), ("PLANE", pl))
        # tht pads sitting in a pour tie to the plane
        for pn in pads:
            info = padlook.get((pn[1], pn[2]))
            if info and info[1] == "tht":
                cx = (info[0][0] + info[0][2]) / 2
                cy = (info[0][1] + info[0][3]) / 2
                for pl in INNER_LAYERS:
                    if pl in polys and pt_in_poly(cx, cy, polys[pl]):
                        uni(pn, ("PLANE", pl))

        roots = {}
        for pn in pads:
            roots.setdefault(find(pn), []).append(pn)
        total_air += len(pads) - 1
        if len(roots) > 1:
            open_air += len(roots) - 1
            open_nets.append((net, len(pads), len(roots),
                              [[f"{e}.{p}" for (_, e, p) in g] for g in roots.values()]))

    pct = 100.0 if total_air == 0 else 100.0 * (1 - open_air / total_air)
    open_nets.sort(key=lambda r: -r[2])
    return {"open_air": open_air, "total_air": total_air, "pct_routed": pct,
            "n_signals": n_signals, "n_open": len(open_nets), "open_nets": open_nets}


# --------------------------------------------------------------------------
# text report
# --------------------------------------------------------------------------
def print_text(res):
    s = res["summary"]
    c = res["counts"]
    print("=" * 70)
    print(f"  BOARD DFM REVIEW  -  {s['board']}")
    print("=" * 70)
    print(f"  Size        : {s['width_mm']} x {s['height_mm']} mm  "
          f"({s['area_cm2']} cm^2)")
    print(f"  Components  : {s['n_elements']} placed "
          f"({s['n_elements_raw']} elements total)")
    print(f"  Stackup     : {s['copper_layers']} copper layers; "
          f"inner planes on L{','.join(s['inner_planes']) or '-'}")
    gap = s['min_pad_gap_mm']
    print(f"  Min pad gap : {gap if gap is not None else 'n/a'} mm   "
          f"overlaps: {s['overlaps']}")
    print(f"  Connectivity: {s['pct_routed']}% routed  "
          f"({s['airwires']} airwires / {s['total_connections']} conns, "
          f"{s['n_signals']} signals)")
    print(f"  Power nets  : {', '.join(s['power_nets']) or 'none'}")
    fp = s['smallest_mask_dam_mm']
    print(f"  Fine-pitch  : {s['fine_pitch_parts']} part(s); "
          f"smallest mask dam {fp if fp is not None else 'n/a'} mm")
    print(f"  Exposed pad : {s['ep_parts']} part(s) with EP/thermal")
    print(f"  Findings    : {c.get('ERROR',0)} ERROR, "
          f"{c.get('WARN',0)} WARN, {c.get('INFO',0)} INFO")
    print("-" * 70)
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    marks = {"ERROR": "[X]", "WARN": "[!]", "INFO": "[i]"}
    for f in sorted(res["findings"], key=lambda f: order.get(f["severity"], 9)):
        print(f"{marks.get(f['severity'],'[ ]')} {f['severity']:5s} {f['id']}")
        print(f"      {f['message']}")
        if f["recommendation"]:
            print(f"      -> {f['recommendation']}")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description="EAGLE/Fusion .brd DFM analyzer")
    ap.add_argument("brd", help="path to .brd file")
    ap.add_argument("--text", action="store_true", help="human-readable report")
    ap.add_argument("-o", "--output", help="write JSON findings to this path")
    args = ap.parse_args()

    if not os.path.isfile(args.brd):
        print(f"error: no such file: {args.brd}", file=sys.stderr)
        return 2
    res = analyze(args.brd)

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(res, fh, indent=2)
        print(f"wrote {args.output}")
    if args.text or not args.output:
        print_text(res)
    return 1 if res["counts"].get("ERROR", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
