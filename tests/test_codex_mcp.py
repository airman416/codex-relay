"""Drives the MCP server over stdio with a fake `codex` binary. Run: python3 tests/test_codex_mcp.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
opt = lambda k: a[a.index(k) + 1] if k in a else None
if a[:1] == ["app-server"]:  # minimal JSON-RPC peer for codex_computer
    send = lambda m: print(json.dumps({"jsonrpc": "2.0", **m}), flush=True)
    recv = lambda: json.loads(sys.stdin.readline())
    while True:
        m = recv()
        if m.get("method") == "initialize":
            assert m["params"]["capabilities"]["experimentalApi"]
            send({"id": m["id"], "result": {}})
        elif m.get("method") == "thread/start":
            prm = m["params"]
            assert prm["sandbox"] in ("workspace-write", "read-only") and prm["ephemeral"]
            assert prm["approvalPolicy"]["granular"]["mcp_elicitations"] and not prm["approvalPolicy"]["granular"]["sandbox_approval"]
            send({"id": m["id"], "result": {"thread": {"id": "t1"}}})
        elif m.get("method") == "turn/start" and m["params"].get("outputSchema"):  # codex_review
            send({"id": m["id"], "result": {}})
            text = m["params"]["input"][0]["text"]
            looked = "localhost:9" in text
            if looked:  # a visual check: one Computer Use action that must be approved
                send({"id": 800, "method": "mcpServer/elicitation/request", "params": {"serverName": "cua_repl"}})
                assert recv()["result"]["action"] == "accept"
            ok = "+fixed" in text
            v = {"approved": ok, "summary": "ok" if ok else "add() is wrong",
                 "issues": [] if ok else [{"severity": "major", "file": "a.py", "line": 1, "comment": "say fixed"}]}
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": json.dumps(v)}}})
            send({"method": "turn/completed", "params": {"turn": {"id": "u0"}}})
            sys.exit(0)
        elif m.get("method") == "turn/start":
            send({"id": m["id"], "result": {}})
            send({"id": 900, "method": "mcpServer/elicitation/request", "params": {"serverName": "cua_repl"}})
            r1 = recv()
            send({"id": 901, "method": "mcpServer/elicitation/request", "params": {"serverName": "other"}})
            r2 = recv()
            send({"id": 902, "method": "item/commandExecution/requestApproval", "params": {}})
            r3 = recv()
            open("shot.png", "w").write("png")
            msg = f"cua:{r1['result']['action']} other:{r2['result']['action']} cmd-refused:{'error' in r3}"
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": msg}}})
            send({"method": "turn/completed", "params": {"turn": {"id": "u1"}}})
            sys.exit(0)
if opt("-s") == "workspace-write":
    open(opt("-C") + "/from_codex.txt", "w").write("hi\n")
    open(opt("-o"), "w").write("wrote from_codex.txt")
else:
    open(opt("-o"), "w").write("answer: " + a[-1] + (" via " + opt("-m") if opt("-m") else ""))
'''

tmp = Path(tempfile.mkdtemp())
fake = tmp / "codex"
fake.write_text(FAKE_CODEX)
fake.chmod(0o755)
repo = tmp / "repo"
repo.mkdir()
git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout
git("init", "-q")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
(repo / "a.py").write_text("x = 1\n")
git("add", "-A")
git("commit", "-qm", "init")

env = {**os.environ, "CODEX_BIN": str(fake), "CODEX_RELAY_HOME": str(tmp / "home")}
srv = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent.parent / "src" / "codex_mcp.py")],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
n = 0


def rpc(method, params=None):
    global n
    n += 1
    srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}) + "\n")
    srv.stdin.flush()
    return json.loads(srv.stdout.readline())


def tool(name, **args):
    r = rpc("tools/call", {"name": name, "arguments": args})["result"]
    return r.get("isError", False), r["content"][0]["text"]


assert rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"] == "codex-relay"
srv.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
assert [t["name"] for t in rpc("tools/list")["result"]["tools"]] == ["codex_review", "codex_task", "codex_ask", "codex_computer"]

assert tool("codex_review", repo=str(repo), task="t") == (False, "No changes to review against HEAD.")

(repo / "a.py").write_text("x = 2\n")
err, text = tool("codex_review", repo=str(repo), task="t")
assert not err and "CHANGES REQUESTED" in text and "a.py:1" in text, text

(repo / "new.py").write_text("fixed\n")  # untracked file must reach the reviewer
err, text = tool("codex_review", repo=str(repo), task="t")
assert not err and "APPROVED" in text and "checked it in the browser" not in text, text
err, text = tool("codex_review", repo=str(repo), task="t", url="http://localhost:9/page")
assert not err and "APPROVED" in text and "(Codex checked it in the browser: 1 Computer Use actions)" in text, text
assert tool("codex_review", repo=str(repo), task="t", url="file:///etc/passwd")[0]

err, text = tool("codex_task", repo=str(repo), task="add a file")
assert not err and "Branch: codex/" in text, text
branch = text.split("Branch: ")[1].split()[0]
assert "from_codex.txt" in git("show", "--stat", branch)
assert not (repo / "from_codex.txt").exists()  # caller's checkout untouched

assert tool("codex_ask", repo=str(repo), question="why?") == (False, "answer: why?")
assert tool("codex_ask", repo=str(repo), question="why?", model="gpt-6-astra") == (False, "answer: why? via gpt-6-astra")
assert tool("codex_ask", repo=str(repo), question="why?", model="--yolo")[0]

out = tmp / "captures"
err, text = tool("codex_computer", task="screenshot example.com", dir=str(out))
assert not err and "cua:accept other:decline cmd-refused:True" in text, text
assert str(out / "shot.png") in text and "(1 Computer Use actions approved)" in text, text
err, text = tool("codex_computer", task="x")  # default folder under CODEX_RELAY_HOME
assert not err and str(tmp / "home" / "computer") in text, text
assert tool("codex_computer", task="x", dir="/")[0] and tool("codex_computer", task="x", dir="rel")[0]

for bad in ({"repo": "relative", "task": "t"}, {"repo": str(repo), "task": " "},
            {"repo": str(repo), "task": "t", "base": "--output=/tmp/x"}):
    err, text = tool("codex_review", **bad)
    assert err, bad
assert tool("nope")[0]
srv.terminate()
print("ok")
