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

Env: CODEX_BIN (codex), CODEX_MODEL (Codex default), CODEX_STEP_TIMEOUT (1200 s),
     CODEX_RELAY_HOME (~/.codex-relay, holds the worktrees).
"""
import json, os, subprocess, sys, tempfile, threading, time, uuid
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
Use codex_ask for a second opinion on a design or a bug."""

REPO = {"type": "string", "description": "Absolute path to a directory inside the git repository."}
TOOLS = [
    {"name": "codex_review",
     "description": "Codex reviews the current uncommitted changes (tracked and untracked) or, with `base`, "
                    "everything since that ref. Returns a JSON verdict {approved, summary, issues[]}. "
                    "Fix blocker/major issues and call again until approved.",
     "inputSchema": {"type": "object", "required": ["repo", "task"], "properties": {
         "repo": REPO,
         "task": {"type": "string", "description": "What the change is supposed to do, so Codex can judge it."},
         "base": {"type": "string", "description": "Optional git ref, e.g. main. Default: HEAD (uncommitted work)."}}},
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
                     "description": "false = Codex edits the repo's working tree directly (no branch, no commit)."}}}},
    {"name": "codex_ask",
     "description": "Ask Codex a question about the repo (design, bug hunt, second opinion). Read-only.",
     "inputSchema": {"type": "object", "required": ["repo", "question"], "properties": {
         "repo": REPO, "question": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
]


def run(cmd, cwd, stdin=None, check=True):
    # stdin must never be inherited: it is the MCP channel.
    io = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, **io)
    if check and r.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


def codex(args, cwd, stdin=None, schema=None):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "last.txt"
        cmd = [CODEX, "exec", "-C", str(cwd), "-o", str(out)] + (["-m", MODEL] if MODEL else [])
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
    v = json.loads(codex(["-s", "read-only", REVIEW_PROMPT.format(task=task)], root, diff, VERDICT))
    head = "APPROVED" if v["approved"] else "CHANGES REQUESTED"
    issues = "\n".join(f"- [{i['severity']}] {i['file']}:{i['line']} {i['comment']}" for i in v["issues"])
    nxt = "" if v["approved"] else "\n\nFix the blocker/major issues, then call codex_review again."
    return f"Codex review: {head}\n{v['summary']}\n{issues}{nxt}\n\n{json.dumps(v)}"


def codex_task(args):
    root, task = git_root(args.get("repo")), text_arg(args, "task")
    if args.get("isolate", True) is False:
        report = codex(["-s", "workspace-write", task], root)
        return f"Codex report:\n{report}\n\nWorking tree now:\n{run(['git', 'status', '--short'], root)}"
    tid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    branch, wt = f"codex/{tid}", HOME / "worktrees" / tid
    run(["git", "worktree", "add", "-b", branch, str(wt), "HEAD"], root)
    report = codex(["-s", "workspace-write", task + "\n\nDo not commit; your changes are committed for you."], wt)
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
    return codex(["-s", "read-only", text_arg(args, "question")], root) or "(Codex returned no answer)"


HANDLERS = {"codex_review": codex_review, "codex_task": codex_task, "codex_ask": codex_ask}

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
