# codex-relay

Claude writes the code. Codex does the computer use in the Codex app: it clicks through the running app to test a change, takes screenshots, and saves page DOM, then hands the result back to Claude. No copying PRs between agents, and you never leave the Claude app.

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
6. Tell me to start a new session, and give me one example prompt to try codex_computer.
```

## Install (by hand)

You need the Codex desktop app, the [Codex CLI](https://github.com/openai/codex) signed in (`codex login`), and `python3`.

In Claude (desktop app Code tab, or `claude` in a terminal):

```
/plugin marketplace add airman416/codex-relay
/plugin install codex-relay@codex-relay
```

Restart the session.

## Use

Ask Claude as you normally do:

> Add a Sign up button to the home page, then have Codex test it at localhost:3000 and fix anything it finds.

> Have Codex go through Timeback, screenshot the leaderboard and save its DOM, then use them in the video.

The plugin gives Claude one tool, `codex_computer`. Each call opens a **new chat in the Codex app** with the task typed in and the in-app browser at the page. **Press Enter** to start it, then watch Codex work. When Codex finishes, Claude gets its report and the files it saved. The chats live under a "codex-relay" project in the Codex app (`~/Documents/Codex/codex-relay`).

The one Enter is required: the Codex app only pre-fills prompts that come from a link.

Settings (env vars): `CODEX_STEP_TIMEOUT` (default 1200 s for Codex to finish), `CODEX_RELAY_START_WAIT` (default 300 s to press Enter), `CODEX_RELAY_APP_DIR` (default `~/Documents/Codex/codex-relay`).

## Test

```
python3 tests/test_codex_mcp.py
```
