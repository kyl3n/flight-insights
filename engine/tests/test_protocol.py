"""Exercise the public stdio boundary; fixtures are synthetic, never aircraft evidence."""
import json
from pathlib import Path
import subprocess
import sys

from q22_engine.__main__ import Engine, MAX_REQUEST


def test_contract_and_missing_methods(tmp_path: Path):
    engine = Engine(tmp_path)
    request = {"jsonrpc": "2.0", "id": "hello", "method": "handshake"}
    reply = engine.handle(request)
    assert reply["id"] == "hello"
    assert reply["result"]["protocol_version"] == 1
    assert "network.apply" in reply["result"]["capabilities"]
    assert "parameters.write" not in reply["result"]["capabilities"]
    assert engine.handle({**request, "method": "parameters.write"})["error"]["code"] == -32601
    assert engine.handle({**request, "params": []})["error"]["code"] == -32602
    assert engine.handle({**request, "jsonrpc": "1.0"})["error"]["code"] == -32600
    assert engine.handle({**request, "id": True})["error"]["code"] == -32600


def test_stdio_recovers_after_bad_frames_and_oversize(tmp_path: Path):
    requests = [
        b"not json\n", b"{\"id\": 3, \"value\": NaN}\n", b"x" * (MAX_REQUEST + 40) + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "handshake"}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "sources.capabilities"}).encode() + b"\n",
    ]
    result = subprocess.run([sys.executable, "-m", "q22_engine", "--data-dir", str(tmp_path)],
                            input=b"".join(requests), capture_output=True, timeout=15, check=True)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 5
    assert [r["error"]["code"] for r in replies[:3]] == [-32700, -32700, -32600]
    assert replies[3]["id"] == 4
    assert replies[3]["result"]["product"] == "Q22 Flight Lab"
    sources = {s["kind"]: s["state"] for s in replies[4]["result"]["sources"]}
    assert sources["dataflash"] == "available"
    assert sources["tlog"] == "available"
    assert sources["skypulse_normalization"] == "unvalidated"
