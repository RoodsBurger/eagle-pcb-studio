#!/usr/bin/env python3
"""
render_svg.py -- render an EAGLE / Autodesk Fusion Electronics .brd to an SVG for
quick visual inspection. Dependency-free (Python 3.8+ stdlib only): parses the XML
with ElementTree and emits SVG text directly.

Draws, with distinct per-layer colours and a legend:
  * board outline  (layer 20 Dimension / Milling 46) -- the closed board edge
  * copper top     (layer 1)  traces / pours
  * copper bottom  (layer 16) traces / pours
  * inner copper   (layers 2..15 Route2..Route15) traces / pours
  * SMD pads (top/bottom), THT pads, vias, drilled holes
  * silkscreen     (layers 21 tPlace / 22 bPlace / 25 tNames ...)
  * keepout / restrict outlines (39/40/41/42/43) lightly

The viewBox matches the board mm extents (1 SVG unit = 1 mm) and the whole drawing
is Y-flipped (SVG y grows downward) so it reads the same way it looks in the editor.

Usage:
    python3 render_svg.py <board.brd> -o <out.svg> [--text]
    python3 render_svg.py <board.brd>                 # writes <board>.svg next to it
"""
import sys, os, math, argparse
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- layer palette
# EAGLE layer number -> (svg colour, human label, draw category).
# category: 'copper' | 'outline' | 'silk' | 'keepout'
LAYER_STYLE = {
    1:  ("#d83b32", "Top copper",    "copper"),
    16: ("#2f6fd0", "Bottom copper", "copper"),
    20: ("#f4d03f", "Board outline", "outline"),
    46: ("#f4d03f", "Milling",       "outline"),
    21: ("#e8e8e8", "Top silk",      "silk"),
    22: ("#9aa0a6", "Bottom silk",   "silk"),
    25: ("#e8e8e8", "Top names",     "silk"),
    26: ("#9aa0a6", "Bottom names",  "silk"),
    27: ("#cfcfcf", "Top values",    "silk"),
    51: ("#8a8f94", "Top doc",       "silk"),
    52: ("#71767a", "Bottom doc",    "silk"),
    39: ("#3aa76d", "Top keepout",   "keepout"),
    40: ("#3aa76d", "Bottom keepout","keepout"),
    41: ("#7d5fb2", "Top restrict",  "keepout"),
    42: ("#7d5fb2", "Bottom restrict","keepout"),
    43: ("#7d5fb2", "Via restrict",  "keepout"),
}
# inner signal copper (Route2..Route15) -> shared style, distinct colour
INNER_COPPER = {n: (f"#9b59b6", f"Route{n}", "copper") for n in range(2, 16)}
for n, v in INNER_COPPER.items():
    LAYER_STYLE.setdefault(n, v)

PAD_COLOR  = "#e0a020"   # THT / via pads
SMD_TOP    = "#d83b32"
SMD_BOT    = "#2f6fd0"
HOLE_COLOR = "#1b1b1b"
DRILL_COLOR= "#0a0a0a"
BG_COLOR   = "#101418"
OUTLINE_COLOR = "#f4d03f"

# Which layers count as the board edge (for outline extraction / fallback bbox).
OUTLINE_LAYERS = {20, 46}


def f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- geometry math
def rot_parse(rot):
    """EAGLE rot string 'R90' / 'MR90' / 'SR0' -> (degrees, mirror_bool)."""
    if not rot:
        return 0.0, False
    mirror = "M" in rot
    digits = "".join(ch for ch in rot if ch.isdigit() or ch == ".")
    deg = f(digits, 0.0)
    return deg, mirror


def xform(deg, mirror):
    """Return a function mapping local (lx,ly) -> rotated/mirrored (x,y).
    Mirror flips about the local Y axis (EAGLE mirrors element to the bottom side)."""
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    sx = -1.0 if mirror else 1.0
    def tf(lx, ly):
        lx = lx * sx
        return (lx * ca - ly * sa, lx * sa + ly * ca)
    return tf


