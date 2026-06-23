# Generation pattern: writing `generate_sch.py` + `generate_brd.py`

How to turn a component/netlist spec into a Fusion Electronics / EAGLE 9.x schematic and
matching board. Grounded in `generate_sch.py` and
`generate_brd.py`.

The schematic is the source of truth for the netlist and the device library; the board
re-uses the *same spec tables* so the two files stay consistent. Both emit raw XML text
with the mandatory `<!DOCTYPE eagle SYSTEM "eagle.dtd">` preserved verbatim.

## The five spec tables

Everything is driven by a handful of Python dicts at the top of `generate_sch.py`. The
board imports them directly (`from generate_sch import PKG, DEVSETS, PARTS, REPLACED,
REAL_FILES, COMP_DIR, build_package`) so there is exactly one definition of each part.

1. **`SYMS`** — `symbol_name → [pin names]`. Generic schematic symbols (the logical
   pins you wire to). `build_symbol()` lays them out as a labelled box with pins on left
   and right halves.

2. **`PKG`** — `package_name → [(pad_xml, pad_name), ...]`. Generic **land patterns**.
   Helper builders (`smd`, `thp`, `two`, `sot23`, `htssop28`, `wroom1`, `screw`, `xh`,
   `hdr`, `tact`) emit IPC-7351-ish pads. `two(offset, dx, dy)` is the chip-passive
   workhorse. Part-specific packages (`CAP_D`, `CAP_V`, `IND43`, `TACT`) carry a note to
   verify against the real component you buy.

3. **`DEVSETS`** — `deviceset_name → (symbol, package, {pin: pad(s)}, ref_prefix)`. Binds
   a symbol to a package and maps every logical pin to one or more physical pads
   (space-separated, e.g. `"GND": "14 20 28 ... EP"`). The prefix sets the reference
   designator letter (`R`, `C`, `U`, `J`, …).

4. **`PARTS`** — `part_name → (deviceset, value, {pin: net})`. The instances on the
   board that use *generic* `BLLIB` devices, with their net membership.

5. **`REPLACED`** + **`REAL_FILES`** — parts that use **vendor `.lbr`** libraries
   instead of generic land patterns. `REAL_FILES` maps `libname → filename` under
   `COMP_DIR`; `REPLACED` maps `part_name → (libname, deviceset, {real_pin: net})` using
   the *real* pin names found inside the vendor `.lbr`.

Net membership is just the `{pin: net}` dicts across `PARTS` and `REPLACED` — there is no
separate netlist file. A net exists because two or more pins name it.

## `parse_vendor()` — copy vendor package XML verbatim

The single most important rule: **vendor footprints must be byte-identical in the .sch
and the .brd**, or Fusion's ERC/consistency check complains the moment you open the board
from the schematic. So the board does **not** rebuild vendor packages — it copies the
`<package>` element straight out of the `.lbr`:

```python
for pk in lib.find("packages").findall("package"):
    pkgs[pk.get("name")] = dict(xml=ET.tostring(pk, encoding="unicode"), ...)
```

`parse_vendor()` (board) and `load_real_libs()` (schematic) both read the same `.lbr`
files and extract:

- the embeddable `<library>` XML (board keeps only `<packages>`; schematic keeps the
  whole library including symbols/devicesets and strips `packages3d` / cloud 3D refs),
- `ds2pkg` — which package each vendor deviceset uses,
- `ds2conn` — the vendor's own pin→pad `<connect>` map (used to emit board contactrefs),
- `ds2attrs` — vendor technology attributes (MPN etc.),
- pad sets and footprint bounding boxes (board needs bboxes for placement).

Generic packages *are* rebuilt identically on both sides because both call the shared
`build_package()` from `generate_sch.py` — same function, same bytes.

## Validation before emit

Both scripts validate the spec before writing anything, and `raise SystemExit` on any
error so a broken spec never produces a file:

- every `DEVSETS` pad exists in its `PKG` pad set;
- every `PARTS` pin exists in its symbol's pin list;
- every `REPLACED` pin exists in the *real* vendor symbol's pins (schematic) and every
  mapped pad exists in the vendor package (board, `validate()`).

This catches the common failure — a datasheet pin/pad typo — at generation time rather
than as a confusing Fusion error.

## What the schematic emits

`generate_sch.py` writes `<schematic>` with:

- **`<libraries>`**: the generic `BLLIB` (`<packages>` + `<symbols>` + `<devicesets>`)
  followed by each vendor `<library>`.
