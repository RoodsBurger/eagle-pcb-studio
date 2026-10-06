# Manufacturing prep — DFM fix playbook

The design-for-manufacturing fixes applied to this project before fab, plus the
reasoning to apply them elsewhere. Grounded in `/tmp/fix_masks_slots.py`
(`fix_drv` / `fix_usb`) and the PCBWay order workflow.

All edits follow the project rule: **parse with ElementTree, but splice the edited
`<package>...</package>` back into the *raw* file text** (regex replace), never full-tree
re-serialize — that preserves the `<!DOCTYPE eagle SYSTEM "eagle.dtd">` and avoids
attribute/whitespace churn that Fusion reads as drift.

## 1. Solder-mask dams on fine pitch

On fine-pitch parts (DRV8313 HTSSOP 0.65 mm, USB-C 0.5 mm) the **solder-mask dam** — the
sliver of green mask between adjacent pad openings — is what stops solder bridging. A fab
has a minimum dam it can reliably print (commonly ~0.1 mm; thinner gangs into one opening
and the pads bridge). Two ways to get a usable dam, plus the escape hatch:

- **Option 1 — mask-defined (widen the dam).** Make the mask opening *smaller* than the
  copper pad so mask overlaps the copper edge. The dam width is then set by the opening
  spacing, not the copper spacing. `fix_drv` shrinks each signal-pad mask opening (layer
  29, `tStop`) to `DRV_DAM_H = 0.40 mm` tall, so at 0.65 mm pitch the dam is
  `0.65 − 0.40 = 0.25 mm`. `fix_usb` shrinks each USB-C mask rect by `USB_SHRINK =
  0.025 mm` per side in the pitch direction → a 0.25 mm dam at 0.5 mm pitch. This is the
  preferred fix for fine pitch.
- **Option 2 — expose / gang.** Open the mask over the whole pad row (one big opening, no
  dams). Relies entirely on stencil + paste control and reflow surface tension; only
  acceptable when the fab can't hold the dam at that pitch and you trust assembly.
- **Hard ceiling — copper gap.** Mask-defining widens the *dam* but does **not** change
  the copper-to-copper gap. If the copper gap itself is below the fab's minimum, no mask
  trick helps; you must respin the footprint with narrower pads / wider gap. State this
  when offering the options.

`fix_drv` is careful to edit only signal-pad openings: it skips the large exposed
thermal pad by gating on opening size (`0.4 < h < 0.7 and w < 2.5` selects the small
signal openings, not the ~3.5 × 9.8 mm EP).

### The reply options (when asking the user how to handle fine pitch)

When a fab flags fine-pitch mask, present three concrete choices:
1. **Mask-defined** — widen the dam to 0.25 mm (recommended; what these scripts do).
2. **Expose / gang** — open mask over the row, lean on stencil + reflow.
3. **Color/finish caveat** — dam printability depends on mask color and finish; some
   colors hold a finer dam than others, and ENIG vs HASL changes the practical floor.

## 2. Plated slots — two idioms

USB-C shield tabs (and similar) need **plated slots**, not round holes.

**Preferred (Fusion Electronics): a native slot pad.** One `<pad>` carries the slot:

```xml
<pad name="S1" x="-4.325" y="4.17" drill="0.6" diameter="1" slotLength="1.7" shape="slot" rot="R90"/>
```

This is the syntax Fusion's own bundled connector libraries use. Fusion's CAM writes each slot
into the Excellon file as a **routed block** (`G00 X.. Y..` / `M15` / `G01 X.. Y..` / `M16`) with the
drill tool as the rout width — exactly what the fab routs. Before every order:
`grep -c M15 DrillFiles/drill_*.xln` must equal the slot count (`analyze_gerbers.py --slots N`
checks it). Fab limits (PCBWay): slot width ≥ 0.5 mm, length/width ≥ 2. The stock `eagle.dtd`
lacks the `slot`/`slotLength` tokens — add them if you validate against the DTD. Clearance
checks must model a slot pad as a **stadium** (core rectangle + two end circles) of the pad width
and slot + ring length, not as a rectangle. Order remark: *"the drill file contains N routed
plated slots (<part>), rout as plated slots"*.

