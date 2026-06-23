#!/usr/bin/env python3
"""
place_components.py -- spec-driven PCB component placement engine.

A reusable, zero-dependency (Python 3.8+ stdlib only) placer extracted and
generalized from the a reference generate_brd.py layout code. It reads a JSON
SPEC describing a board, its parts, the nets between them, and optional group
anchors, then computes a compact placement that:

  * minimizes half-perimeter wirelength (HPWL) over the signal nets, plus a
    group-anchor cohesion term so parts hug their cluster anchor (essential for
    decoupling caps whose only nets are shared power planes -> no HPWL pull);
  * packs interior parts with bottom-left-fill (BLF) + gravity to minimize area;
  * pads every obstacle symmetrically by half a gap (infl) so pads never touch;
  * keeps fixed_edge parts (connectors) flush against the board edge.

It then refines the placement (lift-and-reinsert each part once the full net
context is known) and reports final area, total HPWL, the smallest pad-to-pad
gap, and an overlap count (which must be 0).

SPEC schema (JSON):
{
  "board": {
    "w": <number>,                 # board width  (mm)
    "h": <number>,                 # board height (mm)
    "corner_holes": <bool>,        # optional, default false: 4 corner mount holes
    "gap": <number>,               # optional courtyard gap between interior parts
    "edge_gap": <number>,          # optional gap between edge connectors
    "margin": <number>,            # optional part-to-board-edge clearance
    "hole_inset": <number>,        # optional mount-hole centre inset from edges
    "hole_keep_r": <number>        # optional keep-out radius around each hole
  },
  "parts": [
    { "name": "U1", "w": 7.0, "h": 7.0,        # bbox size (mm), local R0 frame
      "group": "U1",                            # optional cluster id (anchor name)
      "fixed_edge": "bottom",                   # optional: bottom|top|left|right
                                                #   edge parts keep their given w/h and
                                                #   auto-orient long-axis-along-edge (no
                                                #   dimension swap); they slide past corners
      "rot": "R90",                             # optional: force R0|R90|R180|R270 (overrides
                                                #   rot_ok and the edge auto-orient)
      "rot_ok": true,                           # optional, default true: allow R90
      "breakout": 1.8 },                        # optional extra fan-out halo (mm)
    ...
  ],
  "nets": [ { "name": "SDA", "parts": ["U1","J1"] }, ... ],
  "groups": { "U1": ["C1","C2"], ... },         # optional anchor -> member list
  "plane_nets": ["GND","VCC"]                    # optional: nets that don't pull HPWL
}

Output: placements [{ "name", "x", "y", "rot" }] (mm, EAGLE element origin frame).

Usage:
  python3 place_components.py SPEC.json [--text] [-o OUT.json]
  python3 place_components.py --demo [--text] [-o OUT.json]   # synthetic self-test
"""
import sys, os, math, json, argparse


# --------------------------------------------------------------------- defaults
# Tunables mirror generate_brd.py; the spec's board{} block can override them.
DEFAULTS = dict(
    gap=1.05,          # mm courtyard between interior parts (routing channels)
    edge_gap=1.6,      # mm between edge-mounted connectors
    margin=1.2,        # mm part-to-board-edge clearance
    hole_inset=4.2,    # mounting-hole centre inset from each board edge
    hole_keep_r=3.0,   # keep-out radius around each hole (no parts)
    anchor_w=0.6,      # weight on anchor-distance in the placement cost
)
VALID_EDGES = ("bottom", "top", "left", "right")


