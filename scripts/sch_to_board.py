#!/usr/bin/env python3
"""
sch_to_board.py -- one-step "create a board from a schematic" pipeline.

Reads an EXISTING EAGLE / Fusion Electronics schematic (.sch) and writes a fully
PLACED, UNROUTED board (.brd) using this skill's HPWL+BLF placement optimizer
(place_components.py). The board reuses the schematic's exact libraries/packages
(copied VERBATIM so Fusion's board<->schematic consistency check stays clean),
positions every part with the optimizer, and emits one <signal> per net wired to
every pad via the deviceset pin->pad map. Routing is left to the user; the result
loads in Fusion as "schematic switched to board, auto-placed".

Pipeline:
  1. Parse the .sch (ElementTree). Copy the raw <libraries> block VERBATIM. Resolve
     each part's package via its library's deviceset/device, and build the netlist
     from <net>/<segment>/<pinref>, mapping each (part,pin) to a pad via the
     device's <connects><connect pin= pad=>.
  2. Compute each part's footprint bbox (smd + pad geometry, rot-aware) and build a
     place_components spec. Connectors (part name J/CONN/SW, or genuinely
     through-hole / many-pad connectors) are pinned to the board edge.
  3. Run place_components.py to get placements.
  4. Emit a valid EAGLE .brd: DOCTYPE preserved, standard <layers>, rectangular
     board outline on layer 20, the verbatim <libraries>, default classes /
     designrules / autorouter, one <element> per placed part, one <signal> per net
     with a <contactref> for every mapped pad (no wires, no vias).

Usage:
  python3 sch_to_board.py <input.sch> -o <output.brd> [--board WxH] [--margin 2.0] [--text]
"""
import os, sys, math, json, argparse, subprocess, tempfile
import xml.etree.ElementTree as ET
import xml.sax.saxutils as su

HERE = os.path.dirname(os.path.abspath(__file__))
PLACER = os.path.join(HERE, "place_components.py")


def esc(s):
    return su.escape(str(s), {'"': "&quot;"})


# ============================================================ schematic parsing
def slice_libraries_block(raw):
    """Return the raw '<libraries>...</libraries>' text VERBATIM (byte-for-byte) so the
    board embeds the schematic's exact packages. EAGLE keeps a single <libraries> block
    inside <schematic>; we take the first opening tag through its matching close."""
    i = raw.find("<libraries>")
    if i < 0:
        i = raw.find("<libraries ")          # tolerate attributes on the tag
    if i < 0:
        raise ValueError("no <libraries> block found in schematic")
    j = raw.find("</libraries>", i)
    if j < 0:
        raise ValueError("unterminated <libraries> block in schematic")
    return raw[i:j + len("</libraries>")]


def pkg_bbox(pkg_el):
    """Footprint bbox (xmin, ymin, xmax, ymax) in the package's local R0 frame, from its
    copper + outline geometry. Mirrors generate_brd.py's pkg_bbox (rot-aware smd dx/dy)."""
    xs, ys = [], []
    for s in pkg_el.findall("smd"):
        x, y = float(s.get("x")), float(s.get("y"))
        dx, dy = float(s.get("dx")), float(s.get("dy"))
        if s.get("rot", "R0") in ("R90", "R270"):
            dx, dy = dy, dx
        xs += [x - dx / 2, x + dx / 2]; ys += [y - dy / 2, y + dy / 2]
    for p in pkg_el.findall("pad"):
        x, y = float(p.get("x")), float(p.get("y"))
        d = float(p.get("diameter") or (float(p.get("drill", "1")) * 1.5))
        xs += [x - d / 2, x + d / 2]; ys += [y - d / 2, y + d / 2]
    for w in pkg_el.findall("wire"):
        xs += [float(w.get("x1")), float(w.get("x2"))]
        ys += [float(w.get("y1")), float(w.get("y2"))]
    for c in pkg_el.findall("circle"):
        x, y, r = float(c.get("x")), float(c.get("y")), float(c.get("radius"))
        xs += [x - r, x + r]; ys += [y - r, y + r]
    for h in pkg_el.findall("hole"):
        x, y = float(h.get("x")), float(h.get("y")); r = float(h.get("drill")) / 2
        xs += [x - r, x + r]; ys += [y - r, y + r]
    for r in pkg_el.findall("rectangle"):
        xs += [float(r.get("x1")), float(r.get("x2"))]
        ys += [float(r.get("y1")), float(r.get("y2"))]
    for pg in pkg_el.findall("polygon"):
        for v in pg.findall("vertex"):
            xs.append(float(v.get("x"))); ys.append(float(v.get("y")))
    if not xs:
        return (-0.5, -0.5, 0.5, 0.5)
    return (min(xs), min(ys), max(xs), max(ys))


