#!/usr/bin/env python3
"""codex-relay: lets a Claude agent hand computer-use work to Codex (Astra) and get the result back.

Claude writes the code; Codex does what Claude is slow at: clicking through the running app to test a change,
taking screenshots, saving page DOM. One tool, codex_computer, over MCP (stdio).

Each call opens a new Codex app chat with the task typed in (and the in-app browser at `url`). The user presses
Enter once; Codex runs it in the Codex app with its in-app browser while the user watches; codex-relay waits for
the chat to finish and gives Claude the report and the files.

Install once, as a plugin, in the Claude desktop app (Code tab) or the claude CLI:
  /plugin marketplace add airman416/codex-relay
  /plugin install codex-relay@codex-relay

Env: CODEX_BIN (codex), CODEX_STEP_TIMEOUT (1200 s for Codex to finish), CODEX_RELAY_START_WAIT (300 s to press
     Enter), CODEX_RELAY_APP_DIR (~/Documents/Codex/codex-relay: the Codex app project the chats run in).
"""
import json, os, queue, re, subprocess, sys, threading, time, urllib.parse, uuid
from pathlib import Path

CODEX = os.environ.get("CODEX_BIN", "codex")
TIMEOUT = int(os.environ.get("CODEX_STEP_TIMEOUT", "1200"))
START_WAIT = int(os.environ.get("CODEX_RELAY_START_WAIT", "300"))
# One workspace for all chats: the Codex app shows them together under a "codex-relay" project.
APP_ROOT = Path(os.environ.get("CODEX_RELAY_APP_DIR", Path.home() / "Documents" / "Codex" / "codex-relay"))
OPEN_BIN = os.environ.get("CODEX_RELAY_OPEN_BIN", "open")

INSTRUCTIONS = """Codex (OpenAI's agent, in the Codex app) is available for computer-use work.
Write and fix code yourself. Hand computer use to codex_computer: open the running app or a site, click
through it, check that a change looks and works right, take screenshots, save page DOM. Each call opens a
new chat in the Codex app with the task typed in, and waits until the user presses Enter there (the Codex app
never sends a prompt that comes from a link). You are blocked while it waits, so BEFORE each call tell the
user: "Press Enter in the new Codex chat to start it." It returns Codex's report and the files it saved. If it
reports a problem in your change, fix the code and call it again (that needs another Enter)."""

TOOLS = [
    {"name": "codex_computer",
     "description": "Hand computer-use work to Codex in the Codex app: open the running app or a site in its "
                    "in-app browser, click through it, test that a change works, take screenshots, save page DOM "
                    "or data. Opens a new Codex app chat with the task typed in; the user presses Enter once and "
                    "can watch. Returns Codex's report and the files it saved. Can take minutes.",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string", "description": "Complete instructions: which app/site, what to do or check, "
                                                   "what to save. Codex cannot see this chat."},
         "url": {"type": "string", "description": "Optional http(s) URL to open in the in-app browser, e.g. "
                                                  "http://localhost:3000."},
         "dir": {"type": "string", "description": "Absolute folder for the output files. Default: a new folder "
                                                  "under ~/Documents/Codex/codex-relay/."}}},
     "annotations": {"openWorldHint": True}},
]


def text_arg(args, key):
    v = str(args.get(key) or "").strip()
    if not v:
        raise ValueError(f"{key} is required")
    return v


