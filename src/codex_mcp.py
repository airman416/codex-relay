#!/usr/bin/env python3
"""codex-relay: gives a Claude agent Codex as a reviewer and a second worker, over MCP (stdio).

Install once, as a plugin, in the Claude desktop app (Code tab) or the claude CLI:
  /plugin marketplace add airman416/codex-relay
  /plugin install codex-relay@codex-relay

Then ask Claude as usual, e.g. "add rate limiting to /login, loop with Codex review until it approves".

Tools
  codex_review  Codex reviews the working-tree diff (or the diff against a base ref) and returns a JSON verdict.
                For visible changes it also opens the running app in Chrome (Computer Use) and checks it.
  codex_task    Codex does a task on its own git worktree and branch, so it never touches Claude's checkout.
  codex_ask     A read-only second opinion from Codex about the repo.
  codex_computer  Codex uses its browser / computer use (e.g. screenshots and DOM of a web app) and saves files.
                  Runs through `codex app-server`, so Codex's Computer Use approvals come back to this server.

Env: CODEX_BIN (codex), CODEX_MODEL (Codex default), CODEX_STEP_TIMEOUT (1200 s),
     CODEX_RELAY_HOME (~/.codex-relay, holds the worktrees), CODEX_RELAY_START_WAIT (300 s to press Enter in app mode),
     CODEX_RELAY_APP_DIR (~/Documents/Codex/codex-relay: the Codex app project that app-mode chats run in).
"""
import json, os, queue, re, subprocess, sys, tempfile, threading, time, urllib.parse, uuid
from pathlib import Path

CODEX = os.environ.get("CODEX_BIN", "codex")
MODEL = os.environ.get("CODEX_MODEL")
TIMEOUT = int(os.environ.get("CODEX_STEP_TIMEOUT", "1200"))
HOME = Path(os.environ.get("CODEX_RELAY_HOME", Path.home() / ".codex-relay"))
MAX_DIFF = 200_000  # chars sent to Codex

