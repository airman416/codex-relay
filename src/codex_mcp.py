#!/usr/bin/env python3
"""codex-relay: gives a Claude agent Codex as a reviewer and a second worker, over MCP (stdio).

Install once, as a plugin, in the Claude desktop app (Code tab) or the claude CLI:
  /plugin marketplace add airman416/codex-relay
  /plugin install codex-relay@codex-relay

Then ask Claude as usual, e.g. "add rate limiting to /login, loop with Codex review until it approves".

Tools
  codex_review  Codex reviews the working-tree diff (or the diff against a base ref) and returns a JSON verdict.
  codex_task    Codex does a task on its own git worktree and branch, so it never touches Claude's checkout.
  codex_ask     A read-only second opinion from Codex about the repo.
  codex_computer  Codex uses its browser / computer use (e.g. screenshots and DOM of a web app) and saves files.
                  Runs through `codex app-server`, so Codex's Computer Use approvals come back to this server.

Env: CODEX_BIN (codex), CODEX_MODEL (Codex default), CODEX_STEP_TIMEOUT (1200 s),
     CODEX_RELAY_HOME (~/.codex-relay, holds the worktrees).
"""
import json, os, queue, subprocess, sys, tempfile, threading, time, uuid
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

REVIEW_PROMPT = """You are reviewing a diff (on stdin) that another agent wrote for this task:

TASK: {task}

Check correctness, bugs, security, missing edge cases, and whether the diff actually does the task.
Read files in the repo for context if needed, but do not edit anything. Approve only if there are no
blocker or major issues; minor issues alone do not block. Use line 0 when an issue has no single line.
Reply with the JSON verdict only."""

INSTRUCTIONS = """Codex (OpenAI's coding agent) is available as a reviewer and a second worker.
Review loop: after you change code, call codex_review with the repo and what the change must do.
Fix every blocker and major issue, then call codex_review again. Stop when it approves or after 3 rounds,
and tell the user about any issue you chose not to fix and why.
Use codex_task to hand off independent work. It runs on its own branch, so read its diff before you merge it.
Use codex_ask for a second opinion on a design or a bug.
Use codex_computer for browser or computer-use work (open a site, click through it, take screenshots,
save the DOM). Prefer model gpt-6-astra for it. It returns the files it saved; use them in your work."""

REPO = {"type": "string", "description": "Absolute path to a directory inside the git repository."}
MODEL_ARG = {"type": "string", "description": "Optional Codex model for this call, e.g. gpt-6-astra (strongest; use for computer use, browser work and hard reviews), gpt-6.1-sol (default workhorse), gpt-6-luna (fast, cheap). Omit to use the user's Codex default."}
TOOLS = [
    {"name": "codex_review",
     "description": "Codex reviews the current uncommitted changes (tracked and untracked) or, with `base`, "
                    "everything since that ref. Returns a JSON verdict {approved, summary, issues[]}. "
                    "Fix blocker/major issues and call again until approved.",
     "inputSchema": {"type": "object", "required": ["repo", "task"], "properties": {
         "repo": REPO,
         "task": {"type": "string", "description": "What the change is supposed to do, so Codex can judge it."},
         "base": {"type": "string", "description": "Optional git ref, e.g. main. Default: HEAD (uncommitted work)."},
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
                                                  "under ~/.codex-relay/computer/."},
         "model": MODEL_ARG}},
     "annotations": {"openWorldHint": True}},
]


def run(cmd, cwd, stdin=None, check=True):
    # stdin must never be inherited: it is the MCP channel.
    io = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, **io)
    if check and r.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


def codex(args, cwd, stdin=None, schema=None, model=None):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "last.txt"
        cmd = [CODEX, "exec", "-C", str(cwd), "-o", str(out)] + (["-m", model or MODEL] if model or MODEL else [])
        if schema:
            (Path(d) / "schema.json").write_text(json.dumps(schema))
            cmd += ["--output-schema", str(Path(d) / "schema.json")]
        run(cmd + args, cwd, stdin)
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
    v = json.loads(codex(["-s", "read-only", REVIEW_PROMPT.format(task=task)], root, diff, VERDICT, model_arg(args)))
    head = "APPROVED" if v["approved"] else "CHANGES REQUESTED"
    issues = "\n".join(f"- [{i['severity']}] {i['file']}:{i['line']} {i['comment']}" for i in v["issues"])
    nxt = "" if v["approved"] else "\n\nFix the blocker/major issues, then call codex_review again."
    return f"Codex review: {head}\n{v['summary']}\n{issues}{nxt}\n\n{json.dumps(v)}"


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


