"""A real, minimal MCP server over stdio - used by the test suite.

This is not a mock of AEGIS's client. It is an independent process speaking the
actual wire protocol, so the handshake, framing and tool call in the tests are
the real thing rather than a stubbed method call.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "list_notes",
        "description": "List the notes. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_note",
        "description": "Delete a note by id. Destructive.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"destructiveHint": True},
    },
    {
        # No annotations and an unrecognised verb: must classify as write.
        "name": "frobnicate",
        "description": "Does something unclear.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "make_picture",
        "description": "Returns an image block, to exercise image handling.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# A 1x1 transparent PNG.
PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYA"
         "AjCB0C8AAAAASUVORK5CYII=")


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    # Servers commonly print a banner to stdout. The client must survive it.
    sys.stdout.write("fake-mcp-server starting\n")
    sys.stdout.flush()

    # readline(), not `for line in sys.stdin` - iterating a pipe read-aheads and
    # blocks until EOF, which deadlocks a request/response protocol.
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue

        method = message.get("method")
        mid = message.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-notes", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            continue                                   # notification, no reply
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}

            if name == "list_notes":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "note-1\nnote-2"}]}})
            elif name == "delete_note":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text",
                                 "text": f"deleted {args.get('id', '?')}"}]}})
            elif name == "make_picture":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "here is a pixel"},
                                {"type": "image", "mimeType": "image/png",
                                 "data": PIXEL}]}})
            elif name == "frobnicate":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "frobnication failed"}],
                    "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": f"unknown tool {name}"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