**Fallback (EAGLE 9, no slot pads): a round `<pad>` + a single straight layer-46 (Milling) wire
along the slot axis, width = slot/drill width.** Never a closed milling outline (a rectangle of
four wires over a drilled pad is what fabs flag as **"slot + hole overlap"**). Know its limit:
**milling-layer drawings never reach the drill file.** The CAM export carries only the round
drill, so the slot exists for the fab only if the milling layer is in the package and the order
note says *"round drill + route on the same coordinate = one plated slot."* Overlapping gang
drills (several Ø0.6 hits along the slot) are the other legacy workaround; they work but trip
"Drill Clearance" DRC and inflate the hole count.

## 2b. Copper rules worth checking (from boards that passed PCBWay review)

- **Copper-to-edge ≥ 0.4 mm** for every wire end, via and polygon vertex; inset pours ≥ 0.5 mm.
  `analyze_board.py` reports `copper.edge` offenders. DRU `mdCopperDimension` is the matching
  rule (also copper-to-unplated-hole: PCBWay 16 mil / 0.41 mm for wires).
- **Pour separation ≥ 0.3 mm** between polygons of different nets on one layer, measured
  edge-to-edge, not by bounding boxes. EAGLE copper extends `width/2` beyond a polygon's vertex
  path: emit vertices **inset by width/2** (0.1016 mm for the usual 0.2032 width) so the copper
  edge lands on the nominal line. Copper gap = vertex gap − outline width, and must stay ≥ the
  DRU clearance (0.152 mm at 6 mil).
- **Plane-net SMD pads need their own via + stub**, or Fusion shows an airwire even with a
  poured plane. Above ~1 A use several (a 0.3/0.6 via carries ≈ 1 A). THT pads of a rail net
  must sit inside that rail's pour.
- **Junction rule:** two stubs ending at the same point inside a pad form a wire junction that
  EAGLE does not count as a pad connection. Give each stub its own end point ≥ 0.1 mm inside the pad.
- **Coordinate text must match exactly.** A stub connects to its via only when both carry
  identical coordinate text; a 0.0004 mm mismatch is an open in Fusion. Emit 3 decimals, round
  before comparing, and compare with a 2 µm tolerance on import. Duplicated vias at 4-decimal vs
  1 µm coordinates trip "Drill Clearance".
- **No via inside an SMD pad** (EAGLE forbids via-in-pad; `mnLayersViaInSmd`). Treat a via
  touching any pad — even its own net — as an error; keep ≥ 0.2 mm.
- **`thermals="no"`** on vendor thermal-via pads (spokes cannot bridge a 0.7–1.2 mm grid; the
  pads sit solidly in the plane) and on high-current THT pads (≥ 3 A). `slThermalsForVias 0`
  joins vias to pours solidly. A tab spreader pour (e.g. 4.8 × 5.5 mm with 4 vias) must be solid,
  not spoked.
- **Vendor footprints whose thermal vias are drilled `<pad>`s:** add those pads to the EP pin's
  `<connect>` list so the plane reaches them; a 0.2 mm drill needs DRU `msDrill` 0.19 mm or
  Fusion's "Drill Size" check flags every one. `analyze_board.py thermal.ep` counts such pads.
- **Mounting holes:** cut plane pours out around unplated holes (e.g. Ø2.2 mm hole → 2.3 mm
  radius cutout, 1.2 mm from the wall), or a metal screw shorts GND or the rail.
- **RF module antenna keepout:** copper-free on **every** layer under the module's restrict
  rectangles (layers 41/42/43) + 0.5 mm; no part origin within 3 mm; notch pours around the strip.
  Silk text is fine there, text on copper layers is not.
- **Net-class width = the narrowest pad on the class** so the router can leave every pin; carry
  the current in pre-routes and pours, not in router traces.
- **Connector wire-entry faces the board edge** (a per-footprint "front" side); edge proximity
  alone is not enough.