VERDICT = {
    "type": "object", "additionalProperties": False,
    "required": ["approved", "summary", "issues"],
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "file", "line", "comment"],
            "properties": {
                "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "comment": {"type": "string"}}}}}}

REVIEW_PROMPT = """You are reviewing a diff that another agent wrote for this task:

TASK: {task}

Check correctness, bugs, security, missing edge cases, and whether the diff actually does the task.
Read files in the repo for context if needed, but do not edit anything. Approve only if there are no
blocker or major issues; minor issues alone do not block. Use line 0 when an issue has no single line.
{visual}
Reply with the JSON verdict only.

DIFF:
{diff}"""

VISUAL_URL = """The change is running at {url}. Open it in Chrome with your browser / computer-use tools and check
that it looks and behaves as the task says. Report what you see there as issues if it is wrong (file "{url}").
Close any tabs you opened."""
VISUAL_AUTO = """If the change affects something visible (a web page, UI, or app) and you can find where it runs
(for example a localhost URL in the task or the repo's dev config), open it in Chrome with your browser /
computer-use tools and check it. Close any tabs you opened. Skip this for changes with nothing to look at."""

INSTRUCTIONS = """Codex (OpenAI's coding agent) is available as a reviewer and a second worker.
Review loop: after you change code, call codex_review with the repo and what the change must do.
If the change is visible (a page, UI, app) and it is running, pass its URL so Codex also checks it in Chrome.
Fix every blocker and major issue, then call codex_review again. Stop when it approves or after 3 rounds,
and tell the user about any issue you chose not to fix and why.
Use codex_task to hand off independent work. It runs on its own branch, so read its diff before you merge it.
Use codex_ask for a second opinion on a design or a bug.
Use codex_computer for browser or computer-use work (open a site, click through it, take screenshots,
save the DOM). Prefer model gpt-6-astra for it. It returns the files it saved; use them in your work.
With mode "app" it runs as a visible Codex app chat: tell the user to press Enter in the new Codex chat."""

REPO = {"type": "string", "description": "Absolute path to a directory inside the git repository."}
MODEL_ARG = {"type": "string", "description": "Optional Codex model for this call, e.g. gpt-6-astra (strongest; use for computer use, browser work and hard reviews), gpt-6.1-sol (default workhorse), gpt-6-luna (fast, cheap). Omit to use the user's Codex default."}
TOOLS = [
    {"name": "codex_review",
     "description": "Codex reviews the current uncommitted changes (tracked and untracked) or, with `base`, "
                    "everything since that ref. For visible changes Codex also opens the running app in Chrome "
                    "(pass `url`) and checks it. Returns a JSON verdict {approved, summary, issues[]}. "
                    "Fix blocker/major issues and call again until approved.",
     "inputSchema": {"type": "object", "required": ["repo", "task"], "properties": {
         "repo": REPO,
         "task": {"type": "string", "description": "What the change is supposed to do, so Codex can judge it."},
         "base": {"type": "string", "description": "Optional git ref, e.g. main. Default: HEAD (uncommitted work)."},
         "url": {"type": "string", "description": "Optional URL where the change is running (e.g. "
                                                 "http://localhost:3000/signup). Codex opens it in Chrome and checks it "
                                                 "visually. Without it, Codex checks visible changes when it can find them."},
         "model": MODEL_ARG}},
     "annotations": {"readOnlyHint": True}},
    {"name": "codex_task",
     "description": "Hand a coding task to Codex. By default it works in a new git worktree on branch "
                    "codex/<id> and commits there, so your checkout is not touched. Returns Codex's report, "
                    "the branch, and a diffstat. Inspect with `git diff HEAD...<branch>`, bring in with "
                    "`git merge <branch>`. Can take several minutes.",
     "inputSchema": {"type": "object", "required": ["repo", "task"], "properties": {
         "repo": REPO,
         "task": {"type": "string", "description": "Complete, self-contained instructions. Codex cannot see this chat."},
         "isolate": {"type": "boolean", "default": True,
                     "description": "false = Codex edits the repo's working tree directly (no branch, no commit)."},
         "model": MODEL_ARG}}},
    {"name": "codex_ask",
     "description": "Ask Codex a question about the repo (design, bug hunt, second opinion). Read-only.",
     "inputSchema": {"type": "object", "required": ["repo", "question"], "properties": {
         "repo": REPO, "question": {"type": "string"}, "model": MODEL_ARG}},
     "annotations": {"readOnlyHint": True}},
    {"name": "codex_computer",
     "description": "Hand browser / computer-use work to Codex: open sites or apps (e.g. Chrome), click through "
                    "them, take screenshots, save page DOM or data. Codex runs sandboxed with network access and "
                    "can only write into `dir`; its Computer Use actions are approved for this call only. Returns "
                    "Codex's report and the files it saved. Can take several minutes.",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string", "description": "Complete instructions: which site/app, what to do, what to save "
                                                   "and how to name files. Codex cannot see this chat."},
         "dir": {"type": "string", "description": "Absolute folder for the output files. Default: a new folder "
                                                  "under ~/Documents/Codex/<date>/ (app mode: under ~/Documents/Codex/codex-relay/)."},
         "url": {"type": "string", "description": "Optional http(s) URL to start at."},
         "mode": {"type": "string", "enum": ["headless", "app"], "default": "headless",
                  "description": "headless: no clicks needed; Codex drives Chrome with Computer Use. app: opens a new "
                                 "chat in the Codex app with the task typed in and the in-app browser at `url`; the "
                                 "user presses Enter once, can watch it live, and Codex gets full in-app browser "
                                 "control. Use app when the user wants to watch or wants the in-app browser."},
         "model": MODEL_ARG}},
     "annotations": {"openWorldHint": True}},
]


def run(cmd, cwd, check=True):
    # stdin must never be inherited: it is the MCP channel.
    r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
    if check and r.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


def codex(args, cwd, model=None):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "last.txt"
        cmd = [CODEX, "exec", "-C", str(cwd), "-o", str(out)] + (["-m", model or MODEL] if model or MODEL else [])
        run(cmd + args, cwd)
        return out.read_text().strip() if out.exists() else ""


def git_root(path):
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute() or not p.is_dir():
        raise ValueError(f"repo must be an absolute path to an existing directory: {path!r}")
    return Path(run(["git", "rev-parse", "--show-toplevel"], p).strip())


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


