# EAGLE / Autodesk Fusion Electronics XML Format (.brd / .sch)

Reference for `eagle-pcb-studio`. Covers the on-disk XML structure of EAGLE 9.x /
Fusion Electronics `.brd` (board) and `.sch` (schematic) files, the layer-number
conventions, design rules, and the **non-negotiable editing rule**: parse with
ElementTree to *read*, but splice text edits into the raw file so the DOCTYPE and
formatting survive.

Grounded in the real generators and files in this repo:
- `generate_brd.py`
- `generate_sch.py`
- `your-board.brd` / `.sch`
- vendor footprints in `components/*.lbr`

## Table of contents
- [The cardinal editing rule](#the-cardinal-editing-rule)
- [File skeleton (shared by .brd and .sch)](#file-skeleton-shared-by-brd-and-sch)
- [The DOCTYPE and `<eagle version>`](#the-doctype-and-eagle-version)
- [`<drawing>`, `<settings>`, `<grid>`](#drawing-settings-grid)
- [`<layers>` and the layer-number table](#layers-and-the-layer-number-table)
- [`<libraries>` / `<library>`](#libraries--library)
  - [`<packages>` / `<package>` (footprints)](#packages--package-footprints)
  - [`<symbols>` / `<symbol>`](#symbols--symbol)
  - [`<devicesets>` / `<deviceset>` / `<device>` (pin→pad)](#devicesets--deviceset--device-pinpad)
- [Schematic body: `<schematic>`](#schematic-body-schematic)
- [Board body: `<board>`](#board-body-board)
  - [`<plain>` (outline + holes)](#plain-outline--holes)
  - [`<elements>` / `<element>`](#elements--element)
  - [`<signals>` / `<signal>` (nets, copper, planes)](#signals--signal-nets-copper-planes)
- [`<classes>` / `<class>` (net widths)](#classes--class-net-widths)
- [`<designrules>` / `<param>`](#designrules--param)
- [Pulling vendor footprints VERBATIM (`parse_vendor`)](#pulling-vendor-footprints-verbatim-parse_vendor)
- [Coordinate / unit conventions](#coordinate--unit-conventions)

---

## The cardinal editing rule

EAGLE files open with a SYSTEM DOCTYPE:

```xml
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="9.6.2">
```

`xml.etree.ElementTree` **drops the DOCTYPE on re-serialization** and reflows
whitespace/attribute order. EAGLE and Fusion both refuse or mangle a file whose
DOCTYPE is missing. Therefore:

> **Parse with ElementTree to locate/measure things. Never `ET.write()` the whole
> tree back. Apply edits as targeted string splices on the raw file text, then write
> the bytes yourself.**

Practical pattern used throughout this skill:

```python
import xml.etree.ElementTree as ET
raw = open(path, encoding="utf-8").read()
root = ET.fromstring(raw)            # READ ONLY — for finding/measuring
# ... compute an old_substr -> new_substr replacement ...
raw = raw.replace(old_substr, new_substr, 1)   # splice into raw text
open(path, "w", encoding="utf-8").write(raw)    # DOCTYPE + formatting preserved
```

When generating a file from scratch (as `generate_brd.py` / `generate_sch.py` do),
the DOCTYPE is written literally into the f-string template — never via ElementTree.
Escape attribute values with `xml.sax.saxutils.escape(s, {'"': "&quot;"})`.

---

## File skeleton (shared by .brd and .sch)

Both file types are one `<eagle>` → `<drawing>`. The drawing holds `<settings>`,
`<grid>`, `<layers>`, then exactly one of `<board>` or `<schematic>`:

```
<eagle version="9.6.2">
  <drawing>
    <settings>...</settings>
    <grid .../>
    <layers> ... </layers>
    <board> ... </board>          ← .brd
    <!-- or -->
    <schematic> ... </schematic>  ← .sch
  </drawing>
</eagle>
```

`<libraries>` lives *inside* `<board>` (boards embed a copy of every library they
use) and *inside* `<schematic>`. There is no shared external library reference in
the saved file — footprints/symbols are embedded.

## The DOCTYPE and `<eagle version>`

- `version` is the EAGLE file-format version, e.g. `9.6.2`. Fusion still writes
  EAGLE-9 format files; the *application* version (e.g. `Fusion Electronics 9.7.0`)
  only appears in CAM/Gerber job metadata, not in the `.brd`/`.sch`.
- Keep the exact `<!DOCTYPE eagle SYSTEM "eagle.dtd">` line byte-for-byte.

## `<drawing>`, `<settings>`, `<grid>`

```xml
<settings>
  <setting alwaysvectorfont="no"/>
  <setting verticaltext="up"/>
</settings>
<grid distance="0.1" unitdist="inch" unit="inch" style="lines" multiple="1"
      display="no" altdistance="0.01" altunitdist="inch" altunit="inch"/>
```

The grid is cosmetic (editor display). All geometry coordinates are in **mm**
regardless of the grid unit.

## `<layers>` and the layer-number table

Every layer used anywhere must be declared once:

```xml
<layer number="1" name="Top" color="7" fill="1" visible="yes" active="yes"/>
```

EAGLE layer numbers are fixed by convention (the names are conventional too).
The ones that matter for board/footprint generation and CAM:

| # | Name | Role |
|---|------|------|
| 1 | Top | top copper |
| 2 | Route2 | inner copper (inner layer 1 of a 4-layer stack) |
| 15 | Route15 | inner copper (inner layer 2) |
| 16 | Bottom | bottom copper |
| 17 | Pads | through-hole pad copper (all layers) |
| 18 | Vias | via copper (all layers) |
| 19 | Unrouted | airwires / ratsnest |
| 20 | Dimension | board outline / profile (milled edge) |
| 21 | tPlace | top silkscreen |
| 22 | bPlace | bottom silkscreen |
| 25 | tNames | top component-name silk (`>NAME`) |
| 27 | tValues | top value silk (`>VALUE`) |
| 29 | tStop | top solder-mask opening |
| 30 | bStop | bottom solder-mask opening |
| 31 | tCream | top solder-paste (stencil) |
| 32 | bCream | bottom solder-paste |
| 39/40 | tKeepout/bKeepout | placement keep-out |
| 41/42/43 | tRestrict/bRestrict/vRestrict | copper/via restrict (no-pour) |
| 44 | Drills | plated drills |
| 45 | Holes | non-plated holes (mounting) |
| 46 | Milling | internal slots / routed cutouts |
| 47/48/49 | Measures/Document/Reference | docs |
| 51/52 | tDocu/bDocu | assembly docs |
| 90–98 | Modules/Nets/Busses/Pins/Symbols/Names/Values/Info/Guide | schematic-only |

Schematic geometry lives on the 90s layers: symbol bodies on **94 (Symbols)**, pin
wires/labels on **91 (Nets)**, `>NAME` on **95 (Names)**, `>VALUE` on **96 (Values)**.
`generate_brd.py` declares layers 1–98 (`LAYERS` list); `generate_sch.py` merges a
`BASE_LAYERS` dict with the layers found in vendor `.lbr` files.

## `<libraries>` / `<library>`

```xml
<library name="BLLIB">
  <description>...</description>
  <packages> ... </packages>     ← board needs only packages
  <symbols> ... </symbols>       ← schematic adds symbols + devicesets
  <devicesets> ... </devicesets>
</library>
```

A `.brd` embeds only `<packages>` per library (it has no schematic symbols). A `.sch`
embeds `<packages>` + `<symbols>` + `<devicesets>`. Library `name` is the key
referenced by `<element library="...">` and `<part library="...">`.

### `<packages>` / `<package>` (footprints)

A footprint = land pattern. Children:

| tag | meaning | key attrs |
|-----|---------|-----------|
| `<smd>` | SMD pad (copper on `layer`) | `name x y dx dy layer roundness rot` (optional `stop="no"` to suppress mask) |
| `<pad>` | through-hole pad | `name x y drill diameter shape` (round/square/octagon/long) |
| `<hole>` | non-plated hole | `x y drill` |
| `<wire>` | silk / outline / keepout line | `x1 y1 x2 y2 width layer` (`curve="90"` for arcs) |
| `<circle>` | silk circle / hole ring | `x y radius width layer` |
| `<rectangle>` | filled rect | `x1 y1 x2 y2 layer` |
| `<polygon>` | copper pour / courtyard / keepout | `width layer` + child `<vertex x y>` |
| `<text>` | `>NAME` / `>VALUE` placeholder | `x y size layer align` |

Example (generated 0805 chip pad pair; see `generate_sch.py` `smd()` / `two()`):

```xml
<package name="0805">
  <text x="0" y="0" size="1.0" layer="25" align="center">&gt;NAME</text>
  <smd name="1" x="-0.950" y="0.000" dx="1.000" dy="1.450" layer="1" roundness="0" rot="R0"/>
  <smd name="2" x="0.950" y="0.000" dx="1.000" dy="1.450" layer="1" roundness="0" rot="R0"/>
</package>
```

`smd dx/dy` are full pad width/height. Under an `R90`/`R270` element rotation, dx/dy
swap when computing the footprint bbox (see `pkg_bbox()` in `generate_brd.py`).
A `<pad>` with no `diameter` defaults to ~1.5× drill.

### `<symbols>` / `<symbol>`

Schematic-only. A symbol is a box on layer 94 plus `<pin>`s:

```xml
<symbol name="RES">
  <wire ... layer="94"/> ...box...
  <text x="-10.16" y="..." size="1.778" layer="95">&gt;NAME</text>
  <text x="-10.16" y="..." size="1.778" layer="96">&gt;VALUE</text>
  <pin name="1" x="-12.70" y="0.00" visible="pin" length="point" direction="pas" rot="R0"/>
  <pin name="2" x="12.70" y="0.00" visible="pin" length="point" direction="pas" rot="R180"/>
</symbol>
```

Pin `name` is the schematic-side name; it is mapped to a physical pad in the
deviceset's `<connects>`.

### `<devicesets>` / `<deviceset>` / `<device>` (pin→pad)

The deviceset ties a symbol to one or more physical packages. The `<connects>`
block is the **pin→pad map** — the single most important table for netlist work:

```xml
<deviceset name="RES_0603" prefix="R" uservalue="yes">
  <gates><gate name="G$1" symbol="RES" x="0" y="0"/></gates>
  <devices>
    <device name="" package="0603">
      <connects>
        <connect gate="G$1" pin="1" pad="1"/>
        <connect gate="G$1" pin="2" pad="2"/>
      </connects>
      <technologies><technology name=""/></technologies>
    </device>
  </devices>
</deviceset>
```

- `prefix` = reference designator prefix (R, C, U, J, D, Q, L, F, SW).
- One `pin` may map to **multiple pads** (space-separated), e.g. the DRV8313 ground:
  `pad="14 20 28 6 7 10 12 13 19 EP"`. Generators split on whitespace.
- `<technology>` may carry `<attribute name= value=>` (vendor part numbers, etc.);
  `generate_brd.py` copies these into `<element>` attributes.

## Schematic body: `<schematic>`

```xml
<schematic xreflabel="%F%N/%S.%C%R" xrefpart="/%S.%C%R">
  <libraries> ... </libraries>
  <attributes/><variantdefs/>
  <classes> ... </classes>
  <parts>
    <part name="C_MCU1" library="BLLIB" deviceset="CAP_0805" device="" value="10u"/>
  </parts>
  <sheets>
    <sheet>
      <plain/>
      <instances>
        <instance part="C_MCU1" gate="G$1" x="0" y="0" rot="R0"/>
      </instances>
      <busses/>
      <nets>
        <net name="P3V3" class="2">
          <segment>
            <pinref part="C_MCU1" gate="G$1" pin="1"/>
            <wire x1="..." y1="..." x2="..." y2="..." width="0.1524" layer="91"/>
            <label x="..." y="..." size="1.27" layer="91"/>
          </segment>
        </net>
      </nets>
    </sheet>
  </sheets>
</schematic>
```

- `<part>` = a placed instance referencing `library`/`deviceset`/`device`(+`value`).
- `<instance>` = its position on a sheet.
- `<net>` groups `<segment>`s; each segment carries the `<pinref>` connection plus
  net wires (layer 91) and a net `<label>`. `generate_sch.py` emits one labelled
  pin-stub segment per pin and lets EAGLE merge by net name.

## Board body: `<board>`

```xml
<board>
  <plain> ...outline + holes... </plain>
  <libraries> ...embedded packages... </libraries>
  <attributes/><variantdefs/>
  <classes> ... </classes>
  <designrules name="default"/>   <!-- or full <param> list -->
  <elements> ... </elements>
  <signals> ... </signals>
</board>
```

### `<plain>` (outline + holes)

The board outline is a closed loop of `<wire layer="20">` (Dimension). Rounded
corners are quarter-arc wires with `curve="90"`:

```xml
<wire x1="3.500" y1="0.000" x2="50.500" y2="0.000" width="0.2" layer="20"/>
<wire x1="50.500" y1="0.000" x2="54.000" y2="3.500" width="0.2" layer="20" curve="90"/>
```

Mounting holes go straight in `<plain>`:

```xml
<hole x="4.200" y="4.200" drill="2.8"/>
```

(See `rounded_outline()` and the `<hole>` emit in `generate_brd.py`.)

### `<elements>` / `<element>`

A placed footprint instance on the board:

```xml
<element name="C_MCU1" library="BLLIB" package="0805" value="10u"
         x="41.450" y="27.500" rot="R90"/>
```

- `x y` = placement origin in mm; `rot` ∈ `R0/R90/R180/R270`.
- An interactively edited board may add `smashed="yes"` (name/value text moved
  out to free-standing `<attribute>` elements) — preserve it on rewrite.
- Vendor parts carry `<attribute>` children (display="off") with MPN/etc.

### `<signals>` / `<signal>` (nets, copper, planes)

A signal is the board-side net. It lists which pads belong to it via
`<contactref>`, plus any routed copper (`<wire>`), `<via>`, and pour `<polygon>`:

```xml
<signal name="GND" class="0">
  <contactref element="C_MCU1" pad="2"/>
  <contactref element="U_MCU" pad="1"/>
  <via x="23.950" y="14.275" extent="1-16" drill="0.3" diameter="0.6"/>
  <polygon width="0.2032" layer="2" spacing="1.27" pour="solid"
           isolate="0.3" thermals="yes" rank="1">
    <vertex x="0.500" y="0.500"/>
    <vertex x="53.500" y="0.500"/>
    ...
  </polygon>
</signal>
```

- `<contactref element= pad=>` is generated from the deviceset connects: for each
  part-pin's net, every space-separated pad becomes a contactref (`build_signals()`).
- `<wire layer="1|16|...">` = a routed track segment (added by router/user).
- `<via extent="1-16" drill= diameter=>` = a plated via spanning those copper
  layers. `generate_brd.py` pre-places GND thermal vias and power-escape vias so the
  autorouter can reach inaccessible thermal/power pads.
- **Plane pour** = `<polygon ... pour="solid" rank="1">` on an inner copper layer
  (2 or 15). `rank` is the pour priority (lower rank pours first / is overridden by
  higher). `isolate` = clearance to other copper; `thermals="yes"` = spoke
  connections to same-net pads; `spacing` = hatch spacing. The pour is **notched**
  around RF features (the WROOM antenna) by emitting concave vertices.

## `<classes>` / `<class>` (net widths)

Net classes set default track width / clearance / drill per net group:

```xml
<classes>
  <class number="0" name="default" width="0" drill="0"/>
  <class number="1" name="pwr24"  width="0.4" drill="0"/>
  <class number="2" name="pwr3v3" width="0.25" drill="0"/>
</classes>
```

A net opts in with `class="1"`. `generate_brd.py`/`generate_sch.py` assign power
rails (`P24*`, `VBUS`, `P3V3`) to the wide classes; signals stay class 0.
(The generated template seeds 0.5/0.4 mm; the routed board narrowed them to 0.4/0.25.)

## `<designrules>` / `<param>`

Two forms. A fresh board may just reference the named ruleset:

```xml
<designrules name="default"/>
```

A board saved by EAGLE/Fusion expands the full parameter list. The params the skill
cares about (values from `your-board.brd`):

| param | value | meaning |
|-------|-------|---------|
| `layerSetup` | `(1+2*15+16)` | copper stack: signal **1**, inner **2** & **15**, signal **16** → 4-layer |
| `mdDrill` | `0.3mm` | min drill diameter |
| `msDrill` | `0.25mm` | min annular-ring / drill spacing (signal) |
| `msWidth` | `6mil` | min track width |
| `mdWireWire`/`mdPadPad`/… | `0.152mm` | copper-to-copper clearances |
| `mdSmdPad`/`mdSmdSmd` | `6mil` | SMD clearances |
| `mlMinStopFrame` / `mlMaxStopFrame` | `4mil` | **solder-mask expansion** (mask opening = pad + this on each side) |
| `mlMinCreamFrame` / `mlMaxCreamFrame` | `0mil` | solder-paste (cream) shrink/expand |
| `mlViaStopLimit` | `25mil` | vias ≤ this get tented (no mask opening) |
| `rlMinPad*` / `rlMaxPad*` | `10/20mil` | pad annular-ring restring per layer |

`layerSetup` syntax: copper layer numbers in stack order; `*` repeats the previous
core. `(1+2*15+16)` = Top, [inner 2, inner 15], Bottom. The mask-frame params drive
the `tStop`/`bStop` opening size that later appears as the Gerber solder-mask DAM —
see `gerber-parsing.md`.

## Pulling vendor footprints VERBATIM (`parse_vendor`)

Hand-built land patterns are error-prone for fine-pitch parts (WROOM-1, DRV8313
HTSSOP, USB-C). The proven approach: download the manufacturer's EAGLE library
(`.lbr`, e.g. from SnapMagic/Ultra Librarian) and **copy its `<package>` XML
verbatim** into the board/schematic. A `.lbr` is itself an `<eagle>` file with a
`<library>` under `<drawing>`.

`parse_vendor()` in `generate_brd.py` does exactly this:

```python
root = ET.parse(lbr_path).getroot()
lib  = root.find(".//library")
for pk in lib.find("packages").findall("package"):
    pkgs[pk.get("name")] = ET.tostring(pk, encoding="unicode")  # VERBATIM XML
```

It also reads each deviceset's `<device>` to recover `ds2pkg` (deviceset→package),
`ds2conn` (the pin→pad map), and `ds2attrs` (technology attributes). `load_real_libs()`
in `generate_sch.py` embeds the whole vendor `<library>` (renaming it to the
filename) after stripping cloud-only bits (`<packages3d>`, `<package3dinstances>`)
that Fusion doesn't need locally.

Rules when embedding vendor XML:
- **Do not reformat it.** Tostring/verbatim copy keeps the vendor's exact
  pad geometry, mask overrides (`stop="no"`), and `roundness`.
- Re-`name` the `<library>` to a stable key you reference from `<element>`/`<part>`.
- Vendor SMD pads may set `stop="no"` (no mask opening, e.g. thermal EP) — relevant
  when measuring mask DAMs in Gerbers.

## Coordinate / unit conventions

- All board/footprint coordinates are **mm**, Y-up, origin bottom-left of the design.
- Rotations are strings `R0/R90/R180/R270`. To rotate a local pad `(lx,ly)` into
  board coords at element `(ex,ey,rot)`:
  `bx = ex + lx*cosθ − ly*sinθ`, `by = ey + lx*sinθ + ly*cosθ` (see
  `gnd_thermal_vias()` in `generate_brd.py`).
- Lengths in `<param>` may be `mm` or `mil` strings — parse the suffix; 1 mil = 0.0254 mm.
- Always **quote file paths** (this project's paths contain spaces).