def parse_schematic(path):
    """Parse a .sch into the structures the pipeline needs.

    Returns dict with:
      libraries_xml : verbatim '<libraries>...</libraries>' text
      parts         : [{name, library, package, value, pinpad{pin:[pads]}, bbox,
                        n_smd, n_pad}]
      nets          : [{name, members:[part,...]}]
      signals       : {net: [(part, pad), ...]}   board contactrefs
    """
    raw = open(path, encoding="utf-8").read()
    libraries_xml = slice_libraries_block(raw)
    root = ET.fromstring(raw)
    sch = root.find(".//schematic")
    if sch is None:
        raise ValueError(f"{path}: no <schematic> body (is this a .brd?)")
    libs = sch.find("libraries")

    # library -> {pkgmap, dev2pkg, dev2conn, pkg_el}
    libmap = {}
    for lib in libs.findall("library"):
        lname = lib.get("name")
        pkg_el = {}
        pkgs = lib.find("packages")
        if pkgs is not None:
            for pk in pkgs.findall("package"):
                pkg_el[pk.get("name")] = pk
        dev2pkg, dev2conn = {}, {}
        dss = lib.find("devicesets")
        if dss is not None:
            for ds in dss.findall("deviceset"):
                for dev in ds.findall("devices/device"):
                    key = (ds.get("name"), dev.get("name") or "")
                    dev2pkg[key] = dev.get("package")
                    conn = {}
                    cs = dev.find("connects")
                    if cs is not None:
                        for c in cs.findall("connect"):
                            conn.setdefault(c.get("pin"), []).extend(
                                (c.get("pad") or "").split())
                    dev2conn[key] = conn
        libmap[lname] = dict(pkg_el=pkg_el, dev2pkg=dev2pkg, dev2conn=dev2conn)

    # parts: resolve package + pin->pad map + footprint bbox
    parts = []
    part_info = {}                                      # name -> (pinpad, library)
    for pt in sch.findall(".//parts/part"):
        name = pt.get("name")
        lname = pt.get("library")
        ds = pt.get("deviceset")
        dev = pt.get("device") or ""
        lm = libmap.get(lname)
        if lm is None:
            raise ValueError(f"part {name}: library {lname!r} not embedded in schematic")
        pkg = lm["dev2pkg"].get((ds, dev))
        if pkg is None:
            raise ValueError(f"part {name}: device ({ds!r},{dev!r}) has no package")
        pk_el = lm["pkg_el"].get(pkg)
        if pk_el is None:
            raise ValueError(f"part {name}: package {pkg!r} missing in library {lname!r}")
        pinpad = lm["dev2conn"].get((ds, dev), {})
        bbox = pkg_bbox(pk_el)
        n_smd = len(pk_el.findall("smd"))
        n_pad = len(pk_el.findall("pad"))
        parts.append(dict(name=name, library=lname, package=pkg,
                          value=pt.get("value"), pinpad=pinpad, bbox=bbox,
                          n_smd=n_smd, n_pad=n_pad))
        part_info[name] = (pinpad, lname)

    # nets + board signals (contactrefs) from <net>/<segment>/<pinref>
    nets, signals = [], {}
    for net in sch.findall(".//nets/net"):
        nname = net.get("name")
        members = []
        for seg in net.findall("segment"):
            for pr in seg.findall("pinref"):
                pname = pr.get("part"); pin = pr.get("pin")
                if pname not in part_info:
                    continue
                if pname not in members:
                    members.append(pname)
                pinpad = part_info[pname][0]
                for pad in pinpad.get(pin, []):
                    ref = (pname, pad)
                    signals.setdefault(nname, [])
                    if ref not in signals[nname]:
                        signals[nname].append(ref)
        if members:
            nets.append(dict(name=nname, members=members))

    return dict(libraries_xml=libraries_xml, parts=parts, nets=nets, signals=signals)


