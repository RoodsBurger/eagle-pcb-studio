#!/usr/bin/env python3
"""Protocol tests for scripts/fusion_mcp.py against a mock streamable-HTTP MCP server (no Fusion needed)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

STATE = {"sessions": set(), "calls": [], "expired": set()}


class MockMCP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, sid=None, sse=False):
        body = (f"event: message\ndata: {json.dumps(obj)}\n\n" if sse else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sid = self.headers.get("Mcp-Session-Id")
        method = req.get("method")
        if method == "initialize":
            new = f"sess-{len(STATE['sessions']) + 1}"
            STATE["sessions"].add(new)
            return self._send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2025-03-26"}}, sid=new)
        if method == "notifications/initialized":
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
        if sid not in STATE["sessions"] or sid in STATE["expired"]:
            return self._send(404, {"error": "session not found"})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {"tools": [
                {"name": "fusion_mcp_read", "description": "read", "inputSchema": {"properties": {"queryType": {}}}}]}}, sse=True)
        if method == "tools/call":
            p = req["params"]; STATE["calls"].append(p)
            if p["name"] == "boom":
                return self._send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {"isError": True, "content": [{"type": "text", "text": "kaboom"}]}})
            if p["name"] == "fusion_mcp_execute":
                return self._send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [
                    {"type": "text", "text": json.dumps({"message": "line1\n" + json.dumps({"finished": True})})}]}}, sse=True)
            return self._send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps({"echo": p["arguments"]})}]}})
        return self._send(200, {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "no such method"}})


class FusionMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), MockMCP)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True); cls.thread.start()
        cls.tmp = tempfile.mkdtemp()
        os.environ["FUSION_MCP_URL"] = f"http://127.0.0.1:{cls.srv.server_port}/mcp"
        os.environ["FUSION_MCP_SESSION"] = os.path.join(cls.tmp, "sess")
        import fusion_mcp
        cls.F = fusion_mcp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_session_cached_and_tools_listed_over_sse(self):
        tools = self.F.tools()
        self.assertEqual(tools[0]["name"], "fusion_mcp_read")
        self.assertIn(open(os.environ["FUSION_MCP_SESSION"]).read(), STATE["sessions"])

    def test_tool_parses_json_text_and_passes_arguments(self):
        out = self.F.read("document", operation="open")
        self.assertEqual(out, {"echo": {"queryType": "document", "operation": "open"}})

    def test_tool_error_raises(self):
        with self.assertRaises(self.F.FusionError):
            self.F.tool("boom", {})

    def test_script_returns_message_and_last_json_parses(self):
        msg = self.F.script("def run(_c): pass", read_only=True)
        self.assertTrue(msg.startswith("line1"))
        self.assertEqual(self.F._last_json(msg), {"finished": True})
        self.assertEqual(STATE["calls"][-1]["arguments"]["object"]["readOnly"], True)

    def test_expired_session_is_reinitialized(self):
        self.F.tools()
        old = open(os.environ["FUSION_MCP_SESSION"]).read()
        STATE["expired"].add(old)
        out = self.F.read("activeCommand")
        self.assertEqual(out, {"echo": {"queryType": "activeCommand"}})
        self.assertNotEqual(open(os.environ["FUSION_MCP_SESSION"]).read(), old)

    def test_unreachable_server_gives_hint(self):
        old = self.F.URL
        self.F.URL = "http://127.0.0.1:1/mcp"
        try:
            with self.assertRaises(self.F.FusionError) as cm:
                self.F.tools()
        finally:
            self.F.URL = old
        self.assertIn("find-port", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
