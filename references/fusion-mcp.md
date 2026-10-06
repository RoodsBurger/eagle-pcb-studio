# Driving Fusion 360 over MCP

`scripts/fusion_mcp.py` is a client for the **Fusion MCP add-in** — the MCP server that runs inside
Autodesk Fusion 360 and exposes Fusion's Python API over streamable HTTP (JSON-RPC 2.0 on
`http://127.0.0.1:<port>/mcp`). Everything the skill does locally is file-based; this client is the
bridge for the parts of the loop that need Fusion itself: uploading a `.sch`/`.brd` pair as a
Fusion electronics design, reading ERC/DRC results back, exporting EAGLE XML of what Fusion holds,
taking screenshots, and running the 3D interference analysis on a pushed 3D PCB.

## Connecting

1. Start Fusion 360 and the MCP add-in (Utilities → Add-Ins). It listens on localhost; the default
   port in this client is `52435`.
2. If the port differs, find it and export the URL:
   ```bash
   python3 scripts/fusion_mcp.py find-port          # lists candidate URLs from local listeners
   export FUSION_MCP_URL=http://127.0.0.1:<port>/mcp
   ```
3. Smoke test: `python3 scripts/fusion_mcp.py tools` lists the add-in's tools. A connection-refused
   error means Fusion or the add-in is not running.

The client caches the `Mcp-Session-Id` in a temp file (`FUSION_MCP_SESSION` to override), so every
CLI call reuses one MCP session; an expired session (HTTP error) is re-initialized automatically.

## Tool surface the client wraps

| MCP tool | Arguments | Used for |
|---|---|---|
| `fusion_mcp_read` | `queryType: document` (`operation: open`) · `screenshot` (`width`, `height`, `direction`) · `activeCommand` | What is open, viewport PNG (base64), whether Fusion is idle (`SelectCommand`) or stuck in a dialog |
| `fusion_mcp_execute` | `featureType: script` (`object.script`, `object.readOnly`) · `featureType: document` (`object.operation: open`, `object.fileId`) | Run Fusion API Python (a module defining `run(context)`; `print` output comes back as `message`) · open a cloud document by id |
| `fusion_mcp_electronics_read` | `entity_type`, `object.fields`, `object.pagination {limit, offset}` | Schematic: `electronics.Part`, `electronics.Net`, `electronics.Error`. Board: `electronics.Element`, `electronics.Signal`, `electronics.Layer`, `electronics.Error`. Responses carry `items`, `pagination.hasMore` and `coordinate_unit` (inch) |

## Round-trip recipe (what Claude runs)

```bash
F="python3 scripts/fusion_mcp.py"
$F folder <folder-id>                                   # data files in the project folder: name, ext, ver, id
$F upload-pair out/design.sch out/design.brd --folder <folder-id>   # one electronics design (uploadAssembly)
$F folder <folder-id>                                   # pick up the new fsch/fbrd ids
$F read-design --sch <fsch-id> --brd <fbrd-id> --png fusion-board.png   # parts, nets, elements, signals, layers, ERC+DRC errors
$F export <fbrd-id> fusion-export.brd                   # EAGLE XML of what Fusion holds, for the local analyzers
python3 scripts/check_consistency.py out/design.sch fusion-export.brd
$F interference <f3d-id>                                # after the user pushes a 3D PCB: part/part and part/board collisions
```

Library use is the same surface: `import fusion_mcp as F; F.upload_pair(...)`, `F.electronics_read("electronics.Error")`,
`F.script(py_source, read_only=True)`.

Ad-hoc Fusion API scripts: `$F script my_script.py [--read-only] [--prelude lib.py] [--set __NAME__=value]`.
The add-in takes one script string, so a shared library is concatenated in front (`--prelude`), and
placeholders are plain text substitutions (`--set`).

## Gotchas (learned the hard way)

- **Uploads only progress between events.** Inside Fusion the upload future advances only while the
  event loop is pumped, so the upload scripts loop on `adsk.doEvents()` + `uploadState`.
- **Every upload is a new lineage.** The API cannot version an existing electronics design; `folder`
  before and after to find the new ids. Fusion may show an "Upload Options" dialog that needs a click.
- **Never re-upload an EAGLE export.** `export` writes valid EAGLE XML, but Fusion drops part of the
  `<packages3d>` data on export; re-uploading it loses 3D models. Export for *reading back*, keep the
  generator's files as the source of truth.
- **Scripts run to completion even if the client dies.** A timed-out `script` call may still have
  finished inside Fusion. Check state with a read-only script before re-running anything that edits.
- **Wait for idle before scripting.** `active-command` must be `SelectCommand`; `wait-idle` polls for it.
  A modal dialog blocks every script.
- **DRC errors come with a `signature`.** Store approved signatures (design-inherent hits such as a
  connector's locating-peg clearance) and diff against them instead of eyeballing the count.
- **Interference results are falsy when empty** in Python (`count` → `__len__`); the client tests
  `is None` and `count` separately. Two-entity sets that include a referenced occurrence can return
  `None`; analyze the full set.
- **Extrude cuts hit every intersecting body** unless `participantBodies` names the target; always set
  it in generated 3D scripts.
- **"Schematic/PCB sync deactivated: Run ERC"** appears after every import. One ERC run re-activates
  forward/back annotation. ERC consistency also compares device **technology attributes** (MF, MP,
  PACKAGE, PRICE, DESCRIPTION…), so a generated board must copy them onto its `<element>`s exactly as
  EAGLE does, or the pair reads as inconsistent.
- **Copper edits made in Fusion do not flow back to a generator.** Export the board, transplant the
  edited nets' wires/vias into the generator's files, regenerate, upload again. Fusion's route/optimize
  leaves tiny Y-stubs (0.03–1.5 mm) on plane-net pads: they pass DRC but should be dropped on import.
- **Upload stuck in state 0** means Fusion's Job Status queue is wedged — clear it. Do not run scripts
  while the user is routing. Deleted cloud files are unreachable (`findFileById` fails, there is no
  trash). Silk colour is a per-document Fusion setting: re-apply it after every upload.
- **DRC approvals can live in the board** as `<approved>` entries (by signature hash); "Drill Size"
  items on drills that equal the DRU minimum are notes to accept, or raise with `msDrill` 0.19 mm.
- **CAM must run as an N-layer job.** A 2-layer job on a 4-layer board drops the inner planes with no
  error. Check the zip for `copper_inner_l2` / `copper_inner_l3` (`analyze_gerbers.py --layers 4`).
- **3D interference:** exclude copper/soldermask bodies (the client collects Board + Packages bodies)
  or Fusion returns nothing. Known false hits: STEP pin retention kinks a few hundredths wider than
  their holes, the round barrel the 3D PCB draws for slot pads, SOT-23 / can STEP frames rotated 90°,
  library models attached below the board surface.
- **Managed-library package copies:** strip the Fusion-only `<polygonshape>` elements before importing
  into EAGLE 9, and apply a footprint fix to **every** copy of the package (board, schematic, managed
  cache) or it silently fails to land.
