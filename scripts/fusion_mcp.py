#!/usr/bin/env python3
"""Fusion 360 MCP client: drives the Fusion MCP add-in over streamable-HTTP JSON-RPC.

Covers the round-trip a PCB workflow needs: run Fusion API Python, open documents,
upload a .sch/.brd pair as one electronics design, read parts / nets / elements /
signals / ERC+DRC errors back, export EAGLE XML, take screenshots, and run the 3D
interference analysis on a pushed 3D PCB.

Fusion must be running with the MCP add-in started; the add-in listens on localhost.
Set FUSION_MCP_URL if the port differs (``fusion_mcp.py find-port`` lists candidates).

Library:   import fusion_mcp as F; F.tool("fusion_mcp_read", {"queryType": "document", "operation": "open"})
CLI:       python3 scripts/fusion_mcp.py tools | call <tool> '<json>' | script <file.py> | upload-pair <sch> <brd> --folder <id> | ...
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:52435/mcp"
URL = os.environ.get("FUSION_MCP_URL", DEFAULT_URL)
# MCP-Session-Id persists across CLI invocations so each command does not re-initialize.
SESSION_FILE = os.environ.get("FUSION_MCP_SESSION", os.path.join(tempfile.gettempdir(), "fusion_mcp_session"))
CLIENT_INFO = {"name": "eagle-pcb-studio", "version": "1"}
PROTOCOL = "2025-03-26"
_rpc_id = [1]


class FusionError(RuntimeError):
    """Raised for JSON-RPC errors, tool isError results, and unreachable add-ins."""


def _next_id():
    _rpc_id[0] += 1
    return _rpc_id[0]


def _post(payload, sid=None, timeout=600):
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        hdr["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            sid2 = r.headers.get("Mcp-Session-Id")
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.URLError as e:
        if isinstance(e, urllib.error.HTTPError):
            raise
        raise FusionError(f"cannot reach Fusion MCP add-in at {URL}: {e.reason}. Is Fusion running with the "
                          "MCP add-in started? Try `fusion_mcp.py find-port` and set FUSION_MCP_URL.") from e
    if "text/event-stream" in ctype:
        out = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    out = json.loads(line[5:].strip())
                except ValueError:
                    pass
        return out, sid2
    return (json.loads(body) if body.strip() else None), sid2


def session(fresh=False):
    """Return a cached MCP session id, initializing a new session when missing or `fresh`."""
    if not fresh and os.path.exists(SESSION_FILE):
        sid = open(SESSION_FILE).read().strip()
        if sid:
            return sid
    _, sid = _post({"jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    with open(SESSION_FILE, "w") as f:
        f.write(sid or "")
    return sid


def call(method, params=None, timeout=600):
    """Raw JSON-RPC call; an HTTP error (expired session) triggers one retry on a fresh session."""
    payload = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params or {}}
    try:
        res, _ = _post(payload, session(), timeout)
    except urllib.error.HTTPError:
        res, _ = _post(payload, session(fresh=True), timeout)
    if res is None:
        raise FusionError(f"empty response for {method}")
    if "error" in res:
        raise FusionError(res["error"])
    return res


def tools():
    """List the add-in's tools (name, description, input schema)."""
    return call("tools/list")["result"]["tools"]


def tool(name, args, timeout=600):
    """Call one MCP tool and return its text content parsed as JSON where possible."""
    res = call("tools/call", {"name": name, "arguments": args}, timeout)
    result = res.get("result", {})
    outs = []
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                outs.append(json.loads(c["text"]))
            except ValueError:
                outs.append(c["text"])
    if result.get("isError"):
        raise FusionError(outs)
    return outs[0] if len(outs) == 1 else outs


def read(query_type, **kw):
    """fusion_mcp_read: queryType is `document`, `screenshot` or `activeCommand`."""
    return tool("fusion_mcp_read", {"queryType": query_type, **kw})


def script(py, read_only=False, timeout=600):
    """Run Fusion API Python (a module defining run(context)) inside Fusion; returns its printed output."""
    r = tool("fusion_mcp_execute", {"featureType": "script", "object": {"script": py, "readOnly": read_only}}, timeout)
    return r.get("message", r) if isinstance(r, dict) else r


def script_file(path, replacements=None, read_only=False, prelude_paths=()):
    """Run a script file; `replacements` substitute placeholders, `prelude_paths` are concatenated in front."""
    src = "".join(open(p).read() + "\n" for p in prelude_paths) + open(path).read()
    for k, v in (replacements or {}).items():
        src = src.replace(k, v)
    return script(src, read_only=read_only)


def _last_json(text):
    """Parse the last printed line of a script's output as JSON."""
    lines = str(text).strip().splitlines()
    return json.loads(lines[-1]) if lines else None


def active_command():
    return read("activeCommand")


