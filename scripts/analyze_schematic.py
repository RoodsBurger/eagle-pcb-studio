#!/usr/bin/env python3
"""Schematic correctness / ERC review of an EAGLE / Fusion Electronics .sch file.

Self-contained, stdlib-only. Parses EAGLE XML with ElementTree (read-only;
never rewrites the file, so the DOCTYPE is irrelevant here) and reports
findings with severity / id / message / recommendation.

Resolves each part-pin's electrical DIRECTION through the chain
part -> deviceset -> gate -> symbol -> pin, then runs ERC-style checks over the
netlist: floating nets, unconnected pins, miswired NC pins, missing component
values, duplicate part names, and power/supply sanity.

Usage:
    python3 analyze_schematic.py <sch> [--text] [-o out.json]
"""
import sys, os, json, re, argparse
import xml.etree.ElementTree as ET

# EAGLE pin directions. 'io' is the default when the attribute is omitted.
PIN_DIRECTIONS = ("in", "out", "io", "oc", "pwr", "pas", "hiz", "nc", "sup")
DEFAULT_DIR = "io"

# Directions that can source/drive a net (a power net wants at least one).
DRIVER_DIRS = ("out", "oc", "pwr", "sup", "hiz")

# Directions whose pin floating is a hard error vs a soft note.
HIGH_SEV_DIRS = ("in", "pwr", "sup")        # an unconnected input/supply is bad
LOW_SEV_DIRS = ("pas", "nc")                # passives/NC floating is low-priority

# Refdes / deviceset-prefix families whose part spec lives in the value field.
# Passives are meaningless without a value; everything else (ICs, connectors,
# diodes, FETs, switches, test points, crystals-by-MPN) legitimately may not
# carry a value because the deviceset/MPN already identifies the part.
VALUE_REQUIRED_PREFIXES = ("R", "C", "L", "F")

# Net names that denote power/ground rails (matched case-insensitively, exact).
POWER_NET_NAMES = ("VCC", "VDD", "3V3", "5V", "VBUS", "GND", "VSS")
# Extra fragments that strongly imply a rail (substring, case-insensitive).
POWER_NET_HINTS = ("VCC", "VDD", "VSS", "VBUS", "3V3", "P3V3", "P24", "VMOT",
                   "VIN", "VBAT")

# Auto-generated net-name pattern (EAGLE assigns N$1, N$2, ... to unnamed nets).
AUTO_NET_RE = re.compile(r"^N\$\d+$")


# --------------------------------------------------------------------------
# library model: resolve part-pin -> direction
# --------------------------------------------------------------------------
def build_library_index(root):
    """Index every library's symbols and devicesets.

    Returns (symbols, devicesets):
      symbols[(lib, sym)] = {pin_name: direction}
      devicesets[(lib, ds)] = {
        "prefix": str, "uservalue": str,
        "gates": {gate_name: symbol_name},
        "devices": {device_name: {(gate, pin): set(pads)}},
      }
    """
    symbols = {}
    devicesets = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name")
        for sym in lib.iter("symbol"):
            pins = {}
            for pin in sym.findall("pin"):
                pins[pin.get("name")] = pin.get("direction", DEFAULT_DIR)
            symbols[(lib_name, sym.get("name"))] = pins
        for ds in lib.iter("deviceset"):
            gates = {}
            gnode = ds.find("gates")
            if gnode is not None:
                for g in gnode.findall("gate"):
                    gates[g.get("name")] = g.get("symbol")
            devices = {}
            dnode = ds.find("devices")
            if dnode is not None:
                for dev in dnode.findall("device"):
                    conns = {}
                    cnode = dev.find("connects")
                    if cnode is not None:
                        for c in cnode.findall("connect"):
                            key = (c.get("gate"), c.get("pin"))
                            pads = set((c.get("pad") or "").split())
                            conns[key] = pads
                    devices[dev.get("name", "")] = conns
            devicesets[(lib_name, ds.get("name"))] = {
                "prefix": ds.get("prefix", ""),
                "uservalue": ds.get("uservalue", ""),
                "gates": gates,
                "devices": devices,
            }
    return symbols, devicesets


