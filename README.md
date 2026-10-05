# codex-relay

Codex as a tool for Claude. Claude writes the code, Codex reviews it, and Claude fixes the issues until Codex approves. You never leave the Claude app.

## Install (let Claude do it)

Paste this into Claude (desktop app Code tab, or `claude` in a terminal):

```
Install the codex-relay plugin for me. Do these steps in order and stop to tell me if one fails:
1. Run `python3 --version`. If python3 is missing, tell me to install it and stop.
2. Run `codex --version`. If it is missing, install it with `npm install -g @openai/codex`
   (or `brew install --cask codex` if npm is missing).
3. Run `codex login status`. If I am not logged in, tell me to run `codex login` myself in a
   terminal, wait for me to say done, then check again.
4. Run `claude plugin marketplace add airman416/codex-relay`
   then `claude plugin install codex-relay@codex-relay`.
5. Run `claude mcp list` and confirm the codex-relay server shows "Connected".
6. Tell me to start a new session, and give me one example prompt to try the Codex review loop.
```

## Install (by hand)

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

Every tool takes an optional `model`, so Claude can pick per call, for example: *"have Codex do this with gpt-6-astra"*. Note: Codex's browser and computer use only work in the Codex desktop app, not through this plugin.

Settings (env vars): `CODEX_MODEL`, `CODEX_STEP_TIMEOUT` (default 1200 s), `CODEX_RELAY_HOME` (default `~/.codex-relay`).

## Test

```
python3 tests/test_codex_mcp.py
```
