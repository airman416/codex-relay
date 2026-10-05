#!/usr/bin/env python3
"""codex-relay: lets a Claude agent hand computer-use work to Codex (Astra) and get the result back.

Claude writes the code; Codex does what Claude is slow at: clicking through the running app to test a change,
taking screenshots, saving page DOM. One tool, codex_computer, over MCP (stdio).

Install once, as a plugin, in the Claude desktop app (Code tab) or the claude CLI:
  /plugin marketplace add airman416/codex-relay
  /plugin install codex-relay@codex-relay

Modes
  headless (default)  no clicks; runs through `codex app-server`, Codex drives Chrome with Computer Use.
  app                 opens a new Codex app chat with the task typed in; the user presses Enter once and can
                      watch; Codex gets its in-app browser.

Env: CODEX_BIN (codex), CODEX_MODEL (gpt-6-astra), CODEX_STEP_TIMEOUT (1200 s),
     CODEX_RELAY_START_WAIT (300 s to press Enter in app mode),
     CODEX_RELAY_APP_DIR (~/Documents/Codex/codex-relay: the Codex app project that app-mode chats run in).
"""
import json, os, queue, re, subprocess, sys, threading, time, urllib.parse, uuid
from pathlib import Path

CODEX = os.environ.get("CODEX_BIN", "codex")
MODEL = os.environ.get("CODEX_MODEL", "gpt-6-astra")  # Astra: the model the team uses for computer use
TIMEOUT = int(os.environ.get("CODEX_STEP_TIMEOUT", "1200"))

INSTRUCTIONS = """Codex (OpenAI's agent, on its Astra model) is available for computer-use work.
Write and fix code yourself. Hand computer use to codex_computer: open the running app or a site, click
through it, check that a change looks and works right, take screenshots, save page DOM. It returns Codex's
report and the files it saved. If it reports a problem in your change, fix the code and call it again.
Use mode "app" when the user wants to watch or wants Codex's in-app browser; then tell the user to press
Enter in the new Codex chat."""

TOOLS = [
    {"name": "codex_computer",
     "description": "Hand computer-use work to Codex: open the running app or a site (e.g. Chrome), click "
                    "through it, test that a change works, take screenshots, save page DOM or data. Codex runs "
                    "sandboxed with network access and can only write into `dir`; its Computer Use actions are "
                    "approved for this call only. Returns Codex's report and the files it saved. Can take minutes.",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string", "description": "Complete instructions: which app/site, what to do or check, "
                                                   "what to save. Codex cannot see this chat."},
         "url": {"type": "string", "description": "Optional http(s) URL to start at, e.g. http://localhost:3000."},
         "dir": {"type": "string", "description": "Absolute folder for the output files. Default: a new folder "
                                                  "under ~/Documents/Codex/ (app mode: ~/Documents/Codex/codex-relay/)."},
         "mode": {"type": "string", "enum": ["headless", "app"], "default": "headless",
                  "description": "headless: no clicks needed; Codex drives Chrome with Computer Use. app: opens a "
                                 "new chat in the Codex app with the task typed in; the user presses Enter once, "
                                 "can watch it live, and Codex gets its in-app browser."},
         "model": {"type": "string", "description": "Codex model. Default gpt-6-astra."}}},
     "annotations": {"openWorldHint": True}},
]


def model_arg(args):
    m = str(args.get("model") or "").strip()
    if m.startswith("-") or " " in m:
        raise ValueError(f"bad model name: {m!r}")
    return m or None


def text_arg(args, key):
    v = str(args.get(key) or "").strip()
    if not v:
        raise ValueError(f"{key} is required")
    return v


def projectless_dir(task):
    """A new folder per run under ~/Documents/Codex/<date>/, named after the task and the time."""
    words = "-".join(re.findall(r"[a-z0-9]+", task.lower())[:6])[:72] or "run"
    return Path.home() / "Documents" / "Codex" / time.strftime("%Y-%m-%d") / f"{words}-{time.strftime('%H%M%S')}"


def codex_computer(args):
    task, url, mode = text_arg(args, "task"), url_arg(args), str(args.get("mode") or "headless")
    if mode not in ("headless", "app"):
        raise ValueError('mode must be "headless" or "app"')
    out = Path(str(args.get("dir") or projectless_dir(task))).expanduser()
    if not out.is_absolute() or out.resolve() in (Path("/"), Path.home().resolve()):
        raise ValueError(f"dir must be an absolute folder other than / or your home folder: {out}")
    if mode == "app":
        return app_chat(task, url, Path(str(args["dir"])).expanduser() if args.get("dir") else None)
    out.mkdir(parents=True, exist_ok=True)
    before = {p for p in out.rglob("*") if p.is_file()}
    prompt = (f"{task}\n\n" + (f"Start at {url}. " if url else "") +
              f"Use your browser or computer-use tools. Save every file you produce in {out}. "
              "Give each file the extension that matches its real format. When done, close any tabs or windows "
              "you opened, then list each file you saved and what it shows.")
    report, approved = app_server_turn(prompt, out, model_arg(args) or MODEL)
    new = sorted(str(p) for p in out.rglob("*") if p.is_file() and p not in before)
    files = "\n".join(new) or "(no files saved)"
    return f"Codex report:\n{report}\n\nFolder: {out}\nNew files:\n{files}\n({approved} Computer Use actions approved)"