# ============================================================ placement spec
def is_connector(p):
    """Edge-pin a part only when it is a genuine off-board connector: a J*/CONN*
    reference, or a non-IC through-hole / many-pad connector. ICs (U/IC) and tactile
    switches (SW) stay interior -- switches mount on-board near their IC, and dense
    chips (WROOM, DRV) only carry stitching pads, not connector through-holes."""
    nm = p["name"].upper()
    if nm.startswith(("J", "CONN")):
        return True
    if nm.startswith(("U", "IC", "SW")):
        return False
    through_hole = p["n_pad"] > 0
    many_pads = (p["n_smd"] + p["n_pad"]) >= 8
    return through_hole or many_pads


def assign_edges(connectors):
    """Distribute edge-pinned connectors across the two PARALLEL edges (bottom/top),
    longest first onto whichever currently has the shorter run. Only parallel edges are
    used: the placer pins each edge's parts starting from the same corner offset, so
    mixing perpendicular edges would collide the two parts sharing a corner. Splitting
    across bottom+top halves the required board width with no corner conflict.
    Returns ({part_name: edge}, max_edge_run)."""
    used = {"bottom": 0.0, "top": 0.0}
    assign = {}
    order = sorted(connectors, key=lambda p: -(p["bbox"][2] - p["bbox"][0]))
    for p in order:
        run = (p["bbox"][2] - p["bbox"][0])            # length consumed along the edge
        edge = min(used, key=lambda e: used[e])
        assign[p["name"]] = edge
        used[edge] += run + 1.6                         # + an approximate edge gap
    return assign, max(used.values())


def build_spec(parts, nets, W, H, margin):
    """Assemble a place_components spec from the parsed parts/nets, with connectors
    distributed across the four edges."""
    connectors = [p for p in parts if is_connector(p)]
    edge_of, _ = assign_edges(connectors)
    spec_parts = []
    for p in parts:
        a, b, c, d = p["bbox"]
        sp = dict(name=p["name"], w=round(c - a, 4), h=round(d - b, 4), rot_ok=True)
        if p["name"] in edge_of:
            sp["fixed_edge"] = edge_of[p["name"]]
        spec_parts.append(sp)
    spec = dict(board=dict(w=W, h=H, margin=margin),
                parts=spec_parts,
                nets=[dict(name=n["name"], parts=list(n["members"])) for n in nets])
    return spec