def part_pins(part, symbols, devicesets):
    """All schematic pins of a part as {(gate, pin): direction}.

    Resolves part -> deviceset -> each gate -> symbol -> pins. Unresolved links
    (missing library/deviceset/symbol) yield an empty map; the caller flags it.
    """
    key = (part.get("library"), part.get("deviceset"))
    ds = devicesets.get(key)
    if ds is None:
        return {}
    out = {}
    for gate_name, sym_name in ds["gates"].items():
        pins = symbols.get((part.get("library"), sym_name), {})
        for pin_name, direction in pins.items():
            out[(gate_name, pin_name)] = direction
    return out


# --------------------------------------------------------------------------
# netlist model
# --------------------------------------------------------------------------
def collect_nets(root):
    """Map net-name -> list of (part, gate, pin) pinrefs across all sheets.

    Each net may span multiple <segment>s and multiple sheets; pinrefs are
    accumulated. A pin appearing in two segments of the same net counts once.
    """
    nets = {}
    for net in root.iter("net"):
        name = net.get("name")
        refs = nets.setdefault(name, [])
        seen = set()
        for seg in net.findall("segment"):
            for pr in seg.findall("pinref"):
                tup = (pr.get("part"), pr.get("gate"), pr.get("pin"))
                if tup not in seen:
                    seen.add(tup)
                    refs.append(tup)
    return nets


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def is_value_required(prefix, refdes):
    """True if this part's value field carries its essential spec.

    Prefer the deviceset prefix; fall back to the refdes leading letters.
    """
    p = (prefix or "").upper()
    if not p:
        m = re.match(r"^([A-Za-z]+)", refdes or "")
        p = m.group(1).upper() if m else ""
    # Take the leading alpha run so 'SW'/'TP' don't false-match 'S'/'T'.
    lead = re.match(r"^[A-Z]+", p)
    p = lead.group(0) if lead else p
    return p in VALUE_REQUIRED_PREFIXES


def is_power_net(name):
    """True if a net name denotes a power/ground rail."""
    if not name:
        return False
    up = name.upper()
    if up in POWER_NET_NAMES:
        return True
    return any(h in up for h in POWER_NET_HINTS)


