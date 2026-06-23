# Placement methodology

How `place_components.py` (and the placement section of a project `generate_brd.py`)
turns a netlist + footprint set into a compact, routable component layout. Grounded
in `generate_brd.py`.

The goal is a *good starting placement*, not a finished board: minimise wirelength so
the autorouter (or you) can close the board, pack tight to keep the board small, and
never let two pads touch. Routing is left to the user.

## 1. HPWL — the routability proxy

We never route during placement, so we need a cheap scalar that predicts how routable a
placement is. The standard proxy is **HPWL (half-perimeter wirelength)**: for each net,
take the bounding box of the centres of all parts on that net and add `width + height`.
Sum over nets. Lower HPWL = shorter airwires = fewer crossings = easier routing.

```
net_hpwl = (max_x - min_x) + (max_y - min_y)      # over the net's placed part centres
total_hpwl = sum(net_hpwl for every multi-part net)
```

Key refinements used in this project:

- **Part centre = bounding-box centre of the rotated footprint** (`ecenter()` →
  `fr()` rotates the bbox via `rbox()`). It must match the measurement tool's `ecen`
  exactly, or board selection and verification disagree.
- **Plane nets are excluded from the *pull*.** `GND` and `P3V3` connect to nearly every
  part and route to inner copper planes via a stitching via, not a track — so they exert
  no useful placement force. `_pull_nets()` drops `PLANE_NETS = {"GND","P3V3"}` and any
  single-part net. The *board-selection* HPWL (`_placed_hpwl`, `exclude_planes=False`)
  still counts them so it matches the measure tool; the *routability proxy*
  (`exclude_planes=True`) drops them.
- **Incremental HPWL** (`incr_hpwl()`): when trying a candidate position for one part,
  compute only the *growth* of each of its nets' boxes against already-placed
  neighbours. This is O(net degree), not O(all nets), so it's cheap to evaluate at every
  candidate point.

## 2. Greedy min-HPWL insertion

Parts are placed one at a time (`greedy_place()`). For the part being placed:

1. Enumerate candidate positions (see §6) at rotations `R0` and `R90`.
2. For each fitting candidate, cost = `incr_hpwl` to already-placed neighbours **plus**
   a weighted anchor pull (§4).
3. Keep the lowest-cost candidate; ties broken by distance to anchor (or distance to
   origin for ungrouped parts).

Placement order matters because greedy is order-dependent:

1. **Interior IC anchors first** (`U_BUCK`, `U_DRV`) — they're pulled toward the
   motor/power connectors by their signal nets and want first pick of the interior.
2. **Large ungrouped parts** next — big headers need open area before the gaps fill.
3. **Each group's members**, clustered around their now-placed anchor (§4).

## 3. Bottom-left-fill (BLF) for area minimisation

The fallback packer (`blf_pack`) — used when greedy fails, and as the density engine —
implements classic bottom-left-fill. For each part, drop it at the best candidate, then
apply **gravity** (`_gravity()`): slide it toward the origin, snapping flush against the
nearest obstacle or board edge. `scan="rows"` drops −y then −x (sort key `(y,x)`);
`scan="cols"` pushes −x then −y (sort key `(x,y)`). The lowest sort-key resting place
wins. This squeezes air out of the layout and minimises the used bounding box.

`layout(..., full=True)` tries every part ordering from `_orderings()` (area-desc,
height-desc, width-desc, half-perimeter, max-side, min-side, plus index rotations of the
area-desc list) × both scans, and keeps the densest result (smallest `_used_extent`
bbox). `full=False` returns the *first feasible* packing — fast, used during the board-
size search.

## 4. Group-aware clustering (anchor + members + cohesion)

Each IC or connector keeps its support parts physically close. `GROUPS` lists
`(anchor, [members])`; `MEMBER_ANCHOR` maps every member back to its anchor.

The placement cost adds a weighted distance to the anchor:

```
cost = incr_hpwl(...) + ANCHOR_W * dist(part_centre, anchor_centre)   # ANCHOR_W = 0.6
```

This **cohesion term** is what keeps decoupling caps next to their IC. A plane-only
decoupling cap (e.g. `C_MCU3` on `P3V3`/`GND`) has *zero HPWL pull* — both its nets are
planes and dropped by `_pull_nets`. Without the anchor term it would float anywhere. The
term is weak (0.6) so for real signal parts the HPWL term dominates and the anchor pull
only breaks ties; for plane-only caps it is the *only* force, so they hug their IC.

When placing members, the anchor centre is passed explicitly (`anchor_xy=`) so members
cluster around the anchor's *final* position, not a stale estimate.

## 5. Symmetric obstacle padding (`infl` / GAP) so pads never touch

Every placed part reserves a rectangle inflated by half the courtyard gap on *all four
sides*, so the empty channel between any two parts is a full `GAP` wide:

```python
GAP  = 1.05   # mm courtyard between interior parts (routing channel)
CGAP = 1.6    # mm between edge connectors
def infl(r):                 # pad an obstacle by GAP/2 symmetrically
    g = GAP / 2
    return (r[0]-g, r[1]-g, r[2]+g, r[3]+g)
```

