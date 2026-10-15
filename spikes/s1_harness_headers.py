"""Spike S1: does strands-harness forward custom HTTP headers to an MCP server?

Local, no AWS. Starts a tiny HTTP server that records request headers, then
loads it through the same path create_harness uses internally
(MCPClient.load_servers with a {"url", "headers"} config) and opens a session.

Run:  uv run --python 3.13 --with strands-harness --with mcp python spikes/s1_harness_headers.py
PASS = the server saw "Authorization: Bearer spike-token" (and "mcpServers" camelCase
config conversion in strands_harness.config keeps `headers`).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

SEEN: list[dict[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("method") == "initialize":
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "spike", "version": "0"}}}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()

    def do_GET(self):  # noqa: N802
        self.send_response(405)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_DELETE(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def main() -> int:
    from strands.tools.mcp import MCPClient
    from strands_harness.config import _python_mcp_servers

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/mcp"

    # camelCase config path used by harness configs, converted as create_harness does
    cfg = _python_mcp_servers({"ctf": {"url": url, "headers": {"Authorization": "Bearer spike-token"}}})
    print("converted config:", cfg)
    clients = MCPClient.load_servers(cfg, continue_on_error=True, prefix_with_server_name=True)
    try:
        with clients[0]:
            pass
    except Exception as exc:  # the mock server has no tools/list; the handshake is enough
        print("session ended with:", type(exc).__name__)
    auth = [h.get("authorization") for h in SEEN]
    print("authorization headers seen by server:", auth)
    ok = "Bearer spike-token" in auth
    print("S1 RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