def wait_idle(timeout_s=600, poll_s=5):
    """Block until Fusion's active command is SelectCommand (no dialog or command running)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if "SelectCommand" in json.dumps(active_command()):
            return True
        time.sleep(poll_s)
    raise FusionError("Fusion stayed busy (a dialog or command is open)")


def open_doc(file_id):
    """Open a cloud document by data-file id and make it active."""
    return tool("fusion_mcp_execute", {"featureType": "document", "object": {"operation": "open", "fileId": file_id}})


def electronics_read(entity, fields=None, limit=1000, offset=0):
    """fusion_mcp_electronics_read on the active electronics document.

    Schematic entities: electronics.Part, electronics.Net, electronics.Error.
    Board entities: electronics.Element, electronics.Signal, electronics.Layer, electronics.Error.
    Coordinates come back in the unit the response states (`coordinate_unit`, usually inch).
    """
    obj = {"pagination": {"limit": limit, "offset": offset}}
    if fields:
        obj["fields"] = fields
    return tool("fusion_mcp_electronics_read", {"entity_type": entity, "object": obj})


def list_folder(folder_id):
    """List the data files in a cloud folder: name, ext, version, id, versionId."""
    py = '''
import adsk.core, json
def run(_context: str):
    app = adsk.core.Application.get()
    folder = app.data.findFolderById(%r)
    print(json.dumps([{"name": f.name, "ext": f.fileExtension, "ver": f.versionNumber, "id": f.id, "versionId": f.versionId} for f in folder.dataFiles]))
''' % folder_id
    return _last_json(script(py, read_only=True))


def upload(local_path, folder_id, max_wait_s=300):
    """Upload one file into a cloud folder; pumps Fusion's event loop until the upload finishes."""
    py = f'''
import adsk.core, time, json
def run(_context: str):
    app = adsk.core.Application.get()
    folder = app.data.findFolderById({folder_id!r})
    fut = folder.uploadFile({os.path.abspath(local_path)!r})
    for _ in range({int(max_wait_s * 10)}):
        st = fut.uploadState
        if st == adsk.core.UploadStates.UploadFinished: break
        if st == adsk.core.UploadStates.UploadFailed: raise RuntimeError("upload failed")
        adsk.doEvents(); time.sleep(0.1)
    f = fut.dataFile
    print(json.dumps(dict(id=f.id, versionId=f.versionId, name=f.name, ext=f.fileExtension)))
'''
    return _last_json(script(py))


def upload_pair(sch_path, brd_path, folder_id, tries=2, max_wait_s=600):
    """Upload a .sch + .brd pair as one electronics design (DataFolder.uploadAssembly).

    The pair must share a base name. A dropped connection during the long upload is retried
    unless the folder already gained a new board of that name.
    """
    sch_path, brd_path = os.path.abspath(sch_path), os.path.abspath(brd_path)
    name = os.path.splitext(os.path.basename(brd_path))[0]
    py = f'''
import adsk.core, json, time
def run(_context: str):
    app = adsk.core.Application.get()
    folder = app.data.findFolderById({folder_id!r})
    fut = folder.uploadAssembly([{sch_path!r}, {brd_path!r}])
    st = None
    for _ in range({int(max_wait_s * 10)}):
        st = fut.uploadState
        if st in (adsk.core.UploadStates.UploadFinished, adsk.core.UploadStates.UploadFailed): break
        adsk.doEvents(); time.sleep(0.1)
    print(json.dumps({{"finished": st == adsk.core.UploadStates.UploadFinished}}))
'''
    before = {(f["id"], f["ver"]) for f in list_folder(folder_id) if f["name"] == name and f["ext"] == "fbrd"}
    for attempt in range(tries):
        try:
            return _last_json(script(py))
        except Exception:
            time.sleep(20)
            fresh = [f for f in list_folder(folder_id)
                     if f["name"] == name and f["ext"] == "fbrd" and (f["id"], f["ver"]) not in before]
            if fresh:
                return {"finished": True, "note": "connection dropped, new board present", "fbrd": fresh[0]["id"]}
            if attempt == tries - 1:
                raise
    return {"finished": False}


def screenshot(png_path, width=1600, height=1600, direction="top"):
    """Save a viewport screenshot of the active document as PNG."""
    shot = read("screenshot", width=width, height=height, direction=direction)
    with open(png_path, "wb") as f:
        f.write(base64.b64decode(shot["base64Data"]))
    return png_path