def codex_review(args):
    root, task = git_root(args.get("repo")), text_arg(args, "task")
    base = str(args.get("base") or "HEAD")
    if base.startswith("-"):
        raise ValueError("base must be a git ref")
    run(["git", "rev-parse", "--verify", "--quiet", base + "^{commit}"], root)
    diff = run(["git", "diff", base], root)
    for f in run(["git", "ls-files", "--others", "--exclude-standard", "-z"], root).split("\0"):
        if f:  # untracked files are part of the change too
            diff += run(["git", "diff", "--no-index", "--", "/dev/null", f], root, check=False)
    if not diff.strip():
        return f"No changes to review against {base}."
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n[diff truncated; read the files for the rest]\n"
    url = url_arg(args)
    visual = VISUAL_URL.format(url=url) if url else VISUAL_AUTO
    prompt = REVIEW_PROMPT.format(task=task, visual=visual, diff=diff)
    report, looked = app_server_turn(prompt, root, model_arg(args), sandbox="read-only", output_schema=VERDICT)
    try:
        v = json.loads(report)
    except json.JSONDecodeError:
        raise RuntimeError(f"Codex did not return a verdict:\n{report[-1500:]}") from None
    head = "APPROVED" if v["approved"] else "CHANGES REQUESTED"
    issues = "\n".join(f"- [{i['severity']}] {i['file']}:{i['line']} {i['comment']}" for i in v["issues"])
    nxt = "" if v["approved"] else "\n\nFix the blocker/major issues, then call codex_review again."
    seen = f"\n(Codex checked it in the browser: {looked} Computer Use actions)" if looked else ""
    return f"Codex review: {head}\n{v['summary']}\n{issues}{nxt}{seen}\n\n{json.dumps(v)}"


def codex_task(args):
    root, task = git_root(args.get("repo")), text_arg(args, "task")
    if args.get("isolate", True) is False:
        report = codex(["-s", "workspace-write", task], root, model=model_arg(args))
        return f"Codex report:\n{report}\n\nWorking tree now:\n{run(['git', 'status', '--short'], root)}"
    tid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    branch, wt = f"codex/{tid}", HOME / "worktrees" / tid
    run(["git", "worktree", "add", "-b", branch, str(wt), "HEAD"], root)
    report = codex(["-s", "workspace-write", task + "\n\nDo not commit; your changes are committed for you."], wt, model=model_arg(args))
    run(["git", "add", "-A"], wt)
    stat = run(["git", "diff", "--cached", "--stat"], wt)
    if not stat.strip():
        run(["git", "worktree", "remove", "--force", str(wt)], root)
        run(["git", "branch", "-D", branch], root)
        return f"Codex made no changes.\n\nCodex report:\n{report}"
    run(["git", "commit", "-q", "-m", f"codex: {task.splitlines()[0][:60]}"], wt)
    return (f"Codex report:\n{report}\n\nBranch: {branch}\nWorktree: {wt}\n{stat}\n"
            f"Review: git diff HEAD...{branch}\nMerge: git merge {branch}\n"
            f"Discard: git worktree remove --force {wt} && git branch -D {branch}")


def codex_ask(args):
    root = git_root(args.get("repo"))
    return codex(["-s", "read-only", text_arg(args, "question")], root, model=model_arg(args)) or "(Codex returned no answer)"


def projectless_dir(task):
    """Same place and naming as a Codex app "projectless chat", so the run shows up in the Codex sidebar."""
    words = re.findall(r"[a-z0-9]+", task.lower())[:6]
    return Path.home() / "Documents" / "Codex" / time.strftime("%Y-%m-%d") / ("-".join(words)[:80] or "new-chat")


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
    report, approved = app_server_turn(prompt, out, model_arg(args))
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


def app_server_turn(prompt, cwd, model, sandbox="workspace-write", output_schema=None):
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
        thread = srv.call("thread/start", {"cwd": str(cwd), "model": model or MODEL, "sandbox": sandbox,
                                           "approvalPolicy": APPROVALS, "ephemeral": True,
                                           "config": {"sandbox_workspace_write": {"network_access": True}}})
        tid = (thread.get("thread") or {}).get("id") or thread.get("threadId")
        turn_params = {"threadId": tid, "input": [{"type": "text", "text": prompt}]}
        if output_schema:
            turn_params["outputSchema"] = output_schema
        srv.call("turn/start", turn_params)
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


HANDLERS = {"codex_review": codex_review, "codex_task": codex_task, "codex_ask": codex_ask,
            "codex_computer": codex_computer}

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
        if m == "tools/call":  # each call on its own thread, so Claude can run reviews and tasks in parallel
            threading.Thread(target=call_tool, args=(id_, params), daemon=True).start()
            continue
        if id_ is None:
            continue  # notification
        if m == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "codex-relay", "version": "1.0.0"},
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