- **Body gaps ≥ 0.3 mm** per copper side (THT parts count on both sides).

## 3. Footprint sync: `.lbr` → `.sch` / `.brd`

When a footprint is fixed in a vendor `.lbr`, the same `<package>` lives (verbatim) in
the `.sch` and `.brd`; all three must stay byte-identical or Fusion flags a mismatch.
`fix_masks_slots.py`'s `patch()` is the reusable sync idiom:

```python
def patch(path, fixes):
    shutil.copy(path, path + ".maskfix.bak")     # always back up first
    raw  = open(path).read()                       # raw text to splice into
    root = ET.parse(path).getroot()                # tree to *find/edit* the package
    for pkg_name, fn in fixes:
        pk = next(p for p in root.iter("package") if p.get("name") == pkg_name)
        fn(pk)                                      # mutate the ElementTree node
        newblock = ET.tostring(pk, encoding="unicode")
        pat = re.compile(r'<package name="' + re.escape(pkg_name) + r'".*?</package>',
                         re.DOTALL)
        m = pat.search(raw)
        raw = raw[:m.start()] + newblock + raw[m.end():]   # splice into raw text
    open(path, "w").write(raw)
    ET.fromstring(open(path).read())               # validate it still parses
```

Apply the *same* `(pkg_name, fix_fn)` list to the `.lbr`, the `.brd`, and (if it carries
that package) the `.sch`. In this project the same `DRV` and `USB` fixes were patched
into `DRV8313PWPR.lbr`, `TYPE-C-31-M-12.lbr`, and `your-board.brd`. Note the
vendor package names differ from the deviceset names — `SOP65P640X120-29N` (DRV) and
`HRO_TYPE-C-31-M-12` (USB-C) are the *package* element names to match.

Key safety points:
- Always write a backup (`*.maskfix.bak`) before touching the file.
- Splice by the package's raw text span — only that block changes; the DOCTYPE, layer
  table, and every other package stay byte-for-byte identical.
- Re-parse at the end as a validity gate.
- Use a stable number formatter (`fnum`: round to 4 dp, strip trailing zeros, normalise
  `-0`→`0`) so coordinates match EAGLE's own formatting and don't churn the diff.

## 4. PCBWay BOM + order parameters

**BOM**: produced by the BOM script (the only one allowed `openpyxl`). It rolls up the
`PARTS`/`REPLACED` spec by value + footprint into PCBWay's expected columns (reference
designators, qty, value, footprint, MPN from the vendor `.lbr` technology attributes).
DNP parts (values tagged `-DNP`, e.g. the encoder pull-ups and LED clamps) are flagged
Do-Not-Populate, not dropped.

**Order parameters** — these are set in the PCBWay *order form*, NOT in the gerbers:

- **4-layer** stack-up — the board defines inner Route2 (GND) / Route15 (PWR) copper, so
  order it as 4-layer with the impedance/stack the planes assume.
- **ENIG finish** — required for the fine-pitch parts (flat coplanar pads for the
  0.5 mm USB-C and 0.65 mm DRV8313); HASL's uneven surface is marginal at that pitch.
- **Board thickness / copper weight / finish color** — chosen in the order form. The
  gerbers carry geometry only; thickness and finish are commercial options, so don't try
  to encode them in the design — set them at checkout.
- **Fab note** — carry the plated-slot note from §2 ("round drill + route = one slot")
  into the order remarks.

### PCBWay capability numbers (prototype service)

| Item | Limit |
|---|---|
| Trace / space | 0.2 mm comfortable (absolute 4 mil / 0.1 mm) |
| Via drill / minimum drill | 0.3 mm / 0.2 mm |
| Hole-to-hole | ≥ 0.30 mm (vias) / 0.45 mm (PTH) |
| Copper to unplated hole | 16 mil (0.41 mm) |
| Annular ring | 0.15 mm |
| Silkscreen | ≥ 0.8 mm text height, 0.15 mm stroke (ratio 19) |
| Plated slot | width ≥ 0.5 mm, length/width ≥ 2 |
| Size surcharge | boards with **both** sides < 50 mm cost extra — keep one side at 50 mm |