def arc_to_path(x1, y1, x2, y2, curve):
    """SVG arc segment for an EAGLE curved wire (curve = included angle in degrees,
    sign = direction). Returns an 'A ...' path fragment ending at (x2,y2)."""
    ang = math.radians(abs(curve))
    if ang < 1e-6:
        return f"L {x2:.4f} {y2:.4f}"
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-9:
        return f"L {x2:.4f} {y2:.4f}"
    r = (chord / 2.0) / math.sin(ang / 2.0)
    large = 1 if abs(curve) > 180 else 0
    # EAGLE positive curve = CCW; SVG sweep 1 = CW in screen coords. Y is flipped at
    # emit time, so positive EAGLE curve maps to sweep=1 here.
    sweep = 1 if curve > 0 else 0
    return f"A {r:.4f} {r:.4f} 0 {large} {sweep} {x2:.4f} {y2:.4f}"


# ---------------------------------------------------------------- parsing
class Board:
    def __init__(self, path):
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        self.board = self.root.find(".//board")
        if self.board is None:
            raise SystemExit("no <board> element found -- is this a .brd file?")
        self.packages = self._index_packages()

    def _index_packages(self):
        """(library, package) -> package element."""
        idx = {}
        libs = self.board.find("libraries")
        if libs is None:
            return idx
        for lib in libs.findall("library"):
            lname = lib.get("name")
            pkgs = lib.find("packages")
            if pkgs is None:
                continue
            for pk in pkgs.findall("package"):
                idx[(lname, pk.get("name"))] = pk
        return idx


# ---------------------------------------------------------------- SVG primitives
class SVG:
    def __init__(self):
        self.parts = []
        self.counts = {}

    def add(self, frag, kind):
        self.parts.append(frag)
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def line(self, x1, y1, x2, y2, color, width, kind, curve=None):
        w = max(width, 0.06)
        if curve:
            d = f"M {x1:.4f} {y1:.4f} " + arc_to_path(x1, y1, x2, y2, curve)
            self.add(f'<path d="{d}" stroke="{color}" stroke-width="{w:.3f}" '
                     f'fill="none" stroke-linecap="round"/>', kind)
        else:
            self.add(f'<line x1="{x1:.4f}" y1="{y1:.4f}" x2="{x2:.4f}" y2="{y2:.4f}" '
                     f'stroke="{color}" stroke-width="{w:.3f}" '
                     f'stroke-linecap="round"/>', kind)

    def circle(self, cx, cy, r, color, kind, stroke=None, sw=0.0):
        s = f' stroke="{stroke}" stroke-width="{sw:.3f}"' if stroke else ""
        fill = color if color else "none"
        self.add(f'<circle cx="{cx:.4f}" cy="{cy:.4f}" r="{r:.4f}" '
                 f'fill="{fill}"{s}/>', kind)

    def ring(self, cx, cy, r, color, width, kind):
        self.add(f'<circle cx="{cx:.4f}" cy="{cy:.4f}" r="{r:.4f}" fill="none" '
                 f'stroke="{color}" stroke-width="{width:.3f}"/>', kind)

    def rect(self, cx, cy, w, h, deg, color, kind, opacity=1.0, rx=0.0):
        tf = f' transform="rotate({-deg:.3f} {cx:.4f} {cy:.4f})"' if deg else ""
        op = f' fill-opacity="{opacity}"' if opacity != 1.0 else ""
        r = f' rx="{rx:.3f}"' if rx else ""
        self.add(f'<rect x="{cx-w/2:.4f}" y="{cy-h/2:.4f}" width="{w:.4f}" '
                 f'height="{h:.4f}"{r} fill="{color}"{op}{tf}/>', kind)

    def polygon(self, pts, color, kind, opacity=0.25, stroke=None):
        s = "".join(f"{x:.4f},{y:.4f} " for x, y in pts)
        st = f' stroke="{stroke}" stroke-width="0.1"' if stroke else ""
        self.add(f'<polygon points="{s.strip()}" fill="{color}" '
                 f'fill-opacity="{opacity}"{st}/>', kind)