def url_arg(args):
    url = str(args.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    return url


class AppServer:
    """Minimal client for `codex app-server` (newline-delimited JSON-RPC over stdio)."""

    def __init__(self, cwd, timeout, on_request=None, on_message=None):
        self.p = subprocess.Popen([CODEX, "app-server"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        self.lines, self.ids = queue.Queue(), iter(range(1, 10**6))
        self.deadline, self.timeout = time.time() + timeout, timeout
        self.on_request = on_request or (lambda m: {"error": {"code": -32000, "message": "not allowed by codex-relay"}})
        self.on_message = on_message or (lambda m: None)
        threading.Thread(target=lambda: [self.lines.put(l) for l in self.p.stdout] + [self.lines.put(None)],
                         daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "codex-relay", "version": "1.5.0"},
                                 "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def send(self, msg):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.p.stdin.flush()

    def pump(self, until):
        """Read messages, answering Codex's requests, until `until(msg)` returns a value."""
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
            if "method" in m and "id" in m:  # a request from Codex to us
                self.send({"id": m["id"], **self.on_request(m)})
                continue
            self.on_message(m)
            got = until(m)
            if got is not None:
                return got

    def call(self, method, params):
        i = next(self.ids)
        self.send({"id": i, "method": method, "params": params})
        r = self.pump(lambda m: m if m.get("id") == i and "method" not in m else None)
        if "error" in r:
            raise RuntimeError(f"codex app-server {method}: {r['error'].get('message')}")
        return r["result"]

    def close(self):
        self.p.kill()


# Computer Use only asks for approval through a client, so `codex exec` (approval: never) always gets "not
# approved". `codex app-server` sends each approval to us as an MCP elicitation. We accept ones from the
# Computer Use / browser runtime (cua_repl) and decline the rest; shell commands stay inside the sandbox.
COMPUTER_SERVERS = {"cua_repl"}
APPROVALS = {"granular": {"mcp_elicitations": True, "rules": False, "sandbox_approval": False,
                          "request_permissions": False, "skill_approval": False}}


def app_server_turn(prompt, cwd, model):
    """Headless run: one temporary Codex thread; returns (final message, Computer Use actions approved)."""
    texts, approved = [], [0]

    def on_request(m):
        if m["method"] != "mcpServer/elicitation/request":
            return {"error": {"code": -32000, "message": "not allowed by codex-relay"}}
        ok = (m.get("params") or {}).get("serverName") in COMPUTER_SERVERS
        approved[0] += ok
        return {"result": {"action": "accept" if ok else "decline", "content": {}}}

    def on_message(m):
        item = (m.get("params") or {}).get("item") or {}
        if m.get("method") == "item/completed" and item.get("type") == "agentMessage":
            texts.append(item.get("text", ""))

    srv = AppServer(cwd, TIMEOUT, on_request, on_message)
    try:
        thread = srv.call("thread/start", {"cwd": str(cwd), "model": model, "sandbox": "workspace-write",
                                           "approvalPolicy": APPROVALS, "ephemeral": True,
                                           "config": {"sandbox_workspace_write": {"network_access": True}}})
        tid = (thread.get("thread") or {}).get("id") or thread.get("threadId")
        srv.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": prompt}]})
        turn = srv.pump(lambda m: m["params"] if m.get("method") == "turn/completed" else None)
        err = (turn.get("turn") or {}).get("error")
        report = texts[-1] if texts else "(Codex returned no message)"
        return (f"{report}\n\nCodex error: {err}" if err else report), approved[0]
    finally:
        srv.close()


# App mode: the Codex app itself runs the chat, so Codex gets its in-app browser and the user can watch.
# The app only pre-fills prompts that come from a link, so the user presses Enter once to start it.
START_WAIT = int(os.environ.get("CODEX_RELAY_START_WAIT", "300"))
# One workspace for all app-mode chats: the Codex app shows them together under a "codex-relay" project.
APP_ROOT = Path(os.environ.get("CODEX_RELAY_APP_DIR", Path.home() / "Documents" / "Codex" / "codex-relay"))
OPEN_BIN = os.environ.get("CODEX_RELAY_OPEN_BIN", "open")


def app_chat(task, url, out):
    tag = f"[codex-relay {uuid.uuid4().hex[:8]}]"
    out = out or APP_ROOT / time.strftime("%Y-%m-%d") / projectless_dir(task).name
    out.mkdir(parents=True, exist_ok=True)
    before = {p for p in out.rglob("*") if p.is_file()}
    prompt = (f"{tag} {task}\n\n" + (f"Start at {url} in the in-app browser. " if url else "") +
              f"Use the Codex in-app browser (or Computer Use if needed). Save every file you produce in {out}. "
              "Give each file the extension that matches its real format. When done, list each file you saved "
              "and what it shows.")
    query = {"prompt": prompt, "path": str(APP_ROOT), **({"browserUrl": url} if url else {})}
    link = "codex://new?" + urllib.parse.urlencode(query)
    started = time.time()
    r = subprocess.run([OPEN_BIN, link], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"could not open the Codex app: {(r.stderr or r.stdout).strip()}")
    srv = AppServer(Path.home(), START_WAIT + TIMEOUT)
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
    return (f"Codex report (Codex app chat):\n{report}{err}\n\nFolder: {out}\nNew files:\n"
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
                      "serverInfo": {"name": "codex-relay", "version": "2.0.0"},
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
