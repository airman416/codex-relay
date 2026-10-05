"""Drives the MCP server over stdio with a fake `codex` binary. Run: python3 tests/test_codex_mcp.py"""
import json, os, re, subprocess, sys, tempfile, urllib.parse
from pathlib import Path

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, re, sys, urllib.parse
a = sys.argv[1:]
opt = lambda k: a[a.index(k) + 1] if k in a else None
if a[:1] == ["app-server"]:  # minimal JSON-RPC peer for codex_computer
    send = lambda m: print(json.dumps({"jsonrpc": "2.0", **m}), flush=True)
    recv = lambda: json.loads(sys.stdin.readline())
    reads = 0
    while True:
        m = recv()
        if m.get("method") == "initialize":
            assert m["params"]["capabilities"]["experimentalApi"]
            send({"id": m["id"], "result": {}})
        elif m.get("method") == "thread/start":
            prm = m["params"]
            assert prm["sandbox"] in ("workspace-write", "read-only") and prm["ephemeral"] is True
            assert prm["approvalPolicy"]["granular"]["mcp_elicitations"] and not prm["approvalPolicy"]["granular"]["sandbox_approval"]
            send({"id": m["id"], "result": {"thread": {"id": "t1"}}})
        elif m.get("method") == "thread/list":  # app mode: the chat exists once the fake opener "pressed Enter"
            link = os.environ["FAKE_LINK_FILE"]
            prompt = dict(urllib.parse.parse_qsl(open(link).read().split("?", 1)[1]))["prompt"] if os.path.exists(link) else ""
            ok = prompt and m["params"]["searchTerm"] in prompt
            send({"id": m["id"], "result": {"data": [{"id": "app1", "preview": prompt, "cwd": os.environ["FAKE_APP_DIR"]}] if ok else []}})
        elif m.get("method") == "thread/read":
            assert m["params"]["threadId"] == "app1"
            reads += 1
            prompt = dict(urllib.parse.parse_qsl(open(os.environ["FAKE_LINK_FILE"]).read().split("?", 1)[1]))["prompt"]
            out = re.search(r"Save every file you produce in (\S+)\. ", prompt).group(1)
            if reads == 1:  # still running in the app: read from disk it looks interrupted, with no completedAt
                turn = {"status": "interrupted", "completedAt": None, "items": []}
            else:
                open(out + "/page.png", "w").write("png")
                turn = {"status": "completed", "completedAt": 2, "items": [{"type": "agentMessage", "text": "done in the Codex app"}]}
            send({"id": m["id"], "result": {"thread": {"id": "app1", "cwd": os.environ["FAKE_APP_DIR"], "turns": [turn]}}})
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

opener = tmp / "open"  # stands in for macOS `open` + the user pressing Enter in the Codex app
opener.write_text('#!/bin/sh\ncase "$1" in *NOSTART*) exit 0;; esac\nprintf %s "$1" > "$FAKE_LINK_FILE"\n')
opener.chmod(0o755)
app_dir = tmp / "appchat"
app_dir.mkdir()
env = {**os.environ, "CODEX_BIN": str(fake), "CODEX_RELAY_HOME": str(tmp / "home"), "HOME": str(tmp / "userhome"),
       "CODEX_RELAY_OPEN_BIN": str(opener), "CODEX_RELAY_START_WAIT": "3",
       "FAKE_LINK_FILE": str(tmp / "link.txt"), "FAKE_APP_DIR": str(app_dir), "CODEX_RELAY_APP_DIR": str(app_dir)}
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
err, text = tool("codex_computer", task="Open Timeback, screenshot it!")  # default: a Codex projectless-chat folder
assert not err and str(tmp / "userhome" / "Documents" / "Codex") in text and "/open-timeback-screenshot-it" in text, text
assert tool("codex_computer", task="x", dir="/")[0] and tool("codex_computer", task="x", dir="rel")[0]

err, text = tool("codex_computer", task="screenshot the leaderboard", mode="app", url="https://example.com")
link = (tmp / "link.txt").read_text()
assert link.startswith("codex://new?") and "browserUrl=https%3A%2F%2Fexample.com" in link, link
assert dict(urllib.parse.parse_qsl(link.split("?", 1)[1]))["path"] == str(app_dir), link
assert not err and "done in the Codex app" in text and "Codex interrupted" not in text, text
assert re.search(re.escape(str(app_dir)) + r"/\d{4}-\d\d-\d\d/screenshot-the-leaderboard/page\.png", text), text
assert "Codex chat: codex://threads/app1" in text, text
(tmp / "link.txt").unlink()
err, text = tool("codex_computer", task="NOSTART please", mode="app")  # nobody presses Enter
assert err and "Nobody started the Codex app chat" in text, text
assert tool("codex_computer", task="x", mode="cloud")[0] and tool("codex_computer", task="x", url="ftp://x")[0]

for bad in ({"repo": "relative", "task": "t"}, {"repo": str(repo), "task": " "},
            {"repo": str(repo), "task": "t", "base": "--output=/tmp/x"}):
    err, text = tool("codex_review", **bad)
    assert err, bad
assert tool("nope")[0]
srv.terminate()
print("ok")