def run_placer(spec):
    """Invoke place_components.py (same interpreter) on a temp spec; return (placements,
    metrics). Raises on placement failure."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f)
        spec_path = f.name
    out_path = spec_path + ".out.json"
    try:
        r = subprocess.run([sys.executable, PLACER, spec_path, "-o", out_path],
                           capture_output=True, text=True)
        if r.returncode not in (0, 2) or not os.path.exists(out_path):
            raise RuntimeError(f"placement failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}")
        with open(out_path) as fh:
            res = json.load(fh)
        return res["placements"], res["metrics"]
    finally:
        for pth in (spec_path, out_path):
            try:
                os.remove(pth)
            except OSError:
                pass


EDGE_GAP = 1.6          # placer's default gap between edge-mounted connectors


def width_floor(parts, margin):
    """Minimum board width that lets every bottom/top edge run start past the corner
    keep-out (e0 = margin + EDGE_GAP) and end before the far corner."""
    connectors = [p for p in parts if is_connector(p)]
    if not connectors:
        return 0.0
    _, run = assign_edges(connectors)
    return run + 2 * (margin + EDGE_GAP)


def auto_size(parts, nets, margin):
    """Size the board automatically: place once on a generous canvas, read the packed
    bounding box, add the margin all around (and enforce the edge-run width floor), then
    return that (W, H)."""
    tot = 0.0
    widest = 0.0
    for p in parts:
        pa, pb, pc, pd = p["bbox"]
        w, h = pc - pa, pd - pb
        tot += (w + 1.2) * (h + 1.2)
        widest = max(widest, w, h)
    side = math.sqrt(tot) * 1.8
    wfloor = width_floor(parts, margin)
    W0 = max(side, widest + 4 * margin, wfloor + 4)
    H0 = max(side, widest + 4 * margin)
    spec = build_spec(parts, nets, round(W0, 2), round(H0, 2), margin)
    placements, metrics = run_placer(spec)
    x0, y0, x1, y1 = metrics["used_extent"]
    W = max(round((x1 - x0) + 2 * margin, 2), round(wfloor, 2))
    H = round((y1 - y0) + 2 * margin, 2)
    return W, H


# ============================================================ geometry helpers
def rbox(bb, rot):
    """Rotate a local (xmin,ymin,xmax,ymax) bbox about the origin into the element frame."""
    a, b, c, d = bb
    if rot == "R0":   return (a, b, c, d)
    if rot == "R90":  return (-d, a, -b, c)
    if rot == "R180": return (-c, -d, -a, -b)
    if rot == "R270": return (b, -c, d, -a)
    return (a, b, c, d)


def rot_point(px, py, rot):
    """Rotate a local point about the origin by rot (R0/R90/R180/R270)."""
    if rot == "R0":   return (px, py)
    if rot == "R90":  return (-py, px)
    if rot == "R180": return (-px, -py)
    if rot == "R270": return (py, -px)
    return (px, py)


def real_origin(p, x, y, rot):
    """The placer plans with a CENTERED bbox, but a real footprint's origin may sit off
    its bbox centre (e.g. USB-C: bbox center != pad-array center). Shift the placed origin
    so the actual asymmetric footprint occupies the cell the placer reserved: subtract the
    rotated local bbox-centre offset from the placer's origin."""
    a, b, c, d = p["bbox"]
    cx, cy = (a + c) / 2.0, (b + d) / 2.0          # footprint centre in local R0 frame
    ox, oy = rot_point(cx, cy, rot)
    return x - ox, y - oy


def placed_rect(p, x, y, rot):
    """Board-frame tight bbox of the part once placed at the placer origin (with the real
    asymmetric footprint re-centred via real_origin)."""
    rx, ry = real_origin(p, x, y, rot)
    a, b, c, d = rbox(p["bbox"], rot)
    return (rx + a, ry + b, rx + c, ry + d)


# ============================================================ board emission
LAYERS = [
    (1, "Top"), (2, "Route2"), (15, "Route15"), (16, "Bottom"), (17, "Pads"),
    (18, "Vias"), (19, "Unrouted"), (20, "Dimension"), (21, "tPlace"), (22, "bPlace"),
    (23, "tOrigins"), (24, "bOrigins"), (25, "tNames"), (26, "bNames"), (27, "tValues"),
    (28, "bValues"), (29, "tStop"), (30, "bStop"), (31, "tCream"), (32, "bCream"),
    (33, "tFinish"), (34, "bFinish"), (35, "tGlue"), (36, "bGlue"), (37, "tTest"),
    (38, "bTest"), (39, "tKeepout"), (40, "bKeepout"), (41, "tRestrict"),
    (42, "bRestrict"), (43, "vRestrict"), (44, "Drills"), (45, "Holes"), (46, "Milling"),
    (47, "Measures"), (48, "Document"), (49, "Reference"), (51, "tDocu"), (52, "bDocu"),
    (90, "Modules"), (91, "Nets"), (92, "Busses"), (93, "Pins"), (94, "Symbols"),
    (95, "Names"), (96, "Values"), (97, "Info"), (98, "Guide"),
]

