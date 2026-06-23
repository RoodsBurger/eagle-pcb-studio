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

## 2. Plated slots — the EAGLE idiom

USB-C shield tabs (and similar) need **plated slots**, not round holes. The fab-safe
EAGLE idiom is:

> **a round `<pad>` + a single straight `<wire>` on layer 46 (Milling) along the slot
> axis, with the wire `width` equal to the slot/drill width** — NOT a closed milling
> outline (rectangle of four wires).

A closed milling outline overlapping a drilled pad is what a fab flags as
**"slot + hole overlap"**. `fix_usb` converts each shield-tab slot: it groups the
existing layer-46 milling wires by nearest pad, removes them, and emits one axis wire
from `min(y)+width/2` to `max(y)+width/2` (i.e. `max(y)-width/2`) at `x = centre`, with
`width = slot extent`. The round pad provides the plating; the single-axis route at
drill width tells the fab to mill the elongation.

**Excellon caveat:** native Excellon drill files cannot express a slot — only round hits.
So even with the correct EAGLE idiom, the gerber/drill export may render the slot as a
round drill, and the fab may still need a one-line fab note:
**"round drill + route on the same coordinate = one plated slot."** Include that note in
the order rather than assuming the drill file carries the slot.

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
