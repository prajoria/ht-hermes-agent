"""Real ``POST /v1/runs`` probe for HT patch P1: a loopback model asks for one ``read_file`` call
and a plugin records what its tool hooks receive (``$HERMES_HOME/hook_calls.jsonl``)."""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import traceback

from api_authority_peer import _poll_run_terminal

PLUGIN = '''
import json, os
from pathlib import Path

def _record(name):
    def hook(**kw):
        with open(Path(os.environ["HERMES_HOME"]) / "hook_calls.jsonl", "a") as out:
            out.write(json.dumps({"hook": name, **kw}, default=str) + "\\n")
        if name == "pre_tool_call" and kw.get("tool_name") == "read_file":
            return {"action": "modify", "args": {"path": os.environ["P1_REAL_FILE"]}}
    return hook

def register(ctx):
    for name in ("pre_tool_call", "post_tool_call", "transform_tool_result"):
        ctx.register_hook(name, _record(name))
'''


class ToolModel(BaseHTTPRequestHandler):
    """First turn: one ``read_file`` tool call on a path only the hook can fix; then a final answer."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "local-wire-stub", "object": "model"}]})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in request:
            return self._send({})
        answered = any(m.get("role") == "tool" for m in request["messages"])
        call = {"index": 0, "id": "call_p1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "MODEL_PATH.txt"})}}
        message = {"role": "assistant", "content": "P1_DONE"} if answered else {
            "role": "assistant", "content": None, "tool_calls": [call]}
        finish = "stop" if answered else "tool_calls"
        base = {"id": "chatcmpl-p1", "model": "local-wire-stub", "created": 1}
        if not request.get("stream"):
            return self._send({**base, "object": "chat.completion", "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, reason in [(message, None), ({}, finish)]:
            chunk = {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send(self, body):
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def probe():
    import aiohttp
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority

    runner = GatewayRunner()
    if os.environ.get("P1_AUTHORITY") == "1":
        await initialize_session_authority(runner, profile_id="default", instance_id="p1-peer")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "p1-probe-owned-secret", "port": 0}))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.API_SERVER] = adapter
    assert await adapter.connect()
    port = adapter._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer p1-probe-owned-secret", "X-Hermes-Session-Key": os.environ["P1_KEY"]}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
            async with client.post(f"http://127.0.0.1:{port}/v1/runs", headers=headers,
                                   json={"session_id": "job-1.a1", "input": "CALL_TOOL"}) as response:
                accepted = await response.json()
                assert response.status == 202, accepted
            status = await _poll_run_terminal(client, f"http://127.0.0.1:{port}/v1/runs/{accepted['run_id']}", headers)
        Path(os.environ["HERMES_HOME"], "receipt.json").write_text(json.dumps(
            {"run_id": accepted["run_id"], "status": status,
             "authority": getattr(runner, "session_authority", None) is not None}))
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    home = Path(os.environ["HERMES_HOME"])
    plugin = home / "plugins" / "p1_probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: p1_probe\n")
    (plugin / "__init__.py").write_text(PLUGIN)
    real = home / "real.txt"
    real.write_text("P1_REAL_CONTENT\n")
    os.environ["P1_REAL_FILE"] = str(real)
    peer = ThreadingHTTPServer(("127.0.0.1", 0), ToolModel)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{peer.server_port}/v1"
    os.environ.update(OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=base)
    config = {
        "model": {"default": "local-wire-stub", "provider": "custom", "base_url": base},
        "auxiliary": {"title_generation": {"enabled": False}},
        "terminal": {"cwd": str(home)},
        "plugins": {"enabled": ["p1_probe"]},
    }
    if os.environ.get("P1_FLAG"):
        config["ht_patches"] = {"p1_run_hook_kwargs": os.environ["P1_FLAG"] == "on"}
    (home / "config.yaml").write_text(json.dumps(config))
    code = 0
    try:
        asyncio.run(probe())
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        peer.shutdown()
        peer.server_close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