# ---------------------------------------------------------------- rendering
def style_for(layer):
    return LAYER_STYLE.get(layer)


def render(board: Board, svg: SVG, flipY):
    """Walk plain + signals + element footprints, emitting SVG primitives.
    All y-coordinates pass through flipY so the board reads upright."""
    b = board.board

    def W(x1, y1, x2, y2, color, width, kind, curve=None):
        svg.line(x1, flipY(y1), x2, flipY(y2), color, width, kind, curve=curve)

    # ---- board-level <plain>: outline wires, holes, free text-as-marks ----
    plain = b.find("plain")
    if plain is not None:
        _emit_wires(plain, W)
        for h in plain.findall("hole"):
            x, y, d = f(h.get("x")), f(h.get("y")), f(h.get("drill"))
            svg.circle(x, flipY(y), d / 2 + 0.25, HOLE_COLOR, "hole")
            svg.circle(x, flipY(y), d / 2, DRILL_COLOR, "drill")
        for c in plain.findall("circle"):
            _emit_circle(c, svg, flipY)
        for r in plain.findall("rectangle"):
            _emit_rectangle(r, svg, flipY)
        for pg in plain.findall("polygon"):
            _emit_polygon(pg, svg, flipY)

    # ---- signals: routed copper wires, vias, copper pours ----
    sigs = b.find("signals")
    if sigs is not None:
        for s in sigs.findall("signal"):
            _emit_wires(s, W)
            for pg in s.findall("polygon"):
                _emit_polygon(pg, svg, flipY)
            for v in s.findall("via"):
                x, y = f(v.get("x")), f(v.get("y"))
                dia = f(v.get("diameter")) or (f(v.get("drill")) * 1.6)
                drill = f(v.get("drill"))
                svg.circle(x, flipY(y), dia / 2, PAD_COLOR, "via")
                if drill:
                    svg.circle(x, flipY(y), drill / 2, DRILL_COLOR, "via_drill")

    # ---- placed elements: stamp each footprint at its position ----
    els = b.find("elements")
    if els is not None:
        for el in els.findall("element"):
            _emit_element(el, board, svg, flipY)


def _emit_wires(parent, W):
    """Emit every <wire> child whose layer has a known style (copper/outline/silk)."""
    for w in parent.findall("wire"):
        layer = int(f(w.get("layer"), -1))
        st = style_for(layer)
        if st is None:
            continue
        color, _, cat = st
        x1, y1 = f(w.get("x1")), f(w.get("y1"))
        x2, y2 = f(w.get("x2")), f(w.get("y2"))
        width = f(w.get("width"), 0.15)
        curve = w.get("curve")
        cv = f(curve) if curve else None
        kind = cat
        W(x1, y1, x2, y2, color, width, kind, curve=cv)


def _emit_circle(c, svg, flipY):
    layer = int(f(c.get("layer"), -1))
    st = style_for(layer)
    if st is None:
        return
    color, _, _ = st
    x, y, r = f(c.get("x")), f(c.get("y")), f(c.get("radius"))
    width = f(c.get("width"), 0.15)
    svg.ring(x, flipY(y), r, color, max(width, 0.06), "silk")


def _emit_rectangle(r, svg, flipY):
    layer = int(f(r.get("layer"), -1))
    st = style_for(layer)
    if st is None:
        return
    color, _, _ = st
    x1, y1 = f(r.get("x1")), f(r.get("y1"))
    x2, y2 = f(r.get("x2")), f(r.get("y2"))
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    svg.rect(cx, flipY(cy), abs(x2 - x1), abs(y2 - y1), 0, color, "copper")


