# Gerber (RS-274X) & Excellon Parsing — Fusion Electronics output

Reference for `eagle-pcb-studio`. Covers the RS-274X (Gerber X1) format that
Autodesk Fusion Electronics / EAGLE 9.x writes, the Excellon drill format, how
Fusion names its CAM layers (and how that differs from KiCad), and the recipes for
two checks the skill performs: **solder-mask DAM (sliver) width** measurement and
**round-drill / slot overlap** detection.

Grounded in the real CAM output in this repo:
`path/to/your-board/CAMOutputs/`
(`GerberFiles/*.gbr`, `DrillFiles/drill_1_16.xln`, `gerber_job.gbrjob`).

## Table of contents
- [Fusion CAM layer naming (vs KiCad)](#fusion-cam-layer-naming-vs-kicad)
- [RS-274X structure](#rs-274x-structure)
  - [Header: format & units](#header-format--units)
  - [Apertures (`%ADD`)](#apertures-add)
  - [Aperture macros (`%AM`)](#aperture-macros-am)
  - [Draw / move / flash (D01/D02/D03)](#draw--move--flash-d01d02d03)
  - [Regions (G36/G37) and polarity (LPD/LPC)](#regions-g36g37-and-polarity-lpdlpc)
  - [Coordinate decoding](#coordinate-decoding)
- [Excellon drill format](#excellon-drill-format)
- [The `.gbrjob` job file](#the-gbrjob-job-file)
- [Recipe: measure solder-mask DAM widths](#recipe-measure-solder-mask-dam-widths)
- [Recipe: detect round-drill / slot overlaps](#recipe-detect-round-drill--slot-overlaps)
- [Minimal parser skeleton](#minimal-parser-skeleton)

---

## Fusion CAM layer naming (vs KiCad)

Fusion writes **one file per layer with a descriptive base name and a `.gbr`
extension** (drills are `.xln`). The set seen for a 4-layer board:

| file | layer |
|------|-------|
| `copper_top_l1.gbr` | top copper (stack layer 1) |
| `copper_inner_l2.gbr` | inner copper (stack layer 2) |
| `copper_inner_l3.gbr` | inner copper (stack layer 3) |
| `copper_bottom_l4.gbr` | bottom copper (stack layer 4) |
| `soldermask_top.gbr` / `soldermask_bottom.gbr` | solder-mask openings (EAGLE tStop/bStop) |
| `solderpaste_top.gbr` / `solderpaste_bottom.gbr` | paste stencil (tCream/bCream) |
| `silkscreen_top.gbr` / `silkscreen_bottom.gbr` | silkscreen |
| `profile.gbr` | board outline / route (EAGLE Dimension, layer 20) |
| `DrillFiles/drill_1_16.xln` | Excellon drills spanning layers 1→16 |
| `gerber_job.gbrjob` | JSON job/metadata |

**Key contrast with KiCad:** Fusion's Gerbers are plain **X1** — they carry **no
Gerber X2 attributes**. There are no `%TF.FileFunction`, `%TF.FilePolarity`,
`%TA`/`%TO`/`%TD` object/aperture attributes that KiCad emits. So a parser **cannot**
read the layer role from the file body; it must infer the role **from the filename**.
The only metadata is the `%IN<name>*%` image-name comment (e.g. `%INTop Copper*%`,
`%INSoldermask Top*%`) and the JSON `.gbrjob`. KiCad files, by contrast, are
self-describing via X2 and use names like `*-F_Cu.gbr`, `*-F_Mask.gbr`,
`*-Edge_Cuts.gbr`, `*.drl`. Build the layer map off the Fusion base names above.

Header comment in every file: `G04 EAGLE Gerber RS-274X export*` and the job file
identifies `"Application": "Fusion Electronics", "Vendor": "Autodesk"`.

## RS-274X structure

### Header: format & units

Real Fusion header (identical across all `.gbr` here):

```gerber
G04 EAGLE Gerber RS-274X export*
G75*                  ← multi-quadrant circular interpolation enabled
%MOMM*%               ← units = millimetres (%MOIN*% would be inches)
%FSLAX34Y34*%         ← format spec, see below
%LPD*%                ← layer polarity Dark (additive) — default
%IN<image name>*%     ← image/layer name comment
%IPPOS*%              ← image positive
%AMOC8* ... *%        ← (an octagon aperture macro, defined even if unused)
G01*                  ← linear interpolation mode
```

`%FSLAXaYb*%` decoded as `%FS` `L` `A` `X`a `Y`b:
- `L` = leading zeros omitted (trailing zeros present). (`T` = trailing omitted.)
- `A` = absolute coordinates. (`I` = incremental.)
- `X34Y34` = **3 integer digits + 4 decimal digits** per X and Y. With `%MOMM`,
  one integer unit = 1 mm, so the value `X288550` = `28.8550 mm` (divide by 10^4).

So the decode constant is `scale = 10**4` for `34` format, `MM`.

### Apertures (`%ADD`)

`%ADD<dcode><template>,<params>*%` defines a tool. Fusion uses three primitives:

| template | shape | params | example |
|----------|-------|--------|---------|
| `C` | circle | diameter[,hole] | `%ADD10C,3.003200*%` |
| `R` | rectangle | X×Y[,hole] | `%ADD11R,1.653200X1.203200*%` |
| `O` | obround (stadium) | X×Y[,hole] | `%ADD12O,2.0X1.0*%` |

D-codes start at `D10`. (`O` did not appear in these files — every pad here is `C`
or `R` — but obrounds are standard for oval pads/slots; handle them.) A trailing
`,hole` (e.g. `R,1.0X1.0X0.5`) means a circular hole drilled through the flash.

The mask/paste apertures are the copper pad sizes ± the mask/cream frame. E.g.
copper top `%ADD10R,1.450000X1.000000` vs soldermask top
`%ADD11R,1.653200X1.203200` — the mask aperture is ~0.10 mm larger per side,
i.e. the `mlMinStopFrame=4mil`≈0.1016 mm expansion from the design rules.

### Aperture macros (`%AM`)

`%AM<name>*<primitive lines>*%` defines a parametric shape used by `%ADD<d><name>,<args>`.
Fusion always emits `%AMOC8*` (an octagon, primitive code `5` = regular polygon:
`5,exposure,vertices,cx,cy,diameter,rotation`) for octagonal pads. If a board has no
octagon pads it is defined but never selected. A robust parser stores macro bodies
and only needs to evaluate them if a `%ADD` references one.

### Draw / move / flash (D01/D02/D03)

After `Dnn*` selects the current aperture, coordinate blocks end in an operation:

| op | meaning |
|----|---------|
| `D01*` | **draw**: interpolate (line/arc) from current point to (X,Y) with the current aperture → a track/trace |
| `D02*` | **move**: reposition current point, pen up (no copper) |
| `D03*` | **flash**: stamp the current aperture once at (X,Y) → a pad/via |

Examples from `copper_top_l1.gbr`:

```gerber
D15*                       ← select aperture 15
X414500Y265500D03*         ← flash a pad at (41.45, 26.55) mm
```

Tracks are runs of `D02` (move to start) then `D01` (draw to each vertex). With
`G75` active, an arc uses `I`/`J` offset words on the `D01` line.

### Regions (G36/G37) and polarity (LPD/LPC)

`G36*` … `G37*` bound a **filled region** (polygon): the coordinate path between
them is the outline; the enclosed area is filled solid (copper pour, plane, or a
mask/paste opening). The path uses `D02`/`D01` to trace the boundary, no aperture
needed for the fill.

```gerber
G36*
X288509Y159961D02*       ← start of boundary
X288550Y159755D01*       ← trace edges...
...
X288509Y159961D01*       ← back to start
G37*                     ← close & fill region
```

Polarity:
- `%LPD*%` = **Dark** = additive (this is the default; everything is Dark unless changed).
- `%LPC*%` = **Clear** = subtractive — a region/flash drawn in Clear *removes* copper
  from what's underneath. **Inner copper planes use this**: `copper_inner_l2.gbr`
  and `copper_inner_l3.gbr` contain `%LPC*%` to cut anti-pads/clearances out of the
  solid plane. Silkscreen may use `%LPC` for knockout text.

**Fusion mask reality (important for the DAM recipe):** `soldermask_top.gbr` mixes
**both** representations — 224 `D03` flashes (one per simple pad opening) **and** 33
`G36/G37` regions (for larger/merged openings, e.g. connector shells). A mask parser
must collect opening geometry from **both** flashes (aperture bbox at the flash XY)
and regions (polygon bbox). Inner planes are even more region-heavy (107 regions +
143 flashes in `copper_inner_l2.gbr`).

### Coordinate decoding

```python
def decode(num, int_digits, dec_digits, omit="L"):
    s = num  # e.g. "288550" or "-12.5"
    neg = s.startswith("-"); s = s.lstrip("+-")
    if "." in s:                      # explicit decimal: take as-is
        v = float(s)
    else:                             # implied decimal per FS
        total = int_digits + dec_digits
        if omit == "L":               # leading omitted -> pad left
            s = s.zfill(total)
        else:                         # trailing omitted -> pad right
            s = s.ljust(total, "0")
        v = int(s) / (10 ** dec_digits)
    return -v if neg else v
```

For Fusion's `%FSLAX34Y34` + `%MOMM`: `v_mm = int(token) / 10000.0`. Coordinates may
be modal (a missing X reuses the previous X) — track the current point.

## Excellon drill format

`DrillFiles/drill_1_16.xln` (NC drill). Structure:

```
M48                                  ← header start
;GenerationSoftware,Autodesk,EAGLE,9.7.0*%
FMAT,2                               ← format 2
ICI,OFF                              ← incremental input OFF (absolute)
METRIC,TZ,000.000                    ← units=mm, TZ=trailing zeros kept, 3.3 fmt
T8C0.200                             ← tool 8 = 0.200 mm diameter
T7C0.250
...
T1C2.800
%                                    ← header end
G90                                  ← absolute mode
M71                                  ← metric
T1                                   ← select tool 1
X49800Y4200                          ← hit (drill) at this XY
X4200Y46800
...
T2
X9300Y2100
...
M30                                  ← end of program
```

Decoding hits: with `METRIC,TZ,000.000` (3 integer + 3 decimal, trailing zeros
kept), `X49800` = `49.800 mm`, `Y4200` = `4.200 mm` → divide by 1000. (If the header
said `LZ`, leading zeros would be kept instead — pad/scale accordingly. Some Fusion
exports use a `0.0000`/4-decimal METRIC line; read the digit count from the
`METRIC,..,iii.ddd` mask, don't assume.)

Tool table: each `T<n>C<dia>` defines tool n's diameter in mm. A `T<n>` on its own
line *selects* that tool; subsequent XY lines are hits with it. Map every hit to its
tool's diameter for overlap checks.

**Slots:** a routed slot is drilled as a move + route. Two encodings appear in
Excellon:
- **G85 slot**: `X..Y..G85X..Y..` — drill a slot from the first XY to the second
  (oval slot, width = tool diameter).
- **Route mode**: `G00X..Y..` (rapid to start) then `M15` (tool down) … `G01`
  segments … `M16` (tool up) — a milled slot path. `G05`/`M15`/`M16` may appear.

This particular file is **all round hits, no slots** (no `G85`/`M15`). A general
parser must still recognise both slot encodings and treat a slot as a capsule
(stadium) of the tool diameter swept between endpoints.

## The `.gbrjob` job file

`gerber_job.gbrjob` is JSON metadata (not geometry). Useful fields:

```json
{
  "Header": { "GenerationSoftware": { "Application": "Fusion Electronics",
              "Vendor": "Autodesk", "Version": "9.7.0" } },
  "Overall": { "BoardThickness": 1.99, "LayerNumber": 4,
               "Size": { "X": 54, "Y": 51 } }
}
```

Use it to sanity-check layer count and board size, and to confirm the toolchain.
(Standard `.gbrjob` also lists `FilesAttributes` with per-file roles; Fusion's is
minimal — fall back to filename inference.)

## Recipe: measure solder-mask DAM widths

A mask **DAM** (a.k.a. sliver/web) is the strip of remaining solder mask *between*
two adjacent openings. Fabs enforce a minimum (commonly 0.08–0.10 mm); thinner dams
flake off and cause bridging. To measure:

1. Parse `soldermask_top.gbr` / `soldermask_bottom.gbr`. Collect every opening as an
   axis-aligned bbox:
   - **Flash** (`D03`): bbox = aperture extent centred at the decoded XY. For `R`:
     `(x−X/2, y−Y/2, x+X/2, y+Y/2)`. For `C`: half-diameter square (or treat as
     circle). For `O`: the obround's bounding rect.
   - **Region** (`G36/G37`): bbox = min/max of the traced vertices.
   *(Both must be collected — Fusion uses flashes AND regions in the same mask file.)*
2. For each pair of openings whose bboxes are near (within a small search radius),
   compute the **gap** between them = the mask dam width:
   - If the bboxes overlap in Y, the horizontal dam = `max(0, right.x0 − left.x1)`.
   - If they overlap in X, the vertical dam = `max(0, top.y0 − bottom.y1)`.
   - Ignore pairs that overlap (gap ≤ 0 → merged opening, no dam).
3. Report any positive gap below the threshold (e.g. `< 0.10 mm`), with the two
   opening locations, so the user can widen mask or move pads.

bbox-gap is a fast conservative proxy; for diagonal neighbours use the true
edge-to-edge polygon distance if a tighter result is needed.

## Recipe: detect round-drill / slot overlaps

Drills/slots that overlap (or sit too close) break out into one ragged hole and fail
fab. From the Excellon file:

1. Parse the tool table → `{tool: diameter}`. Walk the body tracking the selected
   tool; collect each hit as a **circle** `(x, y, r=dia/2)` and each slot as a
   **capsule** (segment p0→p1 with radius `dia/2`).
2. Pairwise (or via a grid/bbox prefilter for speed):
   - circle–circle: overlap if `dist(c1,c2) < r1 + r2`. Add a `min_wall` margin to
     flag near-misses (`< r1 + r2 + min_wall`).
   - circle–slot / slot–slot: use point-to-segment / segment-to-segment distance
     against the summed radii.
3. Also cross-check **slot vs round drill of a different tool at the same nominal
   location** — a common Fusion artefact where a plated slot and its seed round drill
   are both emitted; coincident centres with `dist ≈ 0` are the signal.
4. Report each overlapping/too-close pair with coordinates, tool diameters, and the
   measured centre distance.

## Minimal parser skeleton

```python
import re

def parse_gerber(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    # format
    m = re.search(r"%FSLAX(\d)(\d)Y(\d)(\d)\*%", raw)
    xi, xd = int(m.group(1)), int(m.group(2))
    mm = "%MOMM*%" in raw
    scale = 10 ** xd
    # apertures
    aps = {}
    for d, tmpl, params in re.findall(r"%ADD(\d+)([CROP]),([0-9.X]+)\*%", raw):
        dims = [float(v) for v in params.split("X")]
        aps[int(d)] = (tmpl, dims)
    # walk body
    cur_ap = None; x = y = 0.0
    flashes, draws, regions = [], [], []
    in_region = False; region_pts = []
    for line in raw.splitlines():
        line = line.strip()
        if line == "G36*": in_region, region_pts = True, []; continue
        if line == "G37*": regions.append(region_pts); in_region = False; continue
        ds = re.match(r"^D(\d+)\*$", line)
        if ds: cur_ap = int(ds.group(1)); continue
        cm = re.match(r"^(?:X(-?\d+))?(?:Y(-?\d+))?D0([123])\*$", line)
        if not cm: continue
        if cm.group(1) is not None: x = int(cm.group(1)) / scale
        if cm.group(2) is not None: y = int(cm.group(2)) / scale
        op = cm.group(3)
        if in_region: region_pts.append((x, y))
        elif op == "3": flashes.append((cur_ap, x, y))
        elif op == "1": draws.append((cur_ap, x, y))
    return dict(scale=scale, mm=mm, aps=aps,
                flashes=flashes, draws=draws, regions=regions)

def parse_excellon(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    hdr = re.search(r"METRIC,(TZ|LZ),(\d+)\.(\d+)", raw)
    dec = int(hdr.group(3)) if hdr else 3
    scale = 10 ** dec
    tools = {int(t): float(d) for t, d in re.findall(r"^T(\d+)C([0-9.]+)", raw, re.M)}
    hits = []; cur = None
    for line in raw.splitlines():
        sel = re.match(r"^T(\d+)$", line.strip())
        if sel: cur = int(sel.group(1)); continue
        h = re.match(r"^X(-?\d+)Y(-?\d+)", line.strip())
        if h and cur is not None:
            hits.append((cur, int(h.group(1))/scale, int(h.group(2))/scale))
    return dict(tools=tools, hits=hits)
```

Notes: handle modal coordinates (missing X or Y reuses the last), `%MOIN` (inch:
scale stays 10^dec but values are inches → ×25.4 for mm), `O`/macro apertures, and
the `G85`/route slot encodings before treating output as authoritative. Always quote
file paths — they contain spaces in this project.