def codex_computer(args):
    task = text_arg(args, "task")
    out = Path(str(args.get("dir") or HOME / "computer" / (time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4])))
    out = out.expanduser()
    if not out.is_absolute() or out.resolve() in (Path("/"), Path.home().resolve()):
        raise ValueError(f"dir must be an absolute folder other than / or your home folder: {out}")
    out.mkdir(parents=True, exist_ok=True)
    before = {p for p in out.rglob("*") if p.is_file()}
    prompt = (f"{task}\n\nUse your browser or computer-use tools. Save every file you produce in {out}. "
              "Give each file the extension that matches its real format. When done, close any tabs or windows "
              "you opened, then list each file you saved and what it shows.")
    report, approved = app_server_turn(prompt, out, model_arg(args))
    new = sorted(str(p) for p in out.rglob("*") if p.is_file() and p not in before)
    files = "\n".join(new) or "(no files saved)"
    return f"Codex report:\n{report}\n\nFolder: {out}\nNew files:\n{files}\n({approved} Computer Use actions approved)"


# Computer Use only asks for approval through a client, so `codex exec` (approval: never) always gets "not
# approved". `codex app-server` sends each approval to us as an MCP elicitation. We accept ones from the
# Computer Use / browser runtime (cua_repl) and decline the rest; shell commands stay inside the sandbox.
COMPUTER_SERVERS = {"cua_repl"}
APPROVALS = {"granular": {"mcp_elicitations": True, "rules": False, "sandbox_approval": False,
                          "request_permissions": False, "skill_approval": False}}


def app_server_turn(prompt, cwd, model):
    p = subprocess.Popen([CODEX, "app-server"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in p.stdout] + [lines.put(None)], daemon=True).start()
    deadline, ids, texts, approved = time.time() + TIMEOUT, iter(range(1, 10**6)), [], 0

    def send(msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    def pump(until):
        """Read messages, answering server requests, until `until(msg)` returns a value."""
        nonlocal approved
        while True:
            try:
                line = lines.get(timeout=max(1, deadline - time.time()))
            except queue.Empty:
                raise subprocess.TimeoutExpired("codex app-server", TIMEOUT)
            if line is None:
                raise RuntimeError("codex app-server exited early")
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in m and "id" in m:  # request from Codex to us
                prm = m.get("params") or {}
                if m["method"] == "mcpServer/elicitation/request":
                    ok = prm.get("serverName") in COMPUTER_SERVERS
                    approved += ok
                    send({"id": m["id"], "result": {"action": "accept" if ok else "decline", "content": {}}})
                else:
                    send({"id": m["id"], "error": {"code": -32000, "message": "not allowed by codex-relay"}})
                continue
            if m.get("method") == "item/completed" and (m["params"].get("item") or {}).get("type") == "agentMessage":
                texts.append(m["params"]["item"].get("text", ""))
            got = until(m)
            if got is not None:
                return got

    def call(method, params):
        i = next(ids)
        send({"id": i, "method": method, "params": params})
        r = pump(lambda m: m if m.get("id") == i and "method" not in m else None)
        if "error" in r:
            raise RuntimeError(f"codex app-server {method}: {r['error'].get('message')}")
        return r["result"]

    try:
        call("initialize", {"clientInfo": {"name": "codex-relay", "version": "1.2.0"},
                            "capabilities": {"experimentalApi": True}})
        send({"method": "initialized"})
        thread = call("thread/start", {"cwd": str(cwd), "model": model or MODEL, "sandbox": "workspace-write",
                                       "approvalPolicy": APPROVALS, "ephemeral": True,
                                       "config": {"sandbox_workspace_write": {"network_access": True}}})
        tid = (thread.get("thread") or {}).get("id") or thread.get("threadId")
        call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": prompt}]})
        turn = pump(lambda m: m["params"] if m.get("method") == "turn/completed" else None)
        err = (turn.get("turn") or {}).get("error")
        report = texts[-1] if texts else "(Codex returned no message)"
        return (f"{report}\n\nCodex error: {err}" if err else report), approved
    finally:
        p.kill()


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