# ============================================================ placement engine
class Placer:
    """Holds one placement problem (board + parts + nets + groups) and solves it.

    All geometry is in millimetres. Part bboxes are given in the part's local R0
    frame as (w, h); the engine works internally with axis-aligned (xmin, ymin,
    xmax, ymax) rectangles rotated about the origin via rbox()."""

    def __init__(self, spec):
        b = spec.get("board", {})
        self.W = float(b["w"])
        self.H = float(b["h"])
        self.corner_holes = bool(b.get("corner_holes", False))
        self.GAP = float(b.get("gap", DEFAULTS["gap"]))
        self.CGAP = float(b.get("edge_gap", DEFAULTS["edge_gap"]))
        self.MARGIN = float(b.get("margin", DEFAULTS["margin"]))
        self.HOLE_INSET = float(b.get("hole_inset", DEFAULTS["hole_inset"]))
        self.HOLE_KEEP_R = float(b.get("hole_keep_r", DEFAULTS["hole_keep_r"]))
        self.ANCHOR_W = float(b.get("anchor_w", DEFAULTS["anchor_w"]))

        # part bboxes are centred on the local origin (matches a typical footprint)
        self.by = {}
        self.rot_ok = {}
        self.fixed_rot = {}
        self.fixed_edge = {}
        self.group = {}
        self.breakout = {}
        order = []
        for p in spec.get("parts", []):
            nm = p["name"]
            w, h = float(p["w"]), float(p["h"])
            self.by[nm] = (-w / 2, -h / 2, w / 2, h / 2)
            self.rot_ok[nm] = bool(p.get("rot_ok", True))
            self.breakout[nm] = float(p.get("breakout", 0.0))
            rr = p.get("rot")
            if rr is not None:
                if rr not in ("R0", "R90", "R180", "R270"):
                    raise ValueError(f"{nm}: bad rot {rr!r}")
                self.fixed_rot[nm] = rr
            fe = p.get("fixed_edge")
            if fe:
                if fe not in VALID_EDGES:
                    raise ValueError(f"{nm}: bad fixed_edge {fe!r}")
                self.fixed_edge[nm] = fe
            g = p.get("group")
            if g:
                self.group[nm] = g
            order.append(nm)
        self.part_order = order

        # nets: part -> set(nets), net -> set(parts)
        self.PLANE_NETS = set(spec.get("plane_nets", []))
        self.PART_NETS = {nm: set() for nm in self.by}
        self.NET_PARTS = {}
        for net in spec.get("nets", []):
            name = net["name"]
            members = [m for m in net["parts"] if m in self.by]
            if not members:
                continue
            self.NET_PARTS.setdefault(name, set()).update(members)
            for m in members:
                self.PART_NETS[m].add(name)

        # groups: anchor -> member list. An explicit "groups" map wins; otherwise
        # derive groups from each part's "group" tag (anchor = the group id itself
        # if it is a real part, else members cluster around their net centroid).
        groups = spec.get("groups")
        if groups:
            self.GROUP_OF = {a: list(m) for a, m in groups.items()}
        else:
            tmp = {}
            for nm, g in self.group.items():
                if g != nm:                            # the anchor part isn't its own member
                    tmp.setdefault(g, []).append(nm)
            self.GROUP_OF = tmp
        # member part -> its anchor (so plane-only caps still get pulled to a place)
        self.MEMBER_ANCHOR = {}
        for a, mem in self.GROUP_OF.items():
            for m in mem:
                self.MEMBER_ANCHOR[m] = a

        # classify parts
        self.EDGE_NAMES = list(self.fixed_edge.keys())
        self.INTERIOR = [nm for nm in self.part_order if nm not in self.fixed_edge]
        # anchors that are themselves interior parts get placed before their members
        self.INTERIOR_ANCHORS = [a for a in self.GROUP_OF
                                 if a in self.INTERIOR and a in self.by]
        grouped = set(self.MEMBER_ANCHOR) | set(self.GROUP_OF)
        self.UNGROUPED = [nm for nm in self.INTERIOR
                          if nm not in grouped and nm not in self.INTERIOR_ANCHORS]

    # --------------------------------------------------------------- geometry
    def rbox(self, bb, rot):
        a, b, c, d = bb
        if rot == "R0":   return (a, b, c, d)
        if rot == "R90":  return (-d, a, -b, c)
        if rot == "R180": return (-c, -d, -a, -b)
        if rot == "R270": return (b, -c, d, -a)
        raise ValueError(rot)

    def fr(self, nm, rot="R0"):
        return self.rbox(self.by[nm], rot)             # rmnx, rmny, rmxx, rmxy

    def overlap(self, r, rects):
        x0, y0, x1, y1 = r
        return any(x0 < a1 - 1e-9 and a0 < x1 - 1e-9 and y0 < b1 - 1e-9 and b0 < y1 - 1e-9
                   for a0, b0, a1, b1 in rects)

    def infl(self, r):                                 # pad an obstacle by half a gap
        g = self.GAP / 2
        return (r[0] - g, r[1] - g, r[2] + g, r[3] + g)

    def hole_rects(self):
        if not self.corner_holes:
            return []
        I, K, W, H = self.HOLE_INSET, self.HOLE_KEEP_R, self.W, self.H
        return [(I - K, I - K, I + K, I + K), (W - I - K, I - K, W - I + K, I + K),
                (I - K, H - I - K, I + K, H - I + K), (W - I - K, H - I - K, W - I + K, H - I + K)]

    def fits(self, r, rects):
        x0, y0, x1, y1 = r
        m, W, H = self.MARGIN, self.W, self.H
        return not (x0 < m - 1e-9 or y0 < m - 1e-9 or x1 > W - m + 1e-9
                    or y1 > H - m + 1e-9 or self.overlap(r, rects))

    @staticmethod
    def seed(cands, x0, y0, x1, y1):
        cands |= {(x1, y0), (x0, y1), (x1, y1)}

    # --------------------------------------------------------------- net costs
    def _pull_nets(self, name):
        """Signal nets that actually pull two parts together (drop planes + singletons)."""
        return [n for n in self.PART_NETS[name]
                if n not in self.PLANE_NETS and len(self.NET_PARTS[n]) >= 2]

    def ecenter(self, name, pos):
        """Board-coord centre of a placed part's bbox."""
        x, y, rot = pos[name]
        a, b, c, d = self.fr(name, rot)
        return (x + (a + c) / 2, y + (b + d) / 2)

    def incr_hpwl(self, name, cx, cy, pos):
        """Added HPWL if `name` is centred at (cx,cy): summed half-perimeter growth of
        each pulling net's placed-centre bounding box."""
        add = 0.0
        for net in self._pull_nets(name):
            xs, ys = [], []
            for q in self.NET_PARTS[net]:
                if q != name and q in pos:
                    qx, qy = self.ecenter(q, pos); xs.append(qx); ys.append(qy)
            if not xs:
                continue
            bx0, bx1 = min(xs), max(xs); by0, by1 = min(ys), max(ys)
            base = (bx1 - bx0) + (by1 - by0)
            nx0, nx1 = min(bx0, cx), max(bx1, cx)
            ny0, ny1 = min(by0, cy), max(by1, cy)
            add += (nx1 - nx0) + (ny1 - ny0) - base
        return add

    def _net_centroid(self, name, pos):
        """Mean centre of the part's already-placed pulling-net neighbours (or None)."""
        xs, ys = [], []
        for net in self._pull_nets(name):
            for q in self.NET_PARTS[net]:
                if q != name and q in pos:
                    qx, qy = self.ecenter(q, pos); xs.append(qx); ys.append(qy)
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def _candidates(self, rects, cands, focus=None, cap=140):
        """Free-corner candidate points: the seeded corners plus the cross product of
        obstacle right/top edges (L-notches). Near `focus` only, capped, to stay cheap."""
        pts = set(cands)
        m = self.MARGIN
        if focus is not None:
            fx, fy = focus
            xs = sorted({m} | {r[2] for r in rects}, key=lambda v: abs(v - fx))[:16]
            ys = sorted({m} | {r[3] for r in rects}, key=lambda v: abs(v - fy))[:16]
            pts |= {(cx, cy) for cx in xs for cy in ys}
            if len(pts) > cap:
                pts = sorted(pts, key=lambda p: (p[0] - fx) ** 2 + (p[1] - fy) ** 2)[:cap]
        else:
            xs = {m} | {r[2] for r in rects}
            ys = {m} | {r[3] for r in rects}
            pts |= {(cx, cy) for cx in xs for cy in ys}
        return pts

    # --------------------------------------------------------------- placement
    def _edge_rot(self, n, side):
        """Rotation for an edge-locked part. An explicit per-part 'rot' wins; otherwise
        orient so the part's LONG axis runs ALONG the edge (minimise how far it intrudes
        into the board) while honouring the given w/h -- no surprise dimension swap."""
        if n in self.fixed_rot:
            return self.fixed_rot[n]
        if not self.rot_ok.get(n, True):
            return "R0"
        a, b, c, d = self.fr(n, "R0"); w0, h0 = c - a, d - b
        if side in ("left", "right"):                  # minimise into-board WIDTH
            return "R0" if w0 <= h0 else "R90"
        return "R0" if h0 <= w0 else "R90"             # bottom/top: minimise into-board HEIGHT

    def place_edges(self, rects, cands):
        """Pin each fixed_edge part flush to its edge, sliding it along the edge past the
        corner keep-outs and any perpendicular-edge parts already placed. Each part keeps
        its given orientation (long axis along the edge) unless it carries an explicit
        'rot'. Returns the pos dict, or None if a connector genuinely won't fit."""
        pos = {}
        W, H, m = self.W, self.H, self.MARGIN
        e0 = (self.HOLE_INSET + self.HOLE_KEEP_R + self.CGAP) if self.corner_holes \
            else (m + self.CGAP)
        step = max(self.CGAP, 0.5)
        by_edge = {"bottom": [], "top": [], "left": [], "right": []}
        for nm in self.EDGE_NAMES:
            by_edge[self.fixed_edge[nm]].append(nm)
        for side, names in by_edge.items():
            t = e0
            for n in names:
                rot = self._edge_rot(n, side)
                a, b, c, d = self.fr(n, rot); w, h = c - a, d - b
                lim = (W - e0) if side in ("bottom", "top") else (H - e0)
                while True:                            # slide along the edge until it fits
                    if side == "bottom":   x0, y0 = t, m
                    elif side == "top":    x0, y0 = t, H - m - h
                    elif side == "left":   x0, y0 = m, t
                    else:                  x0, y0 = W - m - w, t
                    r = (x0, y0, x0 + w, y0 + h)
                    edge_end = r[2] if side in ("bottom", "top") else r[3]
                    if edge_end > lim + 1e-9:
                        return None                    # ran off the end of the edge
                    if self.fits(r, rects):
                        break
                    t += step                          # blocked: slide past the obstacle
                ex = self.breakout.get(n, 0.0); g = self.GAP / 2
                ri = (r[0] - g - (ex if side == "right" else 0),
                      r[1] - g - (ex if side == "top" else 0),
                      r[2] + g + (ex if side == "left" else 0),
                      r[3] + g + (ex if side == "bottom" else 0))
                pos[n] = (x0 - a, y0 - b, rot)
                rects.append(ri); self.seed(cands, *ri)
                t = (r[2] if side in ("bottom", "top") else r[3]) + self.CGAP
        return pos

    def greedy_place(self, name, rects, cands, pos, anchor_xy=None, part_rect=None):
        """Greedy min-HPWL insertion: among fitting free positions (R0/R90 if allowed),
        choose the one minimizing incremental HPWL to placed neighbours PLUS a weighted
        pull toward the group anchor. Mutates rects/cands/pos. Returns True on success."""
        rots = ("R0", "R90") if self.rot_ok.get(name, True) else ("R0",)
        g = self.GAP + 2 * self.breakout.get(name, 0.0)
        axy = anchor_xy
        if axy is None and name in self.MEMBER_ANCHOR and self.MEMBER_ANCHOR[name] in pos:
            axy = self.ecenter(self.MEMBER_ANCHOR[name], pos)
        focus = self._net_centroid(name, pos) or axy
        best = None                                    # (cost, tie, x0, y0, rot, w, h, a, b)
        for cap in (120, 10 ** 9):                      # capped near-focus set, then all
            corner = self._candidates(rects, cands, focus, cap)
            for rot in rots:
                a, b, c, d = self.fr(name, rot); w, h = (c - a) + g, (d - b) + g
                for cx, cy in corner:
                    r = (cx, cy, cx + w, cy + h)
                    if not self.fits(r, rects):
                        continue
                    ctr = (cx + w / 2, cy + h / 2)
                    cost = self.incr_hpwl(name, ctr[0], ctr[1], pos)
                    if axy is not None:
                        dist = math.hypot(ctr[0] - axy[0], ctr[1] - axy[1])
                        cost += self.ANCHOR_W * dist
                        tie = dist
                    else:
                        tie = cx * cx + cy * cy
                    if best is None or (cost, tie) < (best[0], best[1]):
                        best = (cost, tie, cx, cy, rot, w, h, a, b)
            if best is not None:
                break
        if best is None:
            return False
        _, _, x0, y0, rot, w, h, a, b = best
        rect = (x0, y0, x0 + w, y0 + h)
        rects.append(rect); self.seed(cands, x0, y0, x0 + w, y0 + h)
        cands.add((x0, y0))
        pos[name] = (x0 + g / 2 - a, y0 + g / 2 - b, rot)
        if part_rect is not None:
            part_rect[name] = rect
        return True

    def _place_cost(self, name, ctr, pos):
        """Combined placement cost at centre `ctr`: incremental HPWL + anchor pull."""
        cost = self.incr_hpwl(name, ctr[0], ctr[1], pos)
        a = self.MEMBER_ANCHOR.get(name)
        if a is not None and a in pos:
            ax, ay = self.ecenter(a, pos)
            cost += self.ANCHOR_W * math.hypot(ctr[0] - ax, ctr[1] - ay)
        return cost

    def refine(self, rects, cands, pos, part_rect, names, passes=2):
        """Local cleanup: lift each movable part out and re-insert it greedily now that
        every neighbour is placed. Keep the move only if the combined cost drops."""
        for _ in range(passes):
            improved = False
            for nm in names:
                if nm not in part_rect:
                    continue
                old = part_rect[nm]; saved = pos[nm]
                base = self._place_cost(nm, self.ecenter(nm, pos), pos)
                try:
                    rects.remove(old)
                except ValueError:
                    continue
                del part_rect[nm]; del pos[nm]
                if self.greedy_place(nm, rects, cands, pos, part_rect=part_rect):
                    new = self._place_cost(nm, self.ecenter(nm, pos), pos)
                    if new + 1e-6 < base:
                        improved = True
                    elif new > base + 1e-6:            # worse: restore original spot
                        rects.remove(part_rect[nm]); del part_rect[nm]; del pos[nm]
                        rects.append(old); pos[nm] = saved; part_rect[nm] = old
                else:                                  # couldn't re-place: restore
                    rects.append(old); pos[nm] = saved; part_rect[nm] = old
            if not improved:
                break

    def solve(self, do_refine=True):
        """Run the full pipeline and return a pos dict {name: (x, y, rot)} or None.

        Order mirrors generate_brd.py:
          0) corner-hole keep-outs are obstacles; edge connectors pinned to edges
          1) interior group anchors first (pulled by their signal nets)
          2) large ungrouped parts next (big parts need room before gaps fill)
          3) each group's members clustered around its placed anchor
          4) optional local refinement once everything is placed
        """
        rects = list(self.hole_rects())
        cands = {(self.MARGIN, self.MARGIN)}
        for r in rects:
            self.seed(cands, *r)
        epos = self.place_edges(rects, cands)
        if epos is None:
            return None
        pos = dict(epos)
        part_rect = {}

        # 1) interior IC anchors first
        for nm in self.INTERIOR_ANCHORS:
            if not self.greedy_place(nm, rects, cands, pos, part_rect=part_rect):
                return None
        # 2) large ungrouped parts (area descending)
        big = sorted(self.UNGROUPED,
                     key=lambda n: -(self.fr(n)[2] - self.fr(n)[0]) * (self.fr(n)[3] - self.fr(n)[1]))
        for nm in big:
            if not self.greedy_place(nm, rects, cands, pos, part_rect=part_rect):
                return None
        # 3) group members clustered around their anchor
        for anchor, mem in self.GROUP_OF.items():
            if anchor in pos:
                axy = self.ecenter(anchor, pos)
            else:
                axy = None                             # anchor isn't a part -> centroid pull
            mm = sorted(mem,
                        key=lambda n: -(self.fr(n)[2] - self.fr(n)[0]) * (self.fr(n)[3] - self.fr(n)[1]))
            for nm in mm:
                if nm in pos:
                    continue
                if not self.greedy_place(nm, rects, cands, pos, anchor_xy=axy, part_rect=part_rect):
                    return None
        # any interior part still unplaced (no group, not in ungrouped list)
        for nm in self.INTERIOR:
            if nm not in pos:
                if not self.greedy_place(nm, rects, cands, pos, part_rect=part_rect):
                    return None
        # 4) local refinement
        if do_refine:
            movable = [nm for nm in self.INTERIOR if nm in part_rect]
            self.refine(rects, cands, pos, part_rect, movable)
        return pos

    # --------------------------------------------------------------- metrics
    def total_hpwl(self, pos, exclude_planes=False):
        tot = 0.0
        for net, parts in self.NET_PARTS.items():
            if exclude_planes and net in self.PLANE_NETS:
                continue
            pts = [self.ecenter(p, pos) for p in parts if p in pos]
            if len(pts) >= 2:
                xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
                tot += (max(xs) - min(xs)) + (max(ys) - min(ys))
        return tot

    def placed_rects(self, pos):
        """Tight (no-gap) bbox rect per placed part, for overlap/gap metrics."""
        out = {}
        for nm, (x, y, rot) in pos.items():
            a, b, c, d = self.fr(nm, rot)
            out[nm] = (x + a, y + b, x + c, y + d)
        return out

    def count_overlaps(self, pos):
        """Number of unordered part pairs whose tight bboxes physically intersect."""
        rs = list(self.placed_rects(pos).items())
        n = 0
        for i in range(len(rs)):
            x0, y0, x1, y1 = rs[i][1]
            for j in range(i + 1, len(rs)):
                a0, b0, a1, b1 = rs[j][1]
                if x0 < a1 - 1e-6 and a0 < x1 - 1e-6 and y0 < b1 - 1e-6 and b0 < y1 - 1e-6:
                    n += 1
        return n

    def min_gap(self, pos):
        """Smallest edge-to-edge clearance between any two part bboxes (mm)."""
        rs = list(self.placed_rects(pos).values())
        best = float("inf")
        for i in range(len(rs)):
            x0, y0, x1, y1 = rs[i]
            for j in range(i + 1, len(rs)):
                a0, b0, a1, b1 = rs[j]
                dx = max(a0 - x1, x0 - a1, 0.0)        # horizontal gap (0 if overlapping in x)
                dy = max(b0 - y1, y0 - b1, 0.0)
                if dx == 0 and dy == 0:
                    d = 0.0                            # overlapping
                elif dx == 0:
                    d = dy
                elif dy == 0:
                    d = dx
                else:
                    d = math.hypot(dx, dy)
                best = min(best, d)
        return best if best != float("inf") else 0.0

    def used_extent(self, pos):
        """Bounding box (x0,y0,x1,y1) of all placed part bboxes -> packed footprint."""
        xs0, ys0, xs1, ys1 = [], [], [], []
        for r in self.placed_rects(pos).values():
            xs0.append(r[0]); ys0.append(r[1]); xs1.append(r[2]); ys1.append(r[3])
        return (min(xs0), min(ys0), max(xs1), max(ys1))

    def within_board(self, pos):
        """All part bboxes inside the board outline (respecting margin)? -> bool."""
        m, W, H = self.MARGIN, self.W, self.H
        for r in self.placed_rects(pos).values():
            if r[0] < m - 1e-6 or r[1] < m - 1e-6 or r[2] > W - m + 1e-6 or r[3] > H - m + 1e-6:
                return False
        return True