def read_design(sch_id=None, brd_id=None, png_path=None, settle_s=6):
    """Open the schematic and/or board and read parts, nets, elements, signals, layers and ERC/DRC errors."""
    out = {}
    if sch_id:
        open_doc(sch_id)
        time.sleep(settle_s)
        out["parts"] = electronics_read("electronics.Part", ["name"])
        out["nets"] = electronics_read("electronics.Net", ["name"])
        out["errors_sch"] = electronics_read("electronics.Error")
    if brd_id:
        open_doc(brd_id)
        time.sleep(settle_s)
        out["elements"] = electronics_read("electronics.Element", ["name", "x", "y", "angle", "mirror"])
        out["signals"] = electronics_read("electronics.Signal", ["name"])
        out["errors_brd"] = electronics_read("electronics.Error")
        out["layers"] = electronics_read("electronics.Layer", ["number", "name", "used"])
        if png_path:
            screenshot(png_path)
    return out


def export_eagle(file_id, out_path, wait_s=60):
    """Open a Fusion board (.fbrd) or schematic (.fsch) and write it as EAGLE XML; `.brd` or `.sch` by extension."""
    out_path = os.path.abspath(out_path)
    kind = "Brd" if out_path.endswith(".brd") else "Sch"
    want = "Board" if kind == "Brd" else "Schematic"
    py = '''
import adsk.core, json, time
def run(_context: str):
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if not doc or doc.dataFile is None or doc.dataFile.id != %r:
        doc = app.documents.open(app.data.findFileById(%r), True)
    for _ in range(%d):
        adsk.doEvents(); time.sleep(0.1); prod = app.activeProduct; cur = app.activeDocument
        if prod and %r in prod.objectType and cur.dataFile and cur.dataFile.id == %r: break
    prod = app.activeProduct; em = prod.exportManager
    ok = em.execute(em.createEagle%sExportOptions(%r))
    print(json.dumps({"ok": bool(ok), "doc": app.activeDocument.name, "ver": app.activeDocument.dataFile.versionNumber, "type": prod.objectType}))
''' % (file_id, file_id, int(wait_s * 10), want, file_id, kind, out_path)
    r = script(py, read_only=True)
    try:
        return _last_json(r)
    except Exception:
        return {"raw": str(r)[:2000]}


def interference(file_id, wait_s=12):
    """Open a 3D PCB design (.f3d) and run Fusion's interference analysis: board bodies against every package.

    Hits are named by occurrence path and located by bounding box in mm, largest volume first.
    """
    py = '''
import adsk.core, adsk.fusion, json, time
def run(_context: str):
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if not doc or not isinstance(app.activeProduct, adsk.fusion.Design) or doc.dataFile is None or doc.dataFile.id != %r:
        doc = app.documents.open(app.data.findFileById(%r), True)
    for _ in range(%d):
        adsk.doEvents(); time.sleep(0.1); cur = app.activeDocument
        if isinstance(app.activeProduct, adsk.fusion.Design) and cur and cur.dataFile and cur.dataFile.id == doc.dataFile.id: break
    design = adsk.fusion.Design.cast(app.activeProduct); root = design.rootComponent
    coll = adsk.core.ObjectCollection.create(); names = []; tok = {}
    def reg(occ, path):
        for b in occ.bRepBodies: tok[b.nativeObject.entityToken if b.nativeObject else b.entityToken] = path + "/" + b.name
        for c in occ.childOccurrences: reg(c, path + "+" + c.name)
    for o in root.occurrences:
        if o.name.startswith("Board"): coll.add(o); names.append(o.name); reg(o, o.name)
        if o.name.startswith("Packages"):
            for p in o.childOccurrences:
                if p.bRepBodies.count > 0 or p.childOccurrences.count > 0: coll.add(p); names.append(p.name); reg(p, p.name)
    out = {"members": coll.count, "names": names}
    inp = design.createInterferenceInput(coll); inp.areCoincidentFacesIncluded = False
    res = design.analyzeInterference(inp)
    def nm(e):
        try: t = e.nativeObject.entityToken if e.nativeObject else e.entityToken
        except Exception: t = None
        try: return tok.get(t) or tok.get(e.entityToken) or (e.parentComponent.name + "/" + e.name)
        except Exception: return str(e)
    def bb(body):
        b = body.boundingBox
        return [round(v * 10, 2) for v in (b.minPoint.x, b.minPoint.y, b.minPoint.z, b.maxPoint.x, b.maxPoint.y, b.maxPoint.z)]
    hits = []
    for i in range(res.count if res else 0):
        r = res.item(i); h = {"a": nm(r.entityOne), "b": nm(r.entityTwo), "volume_mm3": None}
        try: h["volume_mm3"] = round(r.interferenceBody.volume * 1000.0, 4); h["bb_mm"] = bb(r.interferenceBody)
        except Exception: pass
        hits.append(h)
    out["interferences"] = sorted(hits, key=lambda h: -(h["volume_mm3"] or 0)) if res is not None else None
    print(json.dumps(out))
''' % (file_id, file_id, int(wait_s * 10))
    r = script(py)
    try:
        return _last_json(r)
    except Exception:
        return {"raw": str(r)[:2000]}


