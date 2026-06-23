#!/usr/bin/env python3
"""Resolve the footprint libraries (.lbr) an EAGLE/Fusion schematic needs.

For each <library> a .sch references, report which devicesets/packages the
schematic's parts actually use and whether that footprint geometry is already
EMBEDDED in the .sch (its <package>s contain real smd/pad/hole geometry). When a
library or a used package is missing/empty, SEARCH a set of locations for a
matching .lbr (by filename stem, internal <library name>, or the set of package
names it defines) and mark it found:<path> or MISSING.

Read-only: parses with ElementTree, never rewrites the file (DOCTYPE preserved by
not touching it). Exits non-zero if any library is MISSING, so it works as a gate.

Usage:
    python3 find_libraries.py <sch> [--search DIR]... [--text] [-o out.json]
"""
import sys, os, json, argparse
import xml.etree.ElementTree as ET


# Package children that constitute real land-pattern geometry.
GEOM_TAGS = ("smd", "pad", "hole")


def has_geometry(pkg_el):
    """True if a <package> carries actual pad/hole copper (not an empty stub)."""
    return any(c.tag in GEOM_TAGS for c in pkg_el)


def parse_lib_packages(path):
    """Map deviceset-name -> set(package-name) and package-name -> <package> el.

    Walks devicesets to recover which physical package each deviceset's devices
    point at; returns both that map and the embedded package elements.
    """
    root = ET.parse(path).getroot()
    libs = {}
    for lib in root.iter("library"):
        name = lib.get("name")
        pkg_els = {}
        pkgs = lib.find("packages")
        if pkgs is not None:
            for pk in pkgs.findall("package"):
                pkg_els[pk.get("name")] = pk
        ds2pkgs = {}
        dss = lib.find("devicesets")
        if dss is not None:
            for ds in dss.findall("deviceset"):
                used = set()
                for dev in ds.iter("device"):
                    pk = dev.get("package")
                    if pk:
                        used.add(pk)
                ds2pkgs[ds.get("name")] = used
        libs[name] = {"pkg_els": pkg_els, "ds2pkgs": ds2pkgs}
    return libs


def schematic_usage(sch_path):
    """Per referenced library, the devicesets and packages its parts pull in.

    Returns {libname: {"devicesets": {ds: set(pkgs_used)}, "packages": set,
    "pkg_els": {name: el}, "embedded_libname": str}}.
    """
    libdata = parse_lib_packages(sch_path)
    root = ET.parse(sch_path).getroot()

    usage = {}
    for part in root.iter("part"):
        lib = part.get("library")
        ds = part.get("deviceset")
        dev = part.get("device")
        if lib is None:
            continue
        entry = usage.setdefault(
            lib, {"devicesets": {}, "packages": set(), "device_pkg_overrides": {}}
        )
        # Resolve this part's package via the embedded deviceset's device map.
        lib_info = libdata.get(lib, {})
        ds2pkgs = lib_info.get("ds2pkgs", {})
        pkgs_for_ds = set()
        dss_el = None
        # Prefer the exact device (when named) over the whole deviceset's set.
        if ds in ds2pkgs:
            pkgs_for_ds = set(ds2pkgs[ds])
        entry["devicesets"].setdefault(ds, set()).update(pkgs_for_ds)
        entry["packages"].update(pkgs_for_ds)

    # Fold in the embedded package elements for each referenced library.
    for lib, entry in usage.items():
        lib_info = libdata.get(lib, {})
        entry["pkg_els"] = lib_info.get("pkg_els", {})
    return usage


# --- Library search -------------------------------------------------------

def candidate_search_dirs(sch_path, extra_dirs):
    """Ordered, de-duplicated list of directories to scan for .lbr files."""
    dirs = []
    seen = set()

    def add(d):
        if not d:
            return
        ad = os.path.abspath(d)
        if ad not in seen and os.path.isdir(ad):
            seen.add(ad)
            dirs.append(ad)

    # Explicit --search dirs first.
    for d in extra_dirs or []:
        add(d)

    sch_dir = os.path.dirname(os.path.abspath(sch_path))
    add(sch_dir)
    add(os.path.join(sch_dir, "components"))

    # Walk up a few parent levels looking for components/ siblings.
    cur = sch_dir
    for _ in range(4):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        add(parent)
        add(os.path.join(parent, "components"))
        cur = parent

    # Common EAGLE / Fusion library roots, if present.
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Documents", "EAGLE", "libraries"),
        os.path.join(home, "EAGLE", "lbr"),
        os.path.join(home, ".local", "share", "eagle"),
    ]
    import glob
    for pat in ("/Applications/Autodesk/EAGLE*/lbr",):
        roots.extend(glob.glob(pat))
    for r in roots:
        add(r)
    return dirs


def index_lbr_files(dirs):
    """Find .lbr files in the given dirs; return list of (path, stem, libname,
    set(package-names)). Shallow scan per dir plus one components/ recursion."""
    found = []
    seen_paths = set()
    for d in dirs:
        for entry in sorted(os.listdir(d)):
            if not entry.lower().endswith(".lbr"):
                continue
            path = os.path.join(d, entry)
            ap = os.path.abspath(path)
            if ap in seen_paths or not os.path.isfile(ap):
                continue
            seen_paths.add(ap)
            stem = os.path.splitext(entry)[0]
            libname, pkgnames = read_lbr_meta(ap)
            found.append((ap, stem, libname, pkgnames))
    return found