def analyze(sch_path):
    root = ET.parse(sch_path).getroot()
    findings = []

    def add(sev, fid, msg, rec=""):
        findings.append({"severity": sev, "id": fid, "message": msg,
                         "recommendation": rec})

    symbols, devicesets = build_library_index(root)

    # ---- parts ----------------------------------------------------------
    parts = list(root.iter("part"))
    part_by_name = {}
    dup_names = []
    for p in parts:
        nm = p.get("name")
        if nm in part_by_name:
            dup_names.append(nm)
        else:
            part_by_name[nm] = p

    # Every schematic pin of every placed part: {(part, gate, pin): direction}.
    all_pins = {}
    unresolved_parts = []
    for nm, p in part_by_name.items():
        pins = part_pins(p, symbols, devicesets)
        if not pins:
            unresolved_parts.append(nm)
        for (g, pn), d in pins.items():
            all_pins[(nm, g, pn)] = d

    # ---- nets -----------------------------------------------------------
    nets = collect_nets(root)
    connected_pins = set()
    for refs in nets.values():
        for tup in refs:
            connected_pins.add(tup)

    auto_nets = [n for n in nets if AUTO_NET_RE.match(n or "")]
    total_pinrefs = sum(len(r) for r in nets.values())

    # ---- duplicate part names ------------------------------------------
    if dup_names:
        uniq = sorted(set(dup_names))
        add("ERROR", "parts.duplicate_name",
            f"{len(uniq)} duplicate part name(s): {', '.join(uniq)}.",
            "Each part needs a unique reference designator; rename the clashes.")

    # ---- unresolved devicesets -----------------------------------------
    if unresolved_parts:
        add("WARNING", "parts.unresolved_deviceset",
            f"{len(unresolved_parts)} part(s) reference a deviceset/symbol that "
            f"could not be resolved: {', '.join(sorted(unresolved_parts))}.",
            "Confirm the library/deviceset/symbol exist and are spelled correctly.")

    # ---- floating / single-connection nets -----------------------------
    floating = sorted(n for n, refs in nets.items() if len(refs) < 2)
    if floating:
        add("WARNING", "nets.floating",
            f"{len(floating)} net(s) touch fewer than 2 pins (floating / "
            f"single-connection): {', '.join(floating)}.",
            "A net needs >=2 pins to carry signal; delete the stub or wire it up.")

    # ---- unconnected pins on placed parts ------------------------------
    unconnected = [k for k in all_pins if k not in connected_pins]
    high, low, mid = [], [], []
    for k in unconnected:
        d = all_pins[k]
        label = f"{k[0]}.{k[2]}"
        if d in HIGH_SEV_DIRS:
            high.append((label, d))
        elif d in LOW_SEV_DIRS:
            low.append((label, d))
        else:
            mid.append((label, d))
    if high:
        listing = ", ".join(f"{l} ({d})" for l, d in sorted(high))
        add("ERROR", "pins.unconnected_critical",
            f"{len(high)} input/power/supply pin(s) left unconnected: {listing}.",
            "Tie inputs/supplies to a net; floating in/pwr/sup pins misbehave.")
    if mid:
        listing = ", ".join(f"{l} ({d})" for l, d in sorted(mid))
        add("WARNING", "pins.unconnected",
            f"{len(mid)} signal pin(s) (io/out/oc/hiz) left unconnected: {listing}.",
            "Verify these outputs/bidirs are intentionally left open.")
    if low:
        listing = ", ".join(f"{l} ({d})" for l, d in sorted(low))
        add("INFO", "pins.unconnected_passive",
            f"{len(low)} passive/NC pin(s) left unconnected: {listing}.",
            "Often fine (spare pads, no-connects); confirm none should be wired.")

    # ---- NC pins that ARE connected ------------------------------------
    nc_connected = []
    for n, refs in nets.items():
        for tup in refs:
            if all_pins.get(tup) == "nc":
                nc_connected.append((f"{tup[0]}.{tup[2]}", n))
    if nc_connected:
        listing = ", ".join(f"{p} -> {n}" for p, n in sorted(nc_connected))
        add("WARNING", "pins.nc_connected",
            f"{len(nc_connected)} no-connect (nc) pin(s) are wired to a net: "
            f"{listing}.",
            "NC pins should be left floating; remove the connection or fix the "
            "symbol direction if the pin is actually usable.")

    # ---- missing / empty component values ------------------------------
    missing_value = []
    for nm, p in part_by_name.items():
        val = (p.get("value") or "").strip()
        if val:
            continue
        key = (p.get("library"), p.get("deviceset"))
        ds = devicesets.get(key, {})
        prefix = ds.get("prefix", "")
        if is_value_required(prefix, nm):
            missing_value.append(nm)
    if missing_value:
        add("WARNING", "parts.missing_value",
            f"{len(missing_value)} passive part(s) have no value: "
            f"{', '.join(sorted(missing_value))}.",
            "Set R/C/L/F values; a passive with no value can't be sourced/built.")

    # ---- power / supply sanity -----------------------------------------
    # Every pwr/sup pin should reach a net.
    pwr_floating = sorted(
        f"{k[0]}.{k[2]}" for k in unconnected
        if all_pins[k] in ("pwr", "sup"))
    if pwr_floating:
        add("ERROR", "power.pin_floating",
            f"{len(pwr_floating)} power/supply pin(s) not connected to any net: "
            f"{', '.join(pwr_floating)}.",
            "Connect every pwr/sup pin to its rail; an unpowered rail is fatal.")

    # Power-named nets that have no driving (out/oc/pwr/sup/hiz) pin.
    undriven = []
    for n, refs in nets.items():
        if not is_power_net(n) or len(refs) < 2:
            continue
        has_driver = any(all_pins.get(t) in DRIVER_DIRS for t in refs)
        if not has_driver:
            undriven.append(n)
    if undriven:
        add("WARNING", "power.no_driver",
            f"{len(undriven)} power-named net(s) have no driving supply/out pin "
            f"(only passive/input pins): {', '.join(sorted(undriven))}.",
            "Confirm each rail is fed by a regulator/connector pin marked pwr/sup/"
            "out; a board-edge supply pin may simply lack the right direction.")

    # ---- INFO counts ----------------------------------------------------
    add("INFO", "counts.summary",
        f"{len(part_by_name)} parts, {len(nets)} nets, {len(all_pins)} pins, "
        f"{len(auto_nets)} auto-named net(s) (N$n), "
        f"{total_pinrefs} pin-to-net connections.",
        "")

    summary = {
        "schematic": os.path.basename(sch_path),
        "n_parts": len(part_by_name),
        "n_parts_raw": len(parts),
        "n_nets": len(nets),
        "n_pins": len(all_pins),
        "n_connections": total_pinrefs,
        "auto_named_nets": len(auto_nets),
        "floating_nets": len(floating),
        "unconnected_pins": len(unconnected),
        "unconnected_critical": len(high),
        "nc_connected": len(nc_connected),
        "missing_value_parts": len(missing_value),
        "duplicate_names": sorted(set(dup_names)),
        "unresolved_parts": sorted(unresolved_parts),
        "power_nets": sorted(n for n in nets if is_power_net(n)),
        "undriven_power_nets": sorted(undriven),
    }
    counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"summary": summary, "counts": counts, "findings": findings}


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------
def verdict(counts):
    if counts.get("ERROR", 0):
        return "FAIL"
    if counts.get("WARNING", 0):
        return "PASS (with warnings)"
    return "PASS"