def _emit_polygon(pg, svg, flipY):
    layer = int(f(pg.get("layer"), -1))
    st = style_for(layer)
    if st is None:
        return
    color, _, cat = st
    pts = [(f(v.get("x")), flipY(f(v.get("y")))) for v in pg.findall("vertex")]
    if len(pts) < 3:
        return
    op = 0.22 if cat == "copper" else 0.12
    svg.polygon(pts, color, "pour", opacity=op,
                stroke=color if cat == "keepout" else None)


def _emit_element(el, board, svg, flipY):
    """Stamp a placed footprint: SMD pads, THT pads, package silk/keepout, drills."""
    ex, ey = f(el.get("x")), f(el.get("y"))
    deg, mirror = rot_parse(el.get("rot"))
    tf = xform(deg, mirror)
    pk = board.packages.get((el.get("library"), el.get("package")))
    if pk is None:
        return

    def place(lx, ly):
        rx, ry = tf(lx, ly)
        return ex + rx, ey + ry

    # SMD pads
    for sm in pk.findall("smd"):
        lx, ly = f(sm.get("x")), f(sm.get("y"))
        dx, dy = f(sm.get("dx")), f(sm.get("dy"))
        srot = f(sm.get("rot")[1:]) if sm.get("rot") else 0.0
        px, py = place(lx, ly)
        layer = int(f(sm.get("layer"), 1))
        # mirror sends a top SMD to the bottom layer
        if mirror:
            layer = 16 if layer == 1 else 1
        color = SMD_BOT if layer == 16 else SMD_TOP
        total_rot = deg + (-srot if mirror else srot)
        rx = min(dx, dy) * f(sm.get("roundness"), 0.0) / 100.0
        svg.rect(px, flipY(py), dx, dy, total_rot, color, "smd", rx=rx)

    # THT pads (drilled, copper both sides)
    for pd in pk.findall("pad"):
        lx, ly = f(pd.get("x")), f(pd.get("y"))
        drill = f(pd.get("drill"))
        dia = f(pd.get("diameter")) or max(drill * 1.8, drill + 0.5)
        px, py = place(lx, ly)
        shape = pd.get("shape")
        if shape == "square":
            svg.rect(px, flipY(py), dia, dia, deg, PAD_COLOR, "pad")
        elif shape == "octagon":
            svg.rect(px, flipY(py), dia, dia, deg, PAD_COLOR, "pad", rx=dia * 0.2)
        else:
            svg.circle(px, flipY(py), dia / 2, PAD_COLOR, "pad")
        if drill:
            svg.circle(px, flipY(py), drill / 2, DRILL_COLOR, "pad_drill")

    # package holes (NPTH)
    for h in pk.findall("hole"):
        lx, ly = f(h.get("x")), f(h.get("y"))
        d = f(h.get("drill"))
        px, py = place(lx, ly)
        svg.circle(px, flipY(py), d / 2 + 0.2, HOLE_COLOR, "hole")
        svg.circle(px, flipY(py), d / 2, DRILL_COLOR, "drill")

    # package silk / outline wires
    for w in pk.findall("wire"):
        layer = int(f(w.get("layer"), -1))
        if mirror:
            layer = _mirror_layer(layer)
        st = style_for(layer)
        if st is None:
            continue
        color, _, cat = st
        x1, y1 = place(f(w.get("x1")), f(w.get("y1")))
        x2, y2 = place(f(w.get("x2")), f(w.get("y2")))
        width = f(w.get("width"), 0.12)
        cv = f(w.get("curve")) if w.get("curve") else None
        # element-local curve sign flips under mirror
        if cv is not None and mirror:
            cv = -cv
        svg.line(x1, flipY(y1), x2, flipY(y2), color, width, cat, curve=cv)

    # package circles (silk/keepout)
    for c in pk.findall("circle"):
        layer = int(f(c.get("layer"), -1))
        if mirror:
            layer = _mirror_layer(layer)
        st = style_for(layer)
        if st is None:
            continue
        color, _, _ = st
        px, py = place(f(c.get("x")), f(c.get("y")))
        svg.ring(px, flipY(py), f(c.get("radius")), color,
                 max(f(c.get("width"), 0.1), 0.06), "silk")

    # package rectangles (e.g. tDocu bodies, keepout fills)
    for r in pk.findall("rectangle"):
        layer = int(f(r.get("layer"), -1))
        if mirror:
            layer = _mirror_layer(layer)
        st = style_for(layer)
        if st is None:
            continue
        color, _, cat = st
        x1, y1 = f(r.get("x1")), f(r.get("y1"))
        x2, y2 = f(r.get("x2")), f(r.get("y2"))
        c1 = place(x1, y1); c2 = place(x2, y2)
        cx, cy = (c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2
        svg.rect(cx, flipY(cy), abs(x2 - x1), abs(y2 - y1), deg, color,
                 "silk", opacity=0.4 if cat != "copper" else 1.0)


def _mirror_layer(layer):
    """Top<->bottom layer pairs flip when an element is mirrored to the bottom side."""
    pairs = {1: 16, 16: 1, 21: 22, 22: 21, 25: 26, 26: 25, 27: 28, 28: 27,
             39: 40, 40: 39, 41: 42, 51: 52, 52: 51}
    return pairs.get(layer, layer)


# ---------------------------------------------------------------- extents
def compute_extent(board: Board):
    """Board bbox in mm. Prefer the dimension/milling outline; fall back to every
    drawable coordinate (pads, wires, vias) if no outline is present."""
    ox = []
    oy = []
    allx = []
    ally = []

    def acc(x, y, outline=False):
        if x is None or y is None:
            return
        allx.append(x); ally.append(y)
        if outline:
            ox.append(x); oy.append(y)

    b = board.board
    plain = b.find("plain")
    if plain is not None:
        for w in plain.findall("wire"):
            outline = int(f(w.get("layer"), -1)) in OUTLINE_LAYERS
            acc(f(w.get("x1")), f(w.get("y1")), outline)
            acc(f(w.get("x2")), f(w.get("y2")), outline)
        for h in plain.findall("hole"):
            acc(f(h.get("x")), f(h.get("y")))

    sigs = b.find("signals")
    if sigs is not None:
        for s in sigs.findall("signal"):
            for w in s.findall("wire"):
                acc(f(w.get("x1")), f(w.get("y1")))
                acc(f(w.get("x2")), f(w.get("y2")))
            for v in s.findall("via"):
                acc(f(v.get("x")), f(v.get("y")))
            for pg in s.findall("polygon"):
                for vv in pg.findall("vertex"):
                    acc(f(vv.get("x")), f(vv.get("y")))

    els = b.find("elements")
    if els is not None:
        for el in els.findall("element"):
            ex, ey = f(el.get("x")), f(el.get("y"))
            deg, mirror = rot_parse(el.get("rot"))
            tf = xform(deg, mirror)
            pk = board.packages.get((el.get("library"), el.get("package")))
            if pk is None:
                acc(ex, ey)
                continue
            for tag in ("smd", "pad", "hole", "circle"):
                for e in pk.findall(tag):
                    rx, ry = tf(f(e.get("x")), f(e.get("y")))
                    acc(ex + rx, ey + ry)
            for w in pk.findall("wire"):
                for sx, sy in ((f(w.get("x1")), f(w.get("y1"))),
                               (f(w.get("x2")), f(w.get("y2")))):
                    rx, ry = tf(sx, sy)
                    acc(ex + rx, ey + ry)

    if ox and oy:
        return min(ox), min(oy), max(ox), max(oy), True
    if allx and ally:
        return min(allx), min(ally), max(allx), max(ally), False
    return 0.0, 0.0, 10.0, 10.0, False


# ---------------------------------------------------------------- legend
def legend_svg(used_layers, x, y):
    """Coloured-swatch legend for the layers actually drawn, plus pads/vias/holes."""
    rows = []
    items = []
    seen = set()
    for ln in sorted(used_layers):
        st = LAYER_STYLE.get(ln)
        if not st:
            continue
        color, label, _ = st
        key = (color, label)
        if key in seen:
            continue
        seen.add(key)
        items.append((color, f"{label} (L{ln})"))
    items += [(PAD_COLOR, "THT pad / via"), (SMD_TOP, "SMD top"),
              (SMD_BOT, "SMD bottom"), (DRILL_COLOR, "drill")]
    dy = 4.2
    for i, (color, label) in enumerate(items):
        yy = y + i * dy
        rows.append(f'<rect x="{x:.2f}" y="{yy:.2f}" width="3" height="3" '
                    f'fill="{color}"/>')
        rows.append(f'<text x="{x+4.2:.2f}" y="{yy+2.6:.2f}" '
                    f'font-size="2.6" fill="#d6dce2" '
                    f'font-family="monospace">{label}</text>')
    return "\n".join(rows), len(items) * dy


# ---------------------------------------------------------------- main emit
def build_svg(board: Board):
    minx, miny, maxx, maxy, had_outline = compute_extent(board)
    pad = 2.0
    bw = (maxx - minx) + 2 * pad
    bh = (maxy - miny) + 2 * pad

    # legend sits to the right of the board
    legend_w = 34.0

    # flipY maps board-y -> svg-y within the padded board area
    def flipY(y):
        return (maxy - y) + pad

    svg = SVG()
    render(board, svg, flipY)

    used_layers = set()
    b = board.board
    for w in b.iter("wire"):
        used_layers.add(int(f(w.get("layer"), -1)))
    for pg in b.iter("polygon"):
        used_layers.add(int(f(pg.get("layer"), -1)))
    used_layers = {l for l in used_layers if l in LAYER_STYLE}

    leg, leg_h = legend_svg(used_layers, bw + 2.0, pad + 2.0)

    total_w = bw + legend_w
    total_h = max(bh, leg_h + pad + 6)

    # translate board coords so minx maps to pad
    body = "".join(svg.parts)
    drawing = (f'<g transform="translate({pad - minx:.4f},0)">\n{body}\n</g>')

    header = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {total_w:.3f} {total_h:.3f}" '
        f'width="{total_w*4:.0f}" height="{total_h*4:.0f}" '
        f'font-family="monospace">\n'
        f'<rect x="0" y="0" width="{total_w:.3f}" height="{total_h:.3f}" '
        f'fill="{BG_COLOR}"/>\n'
    )
    title = (f'<text x="{bw+2.0:.2f}" y="{pad:.2f}" font-size="3" fill="#f4d03f">'
             f'{os.path.basename(board_path_global)}</text>\n')
    footer = f'\n{leg}\n</svg>\n'
    out = header + title + drawing + footer
    return out, (minx, miny, maxx, maxy, had_outline), svg.counts


board_path_global = ""


def main():
    global board_path_global
    ap = argparse.ArgumentParser(description="Render an EAGLE/Fusion .brd to SVG.")
    ap.add_argument("brd", help="path to the .brd file")
    ap.add_argument("-o", "--output", help="output .svg path (default: <brd>.svg)")
    ap.add_argument("--text", action="store_true", help="print a human summary")
    args = ap.parse_args()

    if not os.path.isfile(args.brd):
        raise SystemExit(f"not found: {args.brd}")
    board_path_global = args.brd

    out_path = args.output or (os.path.splitext(args.brd)[0] + ".svg")

    board = Board(args.brd)
    svg_text, extent, counts = build_svg(board)

    with open(out_path, "w") as fh:
        fh.write(svg_text)

    minx, miny, maxx, maxy, had_outline = extent
    total = sum(counts.values())
    print(f"wrote {out_path}")
    print(f"board extent: {maxx-minx:.2f} x {maxy-miny:.2f} mm "
          f"({'outline' if had_outline else 'bbox-fallback'})")
    print(f"drawn elements: {total}")
    if args.text:
        for k in sorted(counts):
            print(f"  {k:12s} {counts[k]}")


if __name__ == "__main__":
    main()