def read_lbr_meta(path):
    """Return (internal <library name>, set(package-names)) for an .lbr, or
    (None, set()) if it cannot be parsed."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None, set()
    libname = None
    pkgnames = set()
    lib = None
    for el in root.iter("library"):
        lib = el
        break
    if lib is None:
        return None, set()
    libname = lib.get("name")
    pkgs = lib.find("packages")
    if pkgs is not None:
        for pk in pkgs.findall("package"):
            if pk.get("name"):
                pkgnames.add(pk.get("name"))
    return libname, pkgnames


def match_lbr(lib_name, needed_pkgs, lbr_index):
    """Pick the best .lbr for a library by stem, internal name, or package set.

    Returns the matching .lbr path or None. Preference order:
      1. .lbr whose internal <library name> equals lib_name.
      2. .lbr whose filename stem equals lib_name.
      3. .lbr that supplies every needed package name.
      4. .lbr that supplies at least one needed package name.
    """
    needed = set(p for p in needed_pkgs if p)

    by_internal = [c for c in lbr_index if c[2] and c[2] == lib_name]
    if by_internal:
        return by_internal[0][0]

    by_stem = [c for c in lbr_index if c[1] == lib_name]
    if by_stem:
        return by_stem[0][0]

    if needed:
        full = [c for c in lbr_index if needed.issubset(c[3])]
        if full:
            return full[0][0]
        partial = [c for c in lbr_index if needed & c[3]]
        if partial:
            return partial[0][0]
    return None


# --- Analysis -------------------------------------------------------------

def analyze(sch_path, search_dirs):
    """Resolve every referenced library's status; return a result dict."""
    usage = schematic_usage(sch_path)
    dirs = candidate_search_dirs(sch_path, search_dirs)
    lbr_index = index_lbr_files(dirs)

    libraries = []
    n_embedded = n_found = n_missing = 0

    for lib_name in sorted(usage.keys(), key=lambda s: (s or "").lower()):
        entry = usage[lib_name]
        pkg_els = entry["pkg_els"]
        used_pkgs = set(entry["packages"])
        # If a deviceset resolved no package, still note its name for the report.
        devicesets = sorted(entry["devicesets"].keys(), key=lambda s: s or "")

        # Determine, per used package, whether embedded geometry is present.
        if used_pkgs:
            embedded_ok = all(
                (p in pkg_els and has_geometry(pkg_els[p])) for p in used_pkgs
            )
            missing_pkgs = sorted(
                p for p in used_pkgs
                if p not in pkg_els or not has_geometry(pkg_els[p])
            )
        else:
            # No package resolved from devicesets: fall back to any embedded geom.
            embedded_ok = any(has_geometry(el) for el in pkg_els.values())
            missing_pkgs = [] if embedded_ok else sorted(pkg_els.keys())

        rec = {
            "name": lib_name,
            "devicesets": devicesets,
            "packages_used": sorted(used_pkgs),
            "missing_packages": missing_pkgs,
        }

        if embedded_ok:
            rec["status"] = "embedded"
            rec["lbr"] = None
            n_embedded += 1
        else:
            lbr = match_lbr(lib_name, used_pkgs or set(pkg_els.keys()), lbr_index)
            if lbr:
                rec["status"] = "found"
                rec["lbr"] = lbr
                n_found += 1
            else:
                rec["status"] = "MISSING"
                rec["lbr"] = None
                n_missing += 1
        libraries.append(rec)

    return {
        "sch": os.path.abspath(sch_path),
        "search_dirs": dirs,
        "lbr_files_seen": [c[0] for c in lbr_index],
        "libraries": libraries,
        "summary": {
            "embedded": n_embedded,
            "found": n_found,
            "missing": n_missing,
            "total": len(libraries),
        },
        "pass": n_missing == 0,
    }


def print_text(res):
    """Human-readable per-library report ending in the summary line."""
    print("=== EAGLE/Fusion schematic library resolution ===")
    print(f"  sch: {res['sch']}")
    print(f"  searched {len(res['search_dirs'])} dir(s), "
          f"saw {len(res['lbr_files_seen'])} .lbr file(s)")
    print()
    for lib in res["libraries"]:
        status = lib["status"]
        if status == "embedded":
            tag = "[embedded]"
        elif status == "found":
            tag = "[found]   "
        else:
            tag = "[MISSING] "
        print(f"{tag} {lib['name']}")
        if lib["devicesets"]:
            print(f"    devicesets: {', '.join(lib['devicesets'])}")
        if lib["packages_used"]:
            print(f"    packages:   {', '.join(lib['packages_used'])}")
        if status == "found":
            print(f"    -> {lib['lbr']}")
        elif status == "MISSING":
            if lib["missing_packages"]:
                print(f"    missing geometry for: "
                      f"{', '.join(lib['missing_packages'])}")
            print("    -> no matching .lbr found on disk")
    print()
    s = res["summary"]
    print(f"SUMMARY: {s['total']} libraries "
          f"({s['embedded']} embedded, {s['found']} found on disk, "
          f"{s['missing']} missing)")
    print(f"VERDICT: {'PASS' if res['pass'] else 'FAIL'}")


def main(argv):
    ap = argparse.ArgumentParser(
        description="Resolve the footprint libraries (.lbr) an EAGLE/Fusion "
                    "schematic needs."
    )
    ap.add_argument("sch", help="path to .sch")
    ap.add_argument(
        "--search", action="append", default=[], metavar="DIR",
        help="extra directory to search for .lbr files (repeatable)",
    )
    ap.add_argument("-o", "--output", help="write JSON result to this path")
    ap.add_argument("--text", action="store_true",
                    help="print human summary (default)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.sch):
        print(f"error: no such file: {args.sch}", file=sys.stderr)
        return 2

    res = analyze(args.sch, args.search)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
        print(f"wrote {args.output}")
    if args.text or not args.output:
        print_text(res)

    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