# ============================================================ CLI / reporting
def run(spec, do_refine=True):
    pl = Placer(spec)
    pos = pl.solve(do_refine=do_refine)
    if pos is None:
        return pl, None, None
    placements = [{"name": nm, "x": round(pos[nm][0], 4),
                   "y": round(pos[nm][1], 4), "rot": pos[nm][2]}
                  for nm in pl.part_order if nm in pos]
    metrics = dict(
        board_w=pl.W, board_h=pl.H, board_area_cm2=round(pl.W * pl.H / 100.0, 2),
        used_extent=[round(v, 3) for v in pl.used_extent(pos)],
        hpwl=round(pl.total_hpwl(pos), 3),
        hpwl_signal=round(pl.total_hpwl(pos, exclude_planes=True), 3),
        min_gap=round(pl.min_gap(pos), 4),
        overlaps=pl.count_overlaps(pos),
        within_board=pl.within_board(pos),
        n_parts=len(placements),
    )
    ux = pl.used_extent(pos)
    metrics["packed_area_cm2"] = round((ux[2] - ux[0]) * (ux[3] - ux[1]) / 100.0, 2)
    return pl, placements, metrics


def text_summary(placements, metrics):
    if placements is None:
        return "PLACEMENT FAILED: no feasible layout for the given board/parts."
    lines = []
    lines.append(f"board       : {metrics['board_w']:.1f} x {metrics['board_h']:.1f} mm "
                 f"({metrics['board_area_cm2']:.2f} cm^2)")
    ux = metrics["used_extent"]
    lines.append(f"packed bbox : x[{ux[0]:.2f},{ux[2]:.2f}] y[{ux[1]:.2f},{ux[3]:.2f}]  "
                 f"({metrics['packed_area_cm2']:.2f} cm^2)")
    lines.append(f"parts       : {metrics['n_parts']}")
    lines.append(f"HPWL total  : {metrics['hpwl']:.2f} mm   "
                 f"(signal-only {metrics['hpwl_signal']:.2f} mm)")
    lines.append(f"min gap     : {metrics['min_gap']:.3f} mm")
    lines.append(f"overlaps    : {metrics['overlaps']}")
    lines.append(f"within board: {'yes' if metrics['within_board'] else 'NO'}")
    lines.append("")
    lines.append(f"{'NAME':<12} {'X':>9} {'Y':>9} {'ROT':>5}")
    for p in placements:
        lines.append(f"{p['name']:<12} {p['x']:>9.3f} {p['y']:>9.3f} {p['rot']:>5}")
    return "\n".join(lines)


