#!/usr/bin/env python3
"""Verify a Fusion/EAGLE schematic (.sch) and board (.brd) are consistent.

Catches the thing that drives Fusion ERC "Part X has inconsistent footprints in
schematic and board": a package whose geometry differs between the two files.
Also compares the netlist (parts/elements and nets/signals).

Usage:
  python3 check_consistency.py <sch> <brd> [--sync] [-o out.json] [--text]

--sync rewrites the .sch so every geometrically-differing shared package matches
the .brd (board is source of truth), splicing raw text (DOCTYPE preserved) and
backing up the .sch to <sch>.bak first.
"""
import sys, os, json, re, shutil, argparse
import xml.etree.ElementTree as ET


def isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def attrsig(e):
    """Layer-aware, rounded-coordinate signature of an element's attributes."""
    return frozenset(
        (k, round(float(v), 4) if isnum(v) else v) for k, v in e.attrib.items()
    )


def pkg_sig(pk):
    """Geometric signature of a <package>: shapes only, order-independent."""
    out = []
    for e in pk:
        if e.tag in ("smd", "pad", "hole", "wire", "rectangle", "circle"):
            out.append((e.tag, attrsig(e)))
        elif e.tag == "polygon":
            vs = frozenset(
                (
                    round(float(v.get("x")), 4),
                    round(float(v.get("y")), 4),
                    v.get("curve"),
                )
                for v in e.findall("vertex")
            )
            out.append(("polygon", e.get("layer"), vs))
    return frozenset(out)


def pkg_sigs(path):
    """Map package-name -> geometric signature for a file."""
    root = ET.parse(path).getroot()
    return {p.get("name"): pkg_sig(p) for p in root.iter("package")}


def pkg_block(raw, name):
    """Raw-text <package name="X">...</package> block, with its span."""
    m = re.search(
        r'<package name="' + re.escape(name) + r'".*?</package>', raw, re.DOTALL
    )
    return (m.group(0), m.start(), m.end()) if m else (None, None, None)


def netlist(sch_path, brd_path):
    """Parts (sch part / brd element) and nets (sch net / brd signal) sets."""
    sroot = ET.parse(sch_path).getroot()
    broot = ET.parse(brd_path).getroot()
    sparts = {p.get("name") for p in sroot.iter("part")}
    bparts = {e.get("name") for e in broot.iter("element")}
    snets = {n.get("name") for n in sroot.iter("net")}
    bsigs = {s.get("name") for s in broot.iter("signal")}
    return sparts, bparts, snets, bsigs


def analyze(sch_path, brd_path):
    """Run all consistency checks; return a result dict."""
    spk = pkg_sigs(sch_path)
    bpk = pkg_sigs(brd_path)
    shared = sorted(set(spk) & set(bpk))
    geom_diff = sorted(n for n in shared if spk[n] != bpk[n])
    sch_only_pkg = sorted(set(spk) - set(bpk))
    brd_only_pkg = sorted(set(bpk) - set(spk))

    sparts, bparts, snets, bsigs = netlist(sch_path, brd_path)
    parts_sch_only = sorted(sparts - bparts)
    parts_brd_only = sorted(bparts - sparts)
    nets_sch_only = sorted(snets - bsigs)
    nets_brd_only = sorted(bsigs - snets)

    geom_pass = not geom_diff and not sch_only_pkg and not brd_only_pkg
    parts_pass = not parts_sch_only and not parts_brd_only
    nets_pass = not nets_sch_only and not nets_brd_only

    return {
        "sch": os.path.abspath(sch_path),
        "brd": os.path.abspath(brd_path),
        "packages": {
            "sch_count": len(spk),
            "brd_count": len(bpk),
            "shared": len(shared),
            "geometrically_inconsistent": geom_diff,
            "sch_only": sch_only_pkg,
            "brd_only": brd_only_pkg,
            "pass": geom_pass,
        },
        "parts": {
            "sch_count": len(sparts),
            "brd_count": len(bparts),
            "sch_only": parts_sch_only,
            "brd_only": parts_brd_only,
            "pass": parts_pass,
        },
        "nets": {
            "sch_count": len(snets),
            "brd_count": len(bsigs),
            "sch_only": nets_sch_only,
            "brd_only": nets_brd_only,
            "pass": nets_pass,
        },
        "pass": geom_pass and parts_pass and nets_pass,
    }


