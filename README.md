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

> Add a Sign up button to the home page (running at localhost:3000), then loop with Codex review until it approves.

> Have Codex write the migration for the new `orders` table while you do the API.

> Ask Codex whether this caching approach has a race condition.

> Have Codex open Timeback in Chrome, screenshot the leaderboard and save its DOM, then use them in the video.

| Tool | What it does |
|---|---|
| `codex_review` | Codex reviews your uncommitted changes (or everything since `base`, e.g. `main`) and returns a verdict: approved, plus issues with severity, file, and line. For visible changes it also opens the running app in Chrome and checks it (give Claude the URL, e.g. `localhost:3000`). |
| `codex_task` | Codex does a task on its own git worktree and `codex/<id>` branch, so your checkout is never touched. Claude gets the report and can review or merge it. |
| `codex_ask` | A read-only second opinion from Codex. |
| `codex_computer` | Codex uses its browser and computer use (open a site, click through it, take screenshots, save the DOM) and returns the files it saved. Runs sandboxed with network access; it can only write to its output folder. |

Every tool takes an optional `model`, so Claude can pick per call, for example: *"have Codex do this with gpt-6-astra"*. `codex_computer` needs the Codex desktop app with Computer Use set up (it uses Codex's own browser and Computer Use runtime). No other setup: Computer Use actions are approved only for the duration of each `codex_computer` call, approvals from any other tool are declined, and shell commands stay in Codex's sandbox.

Settings (env vars): `CODEX_MODEL`, `CODEX_STEP_TIMEOUT` (default 1200 s), `CODEX_RELAY_HOME` (default `~/.codex-relay`).

## Test

```
python3 tests/test_codex_mcp.py
```