The DRU that matches these (`layerSetup (1+2*15+16)`, `md*` 0.152 mm, `msWidth` 6 mil,
`msDrill` 0.19 mm, `mdDrill` 0.3 mm, `mdCopperDimension` 0.2 mm, `rlMinPad*` 10 mil,
`rlMinVia*` 6 mil, `mlMin/MaxStopFrame` 4 mil, `mlViaStopLimit` 25 mil, `slThermalIsolate` 10 mil,
`slThermalsForVias` 0) is documented in `eagle-xml-format.md`. The DRU `description` text goes
stale — trust the parameter values, not the prose.

### CAM export

- Run the CAM job for the **actual layer count**. A 2-layer job on a 4-layer board silently drops
  the inner planes and every plane net is open in the fab files. Confirm `copper_inner_l2` /
  `copper_inner_l3` are in the zip (`analyze_gerbers.py --layers 4`).
- The `.gbrjob` `BoardThickness` is the DRU stackup sum (e.g. 1.99), not the ordered thickness;
  thickness, finish and colours are order-form settings.
- PCBWay's upload preview draws Fusion's silk text (filled polygon regions) as a tangle of
  straight lines. The files are fine — verify with a Gerber viewer (gerbonara renders them
  correctly) and mention it in the remarks if the preview worries you.

### Centroid / pick-and-place (CPL)

Columns: `Designator, Mid X, Mid Y, Layer, Rotation`. Rules that avoid placement offsets:
- Use the **body centre**, not the element origin, for footprints whose origin sits on pin 1 or
  off-centre (modules: the silhouette centre can be 4.7 mm from the pad centroid).
- If a footprint's only silk is a polarity bar, use the **pad centroid** — the silk centre lands
  on the cathode bar (1.5–3.5 mm off).
- Drop DNP/DNS parts from the CPL.
- PCBWay's order check may not find a centroid file inside the CAM zip; upload the CPL separately
  under *Production Status* when asked. Ask for the placement preview before reflow.

### Assembly order form

- "Detailed information of assembly" is limited to **600 characters**; state slots, THT handling,
  DNP refs, polarity conventions and "exact MPN for ICs / module / connectors; equivalent passives OK".
- The "Number of …" fields may stay blank. Plated slots count as through-holes if asked.
- China substitutes = **No** with the remark above; THT Populate = Yes means PCBWay hand-solders.
- Layer order for the form ("top-side view to bottom"): silk/paste/mask top, `copper_top_l1`,
  `copper_inner_l2`, `copper_inner_l3`, `copper_bottom_l4`, mask/silk bottom; `profile.gbr`; `drill_*.xln`.
- No fiducials needed for prototypes (PCBWay adds rail fiducials). An EP paste window over
  thermal vias wicks solder — ask for the vias to be plugged or shrink the paste there.

## DFM checklist before ordering

1. Fine-pitch mask dams ≥ fab minimum — mask-define if needed (`fix_drv`/`fix_usb`),
   confirm the copper gap clears the hard ceiling.
2. Plated slots = round pad + single layer-46 axis wire at drill width; add the
   Excellon fab note.
3. Footprint fixes synced `.lbr` → `.brd` (→ `.sch`) byte-identical via the splice
   `patch()`; backups written; files re-parse.
4. BOM rolled up with MPNs; DNP parts flagged.
5. Order form: 4-layer, ENIG, thickness/finish/color set there (not in gerbers); slot
   fab note in remarks.
- [ ] Copper-to-edge ≥ 0.4 mm, pour gaps ≥ 0.3 mm, no via in an SMD pad (`analyze_board.py`).
- [ ] Every plane-net SMD pad has its own via; 0 airwires after Ratsnest.
- [ ] CAM job matches the layer count; inner copper files present (`analyze_gerbers.py --layers N`).
- [ ] Routed-slot count in the drill file equals the slot pads (`analyze_gerbers.py --slots N`).
- [ ] CPL centroids on the body centre; DNP rows removed; polarity marks on silk.