# Default copper net classes (match generate_brd.py / the routed reference board).
CLASSES = (
    '<class number="0" name="default" width="0" drill="0"/>\n'
    '<class number="1" name="pwr24" width="0.5" drill="0"/>\n'
    '<class number="2" name="pwr3v3" width="0.4" drill="0"/>'
)

# Minimal valid autorouter pass block; EAGLE/Fusion fill the rest with defaults.
AUTOROUTER = (
    '<autorouter>\n'
    '<pass name="Default">\n'
    '<param name="RoutingGrid" value="50mil"/>\n'
    '<param name="AutoGrid" value="1"/>\n'
    '<param name="Efforts" value="2"/>\n'
    '<param name="tpViaShape" value="round"/>\n'
    '<param name="PrefDir.1" value="a"/>\n'
    '<param name="PrefDir.16" value="a"/>\n'
    '</pass>\n'
    '</autorouter>'
)


def rect_outline(W, H):
    """Closed rectangular board outline on layer 20 (Dimension), CCW from origin."""
    seg = lambda x1, y1, x2, y2: (
        f'<wire x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
        f'width="0.2" layer="20"/>')
    return "\n".join([
        seg(0, 0, W, 0), seg(W, 0, W, H), seg(W, H, 0, H), seg(0, H, 0, 0)])


def element_xml(p, pos, with_text):
    """One <element> for a placed part. The placer plans with a centred bbox, so the
    real (possibly asymmetric) footprint origin is recovered via real_origin. with_text
    emits a visible >NAME on tNames."""
    x, y, rot = pos
    x, y = real_origin(p, x, y, rot)
    head = (f'<element name="{esc(p["name"])}" library="{esc(p["library"])}" '
            f'package="{esc(p["package"])}"'
            + (f' value="{esc(p["value"])}"' if p["value"] is not None else "")
            + f' x="{x:.3f}" y="{y:.3f}" rot="{rot}"')
    if not with_text:
        return head + "/>"
    body = (f'<attribute name="NAME" x="{x:.3f}" y="{y:.3f}" size="1.0" '
            f'layer="25" rot="{rot}" display="value"/>')
    return head + ">" + body + "</element>"


def emit_board(parsed, placements, W, H, with_text, eagle_version="9.6.2"):
    """Build the full .brd document text (DOCTYPE preserved)."""
    pos = {pl["name"]: (pl["x"], pl["y"], pl["rot"]) for pl in placements}
    by_name = {p["name"]: p for p in parsed["parts"]}

    layers_xml = "\n".join(
        f'<layer number="{n}" name="{esc(nm)}" color="7" fill="1" '
        f'visible="yes" active="yes"/>' for n, nm in LAYERS)
    plain = rect_outline(W, H)

    elements = "\n".join(
        element_xml(by_name[pl["name"]], pos[pl["name"]], with_text)
        for pl in placements if pl["name"] in by_name)

    def signal_xml(net, refs):
        crefs = "".join(
            f'<contactref element="{esc(en)}" pad="{esc(pad)}"/>' for en, pad in refs)
        return f'<signal name="{esc(net)}">{crefs}</signal>'
    signals_xml = "\n".join(
        signal_xml(net, refs) for net, refs in sorted(parsed["signals"].items()))

    doc = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="{esc(eagle_version)}">