def find_port():
    """List localhost listeners that look like Fusion (macOS/Linux, via lsof) as candidate MCP URLs."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"lsof unavailable: {e}"}
    cands = []
    for line in out.splitlines():
        if "autodesk" in line.lower() or "fusion" in line.lower():
            port = line.rsplit(":", 1)[-1].split()[0]
            cands.append(f"http://127.0.0.1:{port}/mcp")
    return {"candidates": sorted(set(cands)), "default": DEFAULT_URL}


def _cli(argv=None):
    ap = argparse.ArgumentParser(description="Fusion 360 MCP client (set FUSION_MCP_URL if the add-in is not on the default port)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools", help="list the add-in's MCP tools")
    sub.add_parser("find-port", help="list candidate MCP URLs from local listeners")
    sub.add_parser("active-command", help="show Fusion's active command (SelectCommand = idle)")
    p = sub.add_parser("wait-idle", help="block until Fusion is idle"); p.add_argument("--timeout", type=float, default=600)
    p = sub.add_parser("call", help="call any tool with JSON args"); p.add_argument("tool"); p.add_argument("args", nargs="?", default="{}")
    p = sub.add_parser("read", help="fusion_mcp_read"); p.add_argument("query_type"); p.add_argument("args", nargs="?", default="{}")
    p = sub.add_parser("script", help="run a Fusion API Python file"); p.add_argument("file"); p.add_argument("--read-only", action="store_true")
    p.add_argument("--prelude", action="append", default=[], help="file(s) concatenated in front of the script")
    p.add_argument("--set", action="append", default=[], metavar="PLACEHOLDER=VALUE", help="literal text substitution before running")
    p = sub.add_parser("open", help="open a cloud document by file id"); p.add_argument("file_id")
    p = sub.add_parser("folder", help="list a cloud folder's data files"); p.add_argument("folder_id")
    p = sub.add_parser("upload", help="upload one file into a cloud folder"); p.add_argument("file"); p.add_argument("--folder", required=True)
    p = sub.add_parser("upload-pair", help="upload a .sch + .brd pair as one electronics design"); p.add_argument("sch"); p.add_argument("brd"); p.add_argument("--folder", required=True)
    p = sub.add_parser("electronics", help="read an electronics entity from the active document"); p.add_argument("entity"); p.add_argument("--fields", nargs="*"); p.add_argument("--limit", type=int, default=1000)
    p = sub.add_parser("read-design", help="open sch/brd by id and read parts, nets, elements, signals, errors, layers"); p.add_argument("--sch"); p.add_argument("--brd"); p.add_argument("--png")
    p = sub.add_parser("export", help="export a Fusion board/schematic to EAGLE .brd/.sch"); p.add_argument("file_id"); p.add_argument("out")
    p = sub.add_parser("screenshot", help="save a viewport screenshot"); p.add_argument("out"); p.add_argument("--direction", default="top"); p.add_argument("--size", type=int, default=1600)
    p = sub.add_parser("interference", help="run 3D interference on a pushed 3D PCB (.f3d id)"); p.add_argument("file_id")
    a = ap.parse_args(argv)
    if a.cmd == "tools":
        return [{"name": t["name"], "description": t.get("description", ""), "input": t.get("inputSchema", {}).get("properties", {})} for t in tools()]
    if a.cmd == "find-port":
        return find_port()
    if a.cmd == "active-command":
        return active_command()
    if a.cmd == "wait-idle":
        return {"idle": wait_idle(a.timeout)}
    if a.cmd == "call":
        return tool(a.tool, json.loads(a.args))
    if a.cmd == "read":
        return read(a.query_type, **json.loads(a.args))
    if a.cmd == "script":
        rep = dict(s.split("=", 1) for s in a.set)
        return {"output": script_file(a.file, rep, a.read_only, a.prelude)}
    if a.cmd == "open":
        return open_doc(a.file_id)
    if a.cmd == "folder":
        return list_folder(a.folder_id)
    if a.cmd == "upload":
        return upload(a.file, a.folder)
    if a.cmd == "upload-pair":
        return upload_pair(a.sch, a.brd, a.folder)
    if a.cmd == "electronics":
        return electronics_read(a.entity, a.fields, a.limit)
    if a.cmd == "read-design":
        return read_design(a.sch, a.brd, a.png)
    if a.cmd == "export":
        return export_eagle(a.file_id, a.out)
    if a.cmd == "screenshot":
        return {"png": screenshot(a.out, a.size, a.size, a.direction)}
    if a.cmd == "interference":
        return interference(a.file_id)
    return None


if __name__ == "__main__":
    try:
        print(json.dumps(_cli(), indent=1, default=str))
    except FusionError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