def url_arg(args):
    url = str(args.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    return url


def run_dir(task):
    """A new folder per run: <APP_ROOT>/<date>/<task words>-<time>."""
    words = "-".join(re.findall(r"[a-z0-9]+", task.lower())[:6])[:72] or "run"
    return APP_ROOT / time.strftime("%Y-%m-%d") / f"{words}-{time.strftime('%H%M%S')}"


class AppServer:
    """Minimal client for `codex app-server` (newline-delimited JSON-RPC over stdio), used to follow the chat."""

    def __init__(self, cwd, timeout):
        self.p = subprocess.Popen([CODEX, "app-server"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.lines, self.ids = queue.Queue(), iter(range(1, 10**6))
        self.deadline, self.timeout = time.time() + timeout, timeout
        threading.Thread(target=lambda: [self.lines.put(l) for l in self.p.stdout] + [self.lines.put(None)],
                         daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "codex-relay", "version": "3.0.1"},
                                 "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def send(self, msg):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.p.stdin.flush()

    def call(self, method, params):
        i = next(self.ids)
        self.send({"id": i, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=max(1, self.deadline - time.time()))
            except queue.Empty:
                raise subprocess.TimeoutExpired("codex app-server", self.timeout)
            if line is None:
                raise RuntimeError("codex app-server exited early")
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in m and "id" in m:  # we only read threads; refuse anything Codex asks of us
                self.send({"id": m["id"], "error": {"code": -32000, "message": "not allowed by codex-relay"}})
            elif m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(f"codex app-server {method}: {m['error'].get('message')}")
                return m["result"]

    def close(self):
        self.p.kill()


def codex_computer(args):
    task, url = text_arg(args, "task"), url_arg(args)
    out = Path(str(args.get("dir") or run_dir(task))).expanduser()
    if not out.is_absolute() or out.resolve() in (Path("/"), Path.home().resolve()):
        raise ValueError(f"dir must be an absolute folder other than / or your home folder: {out}")
    out.mkdir(parents=True, exist_ok=True)
    before = {p for p in out.rglob("*") if p.is_file()}
    tag = f"[codex-relay {uuid.uuid4().hex[:8]}]"
    prompt = (f"{tag} {task}\n\n" + (f"Start at {url} in the in-app browser. " if url else "") +
              f"Use the Codex in-app browser (or Computer Use if needed). Save every file you produce in {out}. "
              "Give each file the extension that matches its real format. When done, list each file you saved "
              "and what it shows.")
    # The Codex app only pre-fills prompts that come from a link, so the user presses Enter to start the chat.
    query = {"prompt": prompt, "path": str(APP_ROOT), **({"browserUrl": url} if url else {})}
    started = time.time()
    r = subprocess.run([OPEN_BIN, "codex://new?" + urllib.parse.urlencode(query)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"could not open the Codex app: {(r.stderr or r.stdout).strip()}")
    srv = AppServer(out, START_WAIT + TIMEOUT)
    try:
        thread = None
        while thread is None:  # wait for the user to press Enter, which creates the thread
            rows = srv.call("thread/list", {"limit": 20, "archived": False, "searchTerm": tag,
                                            "useStateDbOnly": True}).get("data") or []
            thread = next((t for t in rows if tag in (t.get("preview") or "")), None)
            if thread is None:
                if time.time() - started > START_WAIT:
                    raise RuntimeError(f"Nobody started the Codex app chat within {START_WAIT}s. The task is typed "
                                       "into a new Codex chat; ask the user to press Enter there, then call again.")
                time.sleep(2)
        while True:  # wait for the Codex app to finish the turn
            th = srv.call("thread/read", {"threadId": thread["id"], "includeTurns": True})["thread"]
            last = (th.get("turns") or [{}])[-1]
            # Read from disk, a turn the app is still running looks "interrupted"; it is done once it has completedAt.
            if last.get("completedAt") and last.get("status") != "inProgress":
                break
            if time.time() - started > START_WAIT + TIMEOUT:
                raise subprocess.TimeoutExpired("Codex app chat", TIMEOUT)
            time.sleep(3)
    finally:
        srv.close()
    texts = [i.get("text", "") for i in last.get("items") or [] if i.get("type") == "agentMessage"]
    report = texts[-1] if texts else "(Codex returned no message)"
    new = sorted(str(p) for p in out.rglob("*") if p.is_file() and p not in before)
    err = f"\nCodex {last['status']}: {last.get('error')}" if last["status"] != "completed" else ""
    return (f"Codex report:\n{report}{err}\n\nFolder: {out}\nNew files:\n"
            + ("\n".join(new) or "(no files saved)") + f"\nCodex chat: codex://threads/{thread['id']}")


HANDLERS = {"codex_computer": codex_computer}

# --- MCP over stdio: newline-delimited JSON-RPC ----------------------------------------------------

OUT = threading.Lock()


def send(msg):
    with OUT:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call_tool(id_, params):
    name, args = params.get("name"), params.get("arguments") or {}
    try:
        if name not in HANDLERS:
            raise ValueError(f"unknown tool {name}")
        result = {"content": [{"type": "text", "text": HANDLERS[name](args)}]}
    except subprocess.TimeoutExpired:
        result = {"content": [{"type": "text", "text": f"Codex timed out after {TIMEOUT}s"}], "isError": True}
    except Exception as e:  # report every failure to the agent; never kill the server
        result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    send({"jsonrpc": "2.0", "id": id_, "result": result})


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        m, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if m == "tools/call":  # each call on its own thread, so Claude can run several at once
            threading.Thread(target=call_tool, args=(id_, params), daemon=True).start()
            continue
        if id_ is None:
            continue  # notification
        if m == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "codex-relay", "version": "3.0.1"},
                      "instructions": INSTRUCTIONS}
        elif m == "tools/list":
            result = {"tools": TOOLS}
        elif m == "ping":
            result = {}
        else:
            send({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": f"unknown method {m}"}})
            continue
        send({"jsonrpc": "2.0", "id": id_, "result": result})


if __name__ == "__main__":
    main()
