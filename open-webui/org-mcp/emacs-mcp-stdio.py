#!/usr/bin/env python3
"""stdio MCP shim for a running Emacs daemon.

This replaces mcp-server-lib's bash shim for the sidecar.  The bash version
uses `set -e`, so one failed `emacsclient` invocation kills the bridge and
mcp-proxy is left with no upstream.  This version turns that failure into a
JSON-RPC error response and keeps serving.

Protocol: one JSON-RPC message per stdin line; one response line per request.
The Emacs side still runs `mcp-server-lib-process-jsonrpc`, so the shim does
not need to know anything about org-mcp.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import Any

DEFAULT_TIMEOUT = 300.0


def _log(message: str) -> None:
    path = os.environ.get("EMACS_MCP_DEBUG_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(message.rstrip("\n") + "\n")


def _emacsclient(args: argparse.Namespace, expression: str) -> str:
    command = ["emacsclient"]
    if args.socket:
        command.extend(["-s", args.socket])
    command.extend(["-e", expression])

    timeout = float(os.environ.get("EMACS_MCP_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"emacsclient timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot run emacsclient: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"emacsclient exited {completed.returncode}: {detail or 'no output'}"
        )
    return completed.stdout.strip()


def _call_emacs(args: argparse.Namespace, line: str) -> str | None:
    encoded = base64.b64encode(line.encode("utf-8")).decode("ascii")
    server_id = args.server_id.replace("\\", "\\\\").replace('"', '\\"')
    expression = (
        "(base64-encode-string "
        "(encode-coding-string "
        f'(or (mcp-server-lib-process-jsonrpc (base64-decode-string "{encoded}") '
        f'"{server_id}") "") '
        "'utf-8 t) t)"
    )
    output = _emacsclient(args, expression)
    if not output or output == "nil":
        return None
    if output.startswith('"') and output.endswith('"'):
        output = output[1:-1]
    output = output.replace('\\"', '"').replace("\\\\", "\\")
    return base64.b64decode(output).decode("utf-8")


def _error_response(request_id: Any, code: int, message: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        ensure_ascii=False,
    )


def _handle_line(args: argparse.Namespace, line: str) -> str | None:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        _log(f"parse error: {exc}")
        return _error_response(None, -32700, f"Parse error: {exc}")

    request_id = request.get("id") if isinstance(request, dict) else None
    try:
        response = _call_emacs(args, line)
    except Exception as exc:  # noqa: BLE001 - the bridge must stay alive
        _log(f"request failed: {exc}")
        if request_id is None:
            return None
        return _error_response(request_id, -32603, str(exc))

    return response


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--init-function", default="")
    parser.add_argument("--stop-function", default="")
    parser.add_argument("--socket", default="")
    parser.add_argument("--server-id", default="default")
    args, _unknown = parser.parse_known_args()

    _log(f"starting shim server_id={args.server_id}")

    if args.init_function:
        try:
            _emacsclient(args, f"({args.init_function})")
            _log(f"init ok: {args.init_function}")
        except Exception as exc:  # noqa: BLE001
            _log(f"init failed: {exc}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = _handle_line(args, line)
        if response:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()

    if args.stop_function:
        try:
            _emacsclient(args, f"({args.stop_function})")
            _log(f"stop ok: {args.stop_function}")
        except Exception as exc:  # noqa: BLE001
            _log(f"stop failed: {exc}")

    _log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