The part is then centred inside its reserved cell (`x0 + g/2 - a`) so clearance is equal
on all sides. `fits()` also enforces a `MARGIN` (1.2 mm) part-to-board-edge clearance and
rejects any overlap (`overlap()` uses a strict `<` with a 1e-9 epsilon so edge-flush
abutment is allowed but true overlap is not).

**Choke-point fan-out halo.** Dense breakouts get extra room so their pins have escape
space: `BREAKOUT = {"U_DRV": 1.8, "J_USB": 1.8}` adds `2 * 1.8 mm` to that part's gap.
The DRV8313 PWM/VM pins and the USB-C power row are the routing bottlenecks; without the
halo the router can't fan them out.

## 6. Candidate points (free corners + L-notches)

`_candidates()` generates positions without scanning a full grid:

- **Seeded corners**: every time a part lands, `seed()` adds its top-left, bottom-right,
  and top-right corners to the candidate set. New parts can tuck into these corners.
- **L-notch cross product**: the cross product of obstacle right-edges (`r[2]`) and
  top-edges (`r[3]`) plus the `MARGIN` baseline. This lets a part wedge into the notch
  formed by two existing parts.
- **Focus + cap**: building the full cross product is O(n²). When a `focus` (the part's
  net centroid or anchor) is known, only the ~16 nearest right-edges and top-edges are
  combined, and the set is capped (`cap=120`, then unbounded as a fallback). This keeps
  each insertion cheap while still finding good spots near where the part wants to be.

## 7. Edge-locking connectors

Off-board connectors are pinned flush to a board edge and pre-placed *before* interior
parts (`place_edges()`), so cables exit cleanly and the interior packs around them:

```python
EDGE = {"bottom": ["J_PWR","J_MOTOR"],          # power + motor (high current)
        "left":   ["J_SENSOR","J_LED"],          # encoder + LED strip
        "right":  ["J_USB","J_KNOB","J_BTN"]}    # USB-C + knob + button
EDGE_ROT = {"bottom":"R0", "left":"R270", "right":"R90"}
```

Each connector's rotation faces its wire/cable opening *off* the board. Edge placement
starts past the corner mounting-hole keep-outs (`e0 = HOLE_INSET + HOLE_KEEP_R + CGAP`)
and walks along the edge with `CGAP` spacing. The big module (`U_MCU` WROOM-1) is also
edge-locked, with its PCB-antenna end (`+Y` in R0) pointed off the board so no copper
sits under the antenna.

## 8. Mounting holes and board features as obstacles

Four M2.5 corner holes become keep-out rectangles the placer treats as obstacles:

```python
HOLE_DRILL = 2.8   # M2.5 clearance hole
HOLE_INSET = 4.2   # hole centre inset from each edge
HOLE_KEEP_R = 3.0  # keep-out radius (no parts)
def hole_rects(W,H): ... # 4 corner rects added to `rects` before any part is placed
```

`hole_rects()` seeds the obstacle list so greedy/BLF never place a part over a hole. The
board outline is a rounded rectangle (`CORNER_R = 3.5 mm`, quarter-arc wires on layer 20).

## 9. Board-area iteration

We don't know the right board size up front, so the placer searches a grid of W×H and
keeps the lowest-HPWL feasible board within an area budget (`place()`):

1. **Analytic floor** (`_area_floor`): sum of inflated footprint areas × 1.35 overhead,
   made square-ish, clamped ≥ the widest part. Cheap starting size, no packing.
2. **Coarse search**: try `W ∈ [FW-3, FW+4]` × `H = FH + [-2, +5]`, skipping any board
   over `AREA_CAP_CM2 = 30 cm²`. For each, run a *fast* greedy layout
   (`do_refine=False`, only the 7 top-edge MCU anchors). Rank candidate boards by the
   unrefined HPWL proxy, tie-broken by area.
3. **Refine the top 6**: re-run the single best MCU anchor for each with refinement on,
   keep the lowest-HPWL result.
4. **Fallback**: if greedy fails on every board, use the BLF area packer at the analytic
   floor size.

The two-phase split (cheap unrefined search → refine only the promising few) keeps the
whole placement fast while still exploring many board sizes and MCU positions.

## 10. Local refinement (`refine`)

After all parts are placed, `refine()` does a few cleanup passes: lift each movable part
out and re-insert it greedily, now that its net centroid *and* anchor are fully known.
The move is kept only if the combined cost (`_place_cost` = HPWL + anchor pull, mirroring
`greedy_place`) strictly drops; otherwise the original spot is restored. Repeats while
anything improves. This fixes the order-dependence of single-pass greedy without a full
simulated-anneal.

## Tunable knobs (quick reference)

| Constant | Meaning | Project value |
|---|---|---|
| `GAP` | interior courtyard / routing channel | 1.05 mm |
| `CGAP` | spacing between edge connectors | 1.6 mm |
| `MARGIN` | part-to-board-edge clearance | 1.2 mm |
| `ANCHOR_W` | cohesion weight (anchor pull) | 0.6 |
| `BREAKOUT` | extra fan-out halo per choke part | 1.8 mm (`U_DRV`, `J_USB`) |
| `AREA_CAP_CM2` | routability area budget | 30 cm² |
| `HOLE_KEEP_R` | mounting-hole keep-out radius | 3.0 mm |
| `PLANE_NETS` | nets excluded from HPWL pull | `GND`, `P3V3` |
