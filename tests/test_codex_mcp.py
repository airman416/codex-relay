"""Drives the MCP server over stdio with a fake `codex` binary. Run: python3 tests/test_codex_mcp.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
opt = lambda k: a[a.index(k) + 1] if k in a else None
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
if opt("--output-schema"):
    ok = "+fixed" in stdin
    v = {"approved": ok, "summary": "ok" if ok else "add() is wrong",
         "issues": [] if ok else [{"severity": "major", "file": "a.py", "line": 1, "comment": "say fixed"}]}
    open(opt("-o"), "w").write(json.dumps(v))
elif opt("-s") == "workspace-write":
    open(opt("-C") + "/from_codex.txt", "w").write("hi\n")
    open(opt("-o"), "w").write("wrote from_codex.txt")
else:
    open(opt("-o"), "w").write("answer: " + a[-1])
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
assert [t["name"] for t in rpc("tools/list")["result"]["tools"]] == ["codex_review", "codex_task", "codex_ask"]

assert tool("codex_review", repo=str(repo), task="t") == (False, "No changes to review against HEAD.")

(repo / "a.py").write_text("x = 2\n")
err, text = tool("codex_review", repo=str(repo), task="t")
assert not err and "CHANGES REQUESTED" in text and "a.py:1" in text, text

(repo / "new.py").write_text("fixed\n")  # untracked file must reach the reviewer
err, text = tool("codex_review", repo=str(repo), task="t")
assert not err and "APPROVED" in text, text

err, text = tool("codex_task", repo=str(repo), task="add a file")
assert not err and "Branch: codex/" in text, text
branch = text.split("Branch: ")[1].split()[0]
assert "from_codex.txt" in git("show", "--stat", branch)
assert not (repo / "from_codex.txt").exists()  # caller's checkout untouched

assert tool("codex_ask", repo=str(repo), question="why?") == (False, "answer: why?")

for bad in ({"repo": "relative", "task": "t"}, {"repo": str(repo), "task": " "},
            {"repo": str(repo), "task": "t", "base": "--output=/tmp/x"}):
    err, text = tool("codex_review", **bad)
    assert err, bad
assert tool("nope")[0]
srv.terminate()
print("ok")
