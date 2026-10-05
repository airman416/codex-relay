"""Drives the MCP server over stdio with a fake `codex` binary. Run: python3 tests/test_codex_mcp.py"""
import json, os, re, subprocess, sys, tempfile, urllib.parse
from pathlib import Path

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, re, sys, urllib.parse
assert sys.argv[1:] == ["app-server"]
send = lambda m: print(json.dumps({"jsonrpc": "2.0", **m}), flush=True)
recv = lambda: json.loads(sys.stdin.readline())
link_prompt = lambda: dict(urllib.parse.parse_qsl(open(os.environ["FAKE_LINK_FILE"]).read().split("?", 1)[1]))["prompt"]
reads = 0
while True:
    m = recv()
    if m.get("method") == "initialize":
        assert m["params"]["capabilities"]["experimentalApi"]
        send({"id": m["id"], "result": {}})
    elif m.get("method") == "thread/list":  # app mode: the chat exists once the fake opener "pressed Enter"
        prompt = link_prompt() if os.path.exists(os.environ["FAKE_LINK_FILE"]) else ""
        ok = prompt and m["params"]["searchTerm"] in prompt
        send({"id": m["id"], "result": {"data": [{"id": "app1", "preview": prompt}] if ok else []}})
    elif m.get("method") == "thread/read":
        assert m["params"]["threadId"] == "app1"
        reads += 1
        out = re.search(r"Save every file you produce in (\S+)\. ", link_prompt()).group(1)
        if reads == 1:  # still running in the app: read from disk it looks interrupted, with no completedAt
            turn = {"status": "interrupted", "completedAt": None, "items": []}
        else:
            open(out + "/page.png", "w").write("png")
            turn = {"status": "completed", "completedAt": 2, "items": [{"type": "agentMessage", "text": "done in the Codex app"}]}
        send({"id": m["id"], "result": {"thread": {"id": "app1", "turns": [turn]}}})
'''

tmp = Path(tempfile.mkdtemp())
fake = tmp / "codex"
fake.write_text(FAKE_CODEX)
fake.chmod(0o755)
opener = tmp / "open"  # stands in for macOS `open` + the user pressing Enter in the Codex app
opener.write_text('#!/bin/sh\ncase "$1" in *NOSTART*) exit 0;; esac\nprintf %s "$1" > "$FAKE_LINK_FILE"\n')
opener.chmod(0o755)
app_dir = tmp / "appchat"
env = {**os.environ, "CODEX_BIN": str(fake), "HOME": str(tmp / "userhome"), "CODEX_RELAY_OPEN_BIN": str(opener),
       "CODEX_RELAY_START_WAIT": "3", "CODEX_RELAY_APP_DIR": str(app_dir), "FAKE_LINK_FILE": str(tmp / "link.txt")}
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
assert [t["name"] for t in rpc("tools/list")["result"]["tools"]] == ["codex_computer"]

# opens a pre-filled Codex app chat, waits for it to start and finish, returns report + files
err, text = tool("codex_computer", task="screenshot the leaderboard", url="https://example.com")
link = (tmp / "link.txt").read_text()
query = dict(urllib.parse.parse_qsl(link.split("?", 1)[1]))
assert link.startswith("codex://new?") and query["browserUrl"] == "https://example.com" and query["path"] == str(app_dir), link
assert not err and "done in the Codex app" in text and "Codex interrupted" not in text, text
assert re.search(re.escape(str(app_dir)) + r"/\d{4}-\d\d-\d\d/screenshot-the-leaderboard-\d{6}/page\.png", text), text
assert "Codex chat: codex://threads/app1" in text, text
custom = tmp / "my-captures"
(tmp / "link.txt").unlink()
err, text = tool("codex_computer", task="save the DOM", dir=str(custom))  # explicit output folder
assert not err and str(custom / "page.png") in text, text
(tmp / "link.txt").unlink()
err, text = tool("codex_computer", task="NOSTART please")  # nobody presses Enter
assert err and "Nobody started the Codex app chat" in text, text

# bad input
for bad in ({"task": " "}, {"task": "x", "dir": "/"}, {"task": "x", "dir": "rel"}, {"task": "x", "url": "file:///etc/passwd"}):
    assert tool("codex_computer", **bad)[0], bad
assert tool("nope")[0]
srv.terminate()
print("ok")