<drawing>
<settings>
<setting alwaysvectorfont="no"/>
<setting verticaltext="up"/>
</settings>
<grid distance="0.1" unitdist="inch" unit="inch" style="lines" multiple="1" display="no" altdistance="0.01" altunitdist="inch" altunit="inch"/>
<layers>
{layers_xml}
</layers>
<board>
<plain>
{plain}
</plain>
{parsed["libraries_xml"]}
<attributes/>
<variantdefs/>
<classes>
{CLASSES}
</classes>
<designrules name="default"/>
{AUTOROUTER}
<elements>
{elements}
</elements>
<signals>
{signals_xml}
</signals>
</board>
</drawing>
</eagle>
'''
    return doc


# ============================================================ overlap report
def overlap_count(parts, placements):
    """Count unordered part-pair footprint-bbox intersections in the FINAL board frame
    (real asymmetric footprints, rot-aware) -> must be 0."""
    by_name = {p["name"]: p for p in parts}
    rs = []
    for pl in placements:
        p = by_name.get(pl["name"])
        if p is None:
            continue
        rs.append(placed_rect(p, pl["x"], pl["y"], pl["rot"]))
    n = 0
    for i in range(len(rs)):
        x0, y0, x1, y1 = rs[i]
        for j in range(i + 1, len(rs)):
            a0, b0, a1, b1 = rs[j]
            if x0 < a1 - 1e-6 and a0 < x1 - 1e-6 and y0 < b1 - 1e-6 and b0 < y1 - 1e-6:
                n += 1
    return n


# ============================================================ CLI
def parse_board_arg(s):
    parts = s.lower().replace("mm", "").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--board must be WxH, e.g. 50x40")
    return float(parts[0]), float(parts[1])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Create a placed, unrouted EAGLE board (.brd) from a schematic (.sch).")
    ap.add_argument("sch", help="input schematic (.sch)")
    ap.add_argument("-o", "--out", required=True, help="output board (.brd)")
    ap.add_argument("--board", type=parse_board_arg,
                    help="force board size WxH in mm (e.g. 50x40); else auto-sized")
    ap.add_argument("--margin", type=float, default=2.0,
                    help="part-to-edge clearance / auto-size padding (mm, default 2.0)")
    ap.add_argument("--text", action="store_true",
                    help="print a human-readable placement + board summary")
    args = ap.parse_args(argv)

    if not os.path.exists(args.sch):
        ap.error(f"schematic not found: {args.sch}")

    parsed = parse_schematic(args.sch)
    parts, nets = parsed["parts"], parsed["nets"]
    if not parts:
        ap.error("schematic has no parts")

    if args.board:
        W, H = args.board
    else:
        W, H = auto_size(parts, nets, args.margin)

    spec = build_spec(parts, nets, W, H, args.margin)
    placements, metrics = run_placer(spec)

    doc = emit_board(parsed, placements, W, H, args.text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(doc)

    ov = overlap_count(parts, placements)
    ncref = sum(len(r) for r in parsed["signals"].values())
    n_edge = sum(1 for sp in spec["parts"] if "fixed_edge" in sp)

    # within-board from the real placed footprints (not the placer's centred proxy)
    by_name = {p["name"]: p for p in parts}
    within = True
    for pl in placements:
        x0, y0, x1, y1 = placed_rect(by_name[pl["name"]], pl["x"], pl["y"], pl["rot"])
        if x0 < -1e-6 or y0 < -1e-6 or x1 > W + 1e-6 or y1 > H + 1e-6:
            within = False

    if args.text:
        print(f"input        : {args.sch}")
        print(f"output       : {args.out}")
        print(f"board        : {W:.1f} x {H:.1f} mm  ({W * H / 100.0:.2f} cm^2)"
              f"  {'(forced)' if args.board else '(auto-sized)'}")
        print(f"elements     : {len(placements)}  ({n_edge} edge-pinned connectors)")
        print(f"signals      : {len(parsed['signals'])}  ({ncref} contactrefs)")
        print(f"overlaps     : {ov}")
        print(f"within board : {'yes' if within else 'NO'}")
        print(f"min gap      : {metrics['min_gap']:.3f} mm")
        print(f"HPWL         : {metrics['hpwl']:.2f} mm")
        print()
        print(f"{'NAME':<12} {'X':>9} {'Y':>9} {'ROT':>5}")
        for pl in placements:
            rx, ry = real_origin(by_name[pl["name"]], pl["x"], pl["y"], pl["rot"])
            print(f"{pl['name']:<12} {rx:>9.3f} {ry:>9.3f} {pl['rot']:>5}")
    else:
        print(f"wrote {args.out}: {W:.1f}x{H:.1f}mm  "
              f"elements={len(placements)}  signals={len(parsed['signals'])}  "
              f"contactrefs={ncref}  overlaps={ov}")

    return 0 if ov == 0 and within else 2


if __name__ == "__main__":
    raise SystemExit(main())