def print_text(res):
    s = res["summary"]
    c = res["counts"]
    print("=" * 70)
    print(f"  SCHEMATIC ERC REVIEW  -  {s['schematic']}")
    print("=" * 70)
    print(f"  Parts        : {s['n_parts']} "
          f"({s['n_parts_raw']} <part> entries)")
    print(f"  Nets         : {s['n_nets']}  "
          f"({s['auto_named_nets']} auto-named N$n)")
    print(f"  Pins         : {s['n_pins']}  "
          f"({s['n_connections']} pin-to-net connections)")
    print(f"  Power nets   : {', '.join(s['power_nets']) or 'none'}")
    print(f"  Floating nets: {s['floating_nets']}")
    print(f"  Unconn pins  : {s['unconnected_pins']} "
          f"({s['unconnected_critical']} critical in/pwr/sup)")
    print(f"  NC wired     : {s['nc_connected']}")
    print(f"  Missing value: {s['missing_value_parts']} passive(s)")
    if s["duplicate_names"]:
        print(f"  Dup names    : {', '.join(s['duplicate_names'])}")
    if s["unresolved_parts"]:
        print(f"  Unresolved   : {', '.join(s['unresolved_parts'])}")
    print(f"  Findings     : {c.get('ERROR',0)} ERROR, "
          f"{c.get('WARNING',0)} WARNING, {c.get('INFO',0)} INFO")
    print("-" * 70)
    order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    marks = {"ERROR": "[X]", "WARNING": "[!]", "INFO": "[i]"}
    for f in sorted(res["findings"], key=lambda f: order.get(f["severity"], 9)):
        print(f"{marks.get(f['severity'],'[ ]')} {f['severity']:7s} {f['id']}")
        print(f"      {f['message']}")
        if f["recommendation"]:
            print(f"      -> {f['recommendation']}")
    print("-" * 70)
    print(f"  VERDICT: {verdict(c)}")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(
        description="EAGLE/Fusion .sch schematic correctness / ERC analyzer")
    ap.add_argument("sch", help="path to .sch file")
    ap.add_argument("--text", action="store_true", help="human-readable report")
    ap.add_argument("-o", "--output", help="write JSON findings to this path")
    args = ap.parse_args()

    if not os.path.isfile(args.sch):
        print(f"error: no such file: {args.sch}", file=sys.stderr)
        return 2
    res = analyze(args.sch)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
        print(f"wrote {args.output}")
    if args.text or not args.output:
        print_text(res)
    return 1 if res["counts"].get("ERROR", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