- **`<classes>`**: net classes for wider power copper (`pwr24` 0.5 mm, `pwr3v3` 0.4 mm).
- **`<parts>`**: one `<part>` per instance referencing `library`/`deviceset`/`device`.
- **`<sheets><sheet>`** with `<instances>` (parts placed on a wide grid to avoid
  pin-coordinate collisions) and **`<nets>`**: each net is a set of `<segment>`s, one per
  pin, each a `<pinref>` + a short labelled stub `<wire>` on layer 91. Grouping segments
  by net name under one `<net>` is what makes them electrically one net.

Silkscreen polarity marks (`SILK`: cathode bars, `+` for polarised caps, pin-1 dots) are
added to generic packages on layer 21 so PCBWay places polarised parts correctly.

## What the board emits

`generate_brd.py` runs the placement engine (see `placement-methodology.md`) then writes
`<board>` with:

- **`<plain>`**: rounded board outline (layer 20) + mounting `<hole>`s.
- **`<libraries>`**: the same `BLLIB` packages + verbatim vendor `<packages>`.
- **`<elements>`**: one `<element>` per part with its placed `x`/`y`/`rot` and (for
  vendor parts) `<attribute>` MPN tags.
- **`<signals>`**: per net, a `<contactref element=... pad=.../>` for every pad on that
  net (expanding multi-pad pins via `conn[pin].split()`), plus inner-plane polygons for
  `GND`/`P3V3` and pre-placed thermal/escape vias.

The board adds the 4-layer plane machinery the schematic doesn't need: inner `GND`
(Route2) and `P3V3` (Route15) `<polygon>` pours, both notched around the WROOM antenna;
`gnd_thermal_vias()` for inaccessible exposed-pad GND; `power_escape_vias()` for VBUS/VM
pins the router can't break out of dense parts.

## Keeping sch and brd footprints byte-identical

This is the rule that makes "generate sch, generate brd, open board from sch" work
without ERC drift:

1. **Generic packages**: only ever built by the shared `build_package(name, PKG[name])`.
   Never re-implement the pad math in the board.
2. **Vendor packages**: copied verbatim from the `.lbr` by both sides; never
   re-serialised through a full tree round-trip on one side only.
3. **Net classes** (`<classes>` block) are emitted identically in both files.
4. **The `NET_CLASS` map** (`P24`/`VBUS` → wider copper) is duplicated identically.

If you ever *edit* an existing footprint, edit the `.lbr` and re-sync it into both files
by **splicing the raw `<package>...</package>` text** (regex replace on the source bytes),
never by re-serialising the whole document — that would reorder attributes and reflow
whitespace, which reads as a diff to Fusion. See `manufacturing-prep.md §footprint sync`
and `fix_masks_slots.py` for the splice idiom.

## DOCTYPE and XML rules

- Emit the literal header verbatim, every time:
  ```
  <?xml version="1.0" encoding="utf-8"?>
  <!DOCTYPE eagle SYSTEM "eagle.dtd">
  <eagle version="9.6.2">
  ```
- Escape every attribute value (`xml.sax.saxutils.escape` with `{'"': "&quot;"}`).
- Parse with `ElementTree` for *reading/validation*, but build output as f-string text
  so the DOCTYPE and formatting survive. `ET.fromstring(...)` at the end is a cheap
  "does it still parse" check.
- Layer tables: the schematic needs the symbol/net layers (90–98) plus a copper subset;
  the board needs the full physical layer stack (1–98, including Route2/Route15 inner
  copper, restrict/keepout 41–43, milling 46). Merge vendor `.lbr` layer numbers in so
  vendor footprints' layer references resolve.

## Adding a new part — checklist

1. Generic part: add a `PKG` entry (if a new land pattern), a `DEVSETS` entry binding
   symbol+package+pin→pad+prefix, then a `PARTS` entry with value and `{pin: net}`.
2. Vendor part: drop the `.lbr` in `COMP_DIR`, add it to `REAL_FILES`, add a `REPLACED`
   entry using the *real* pin names from the `.lbr`.
3. Run `python3 generate_sch.py` — it validates and reports `parts=… nets=…`.
4. Run `python3 generate_brd.py` — it re-validates, places, and reports board size /
   element / signal / contactref counts.
5. Open the `.brd` from the `.sch` in Fusion; ERC should be clean if footprints match.
