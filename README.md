# codex-relay

Codex as a tool for Claude. Claude writes the code, Codex reviews it, and Claude fixes the issues until Codex approves. You never leave the Claude app.

## Install

You need the [Codex CLI](https://github.com/openai/codex), signed in (`codex login`), and `python3`.

In Claude (desktop app Code tab, or `claude` in a terminal):

```
/plugin marketplace add airman416/codex-relay
/plugin install codex-relay@codex-relay
```

Restart the session. That is all.

## Use

Ask Claude as you normally do:

> Add rate limiting to /login, then loop with Codex review until it approves.

> Have Codex write the migration for the new `orders` table while you do the API.

> Ask Codex whether this caching approach has a race condition.

| Tool | What it does |
|---|---|
| `codex_review` | Codex reviews your uncommitted changes (or everything since `base`, e.g. `main`) and returns a verdict: approved, plus issues with severity, file, and line. |
| `codex_task` | Codex does a task on its own git worktree and `codex/<id>` branch, so your checkout is never touched. Claude gets the report and can review or merge it. |
| `codex_ask` | A read-only second opinion from Codex. |

Settings (env vars): `CODEX_MODEL`, `CODEX_STEP_TIMEOUT` (default 1200 s), `CODEX_RELAY_HOME` (default `~/.codex-relay`).

## Test

```
python3 tests/test_codex_mcp.py
```