def demo_spec():
    """A small synthetic board: one MCU + decoupling caps + 2 edge connectors,
    wired with a handful of signal nets and a shared GND plane net."""
    return {
        "board": {"w": 30.0, "h": 26.0, "corner_holes": True},
        "parts": [
            {"name": "U1",  "w": 7.0, "h": 7.0, "group": "U1"},
            {"name": "C1",  "w": 1.6, "h": 0.8, "group": "U1"},
            {"name": "C2",  "w": 1.6, "h": 0.8, "group": "U1"},
            {"name": "C3",  "w": 2.0, "h": 1.25, "group": "U1"},
            {"name": "R1",  "w": 1.6, "h": 0.8},
            {"name": "R2",  "w": 1.6, "h": 0.8},
            {"name": "J1",  "w": 8.0, "h": 5.0, "fixed_edge": "bottom", "rot_ok": False},
            {"name": "J2",  "w": 6.0, "h": 4.0, "fixed_edge": "right",  "rot_ok": False},
        ],
        "nets": [
            {"name": "SDA",  "parts": ["U1", "J1", "R1"]},
            {"name": "SCL",  "parts": ["U1", "J1", "R2"]},
            {"name": "TX",   "parts": ["U1", "J2"]},
            {"name": "RX",   "parts": ["U1", "J2"]},
            {"name": "VCC",  "parts": ["U1", "C1", "C2", "C3", "J1", "J2"]},
            {"name": "GND",  "parts": ["U1", "C1", "C2", "C3", "R1", "R2", "J1", "J2"]},
        ],
        "groups": {"U1": ["C1", "C2", "C3"]},
        "plane_nets": ["GND", "VCC"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Spec-driven PCB component placer (HPWL + BLF).")
    ap.add_argument("spec", nargs="?", help="path to a JSON placement spec")
    ap.add_argument("--demo", action="store_true", help="run the built-in synthetic spec")
    ap.add_argument("--text", action="store_true", help="print a human-readable summary")
    ap.add_argument("-o", "--out", help="write placements JSON to this path")
    ap.add_argument("--no-refine", action="store_true", help="skip local refinement pass")
    args = ap.parse_args(argv)

    if args.demo:
        spec = demo_spec()
    elif args.spec:
        with open(args.spec) as f:
            spec = json.load(f)
    else:
        ap.error("provide a SPEC path or --demo")

    pl, placements, metrics = run(spec, do_refine=not args.no_refine)

    if args.text or not args.out:
        print(text_summary(placements, metrics))

    if args.out:
        if placements is None:
            print("PLACEMENT FAILED; nothing written.", file=sys.stderr)
            return 1
        with open(args.out, "w") as f:
            json.dump({"placements": placements, "metrics": metrics}, f, indent=2)
        if not args.text:
            print(f"wrote {len(placements)} placements -> {args.out}")

    if placements is None:
        return 1
    return 0 if metrics["overlaps"] == 0 and metrics["within_board"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