def sync(sch_path, brd_path, geom_diff):
    """Replace each differing package block in .sch with the .brd's block."""
    if not geom_diff:
        return [], None
    bak = sch_path + ".bak"
    shutil.copy(sch_path, bak)
    with open(sch_path, encoding="utf-8") as f:
        sch_raw = f.read()
    with open(brd_path, encoding="utf-8") as f:
        brd_raw = f.read()
    replaced = []
    for name in geom_diff:
        bblock, _, _ = pkg_block(brd_raw, name)
        sblock, ss, se = pkg_block(sch_raw, name)
        if bblock is None or sblock is None:
            continue
        sch_raw = sch_raw[:ss] + bblock + sch_raw[se:]
        replaced.append(name)
    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(sch_raw)
    ET.fromstring(sch_raw)  # validate the rewritten file still parses
    return replaced, bak


def verdict(ok):
    return "PASS" if ok else "FAIL"


def print_text(res):
    p = res["packages"]
    pa = res["parts"]
    n = res["nets"]
    print("=== EAGLE/Fusion sch <-> brd consistency ===")
    print(f"  sch: {res['sch']}")
    print(f"  brd: {res['brd']}")
    print()
    print(
        f"[{verdict(p['pass'])}] PACKAGES  "
        f"sch={p['sch_count']} brd={p['brd_count']} shared={p['shared']}"
    )
    if p["geometrically_inconsistent"]:
        print(
            "    geometrically inconsistent (ERC errors): "
            + ", ".join(p["geometrically_inconsistent"])
        )
    else:
        print("    geometrically inconsistent (ERC errors): NONE")
    if p["sch_only"]:
        print("    in sch only: " + ", ".join(p["sch_only"]))
    if p["brd_only"]:
        print("    in brd only: " + ", ".join(p["brd_only"]))
    print()
    print(
        f"[{verdict(pa['pass'])}] PARTS     "
        f"sch={pa['sch_count']} brd={pa['brd_count']}"
    )
    if pa["sch_only"]:
        print("    in sch only: " + ", ".join(pa["sch_only"]))
    if pa["brd_only"]:
        print("    in brd only: " + ", ".join(pa["brd_only"]))
    if pa["pass"]:
        print("    diff: NONE")
    print()
    print(
        f"[{verdict(n['pass'])}] NETS      "
        f"sch={n['sch_count']} brd={n['brd_count']}"
    )
    if n["sch_only"]:
        print("    in sch only: " + ", ".join(n["sch_only"]))
    if n["brd_only"]:
        print("    in brd only: " + ", ".join(n["brd_only"]))
    if n["pass"]:
        print("    diff: NONE")
    print()
    print(f"VERDICT: {verdict(res['pass'])}")


def main(argv):
    ap = argparse.ArgumentParser(description="EAGLE/Fusion sch<->brd consistency check")
    ap.add_argument("sch", help="path to .sch")
    ap.add_argument("brd", help="path to .brd")
    ap.add_argument(
        "--sync",
        action="store_true",
        help="rewrite .sch differing packages from .brd (board is source of truth)",
    )
    ap.add_argument("-o", "--output", help="write JSON result to this path")
    ap.add_argument("--text", action="store_true", help="print human summary (default)")
    args = ap.parse_args(argv)

    for label, path in (("sch", args.sch), ("brd", args.brd)):
        if not os.path.isfile(path):
            print(f"error: {label} not found: {path}", file=sys.stderr)
            return 2

    res = analyze(args.sch, args.brd)

    if args.sync:
        geom_diff = res["packages"]["geometrically_inconsistent"]
        replaced, bak = sync(args.sch, args.brd, geom_diff)
        if replaced:
            print(f"--sync: backed up {args.sch} -> {bak}")
            print(f"--sync: replaced {len(replaced)} package(s): {', '.join(replaced)}")
            res = analyze(args.sch, args.brd)  # re-verify
            print("--sync: re-verified after rewrite")
        else:
            print("--sync: nothing to do (no geometrically-differing shared packages)")
        print()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
        print(f"wrote {args.output}")

    if args.text or not args.output:
        print_text(res)

    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
