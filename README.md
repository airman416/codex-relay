# codex-relay

Claude writes the code. Codex does the computer use in the Codex app: it clicks through the running app to test a change, takes screenshots, and saves page DOM, then hands the result back to Claude. No copying PRs between agents, and you never leave the Claude app.

## How it works for you

1. You ask Claude for something that needs computer use, for example "test the new Sign up button at localhost:3000".
2. Claude hands it to Codex. A **new chat opens in the Codex app**, with the task already typed in and the in-app browser at the page.
3. **You press Enter in that Codex chat.** This is the only thing you do.
4. Codex does the work in the Codex app while you watch.
5. When Codex finishes, its report and the files it saved go back to Claude automatically. Claude continues, for example it fixes the bug that Codex found.

**Why you must press Enter:** Claude could start Codex by itself only with Codex's permission checks turned off (no sandbox, no approvals), which is not safe. And only a chat in the Codex app has Codex's fast in-app browser. So Claude opens the chat with the task typed in, and you start it with one Enter.

**Good to know:**
- Each handoff opens a new chat, so each needs its own Enter. If Claude fixes something and asks Codex to test again, press Enter in the new chat too.
- If nobody presses Enter within 5 minutes, Claude gets a "not started" message and tells you.
- Claude waits while Codex works, and Codex cannot ask Claude questions during the task. Codex reports once, when it finishes.
- If Codex asks for a permission in the Codex app (for example to control another app), answer it there.
- The chats appear under a "codex-relay" project in the Codex app, and the files go to `~/Documents/Codex/codex-relay/<date>/`.

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
6. Explain to me in 2 short lines: each handoff to Codex opens a new Codex app chat that I start with
   Enter, because starting Codex without me would turn off its permission checks, and only the Codex
   app chat has the fast in-app browser.
7. Tell me to start a new session, and give me one example prompt to try codex_computer.
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

The plugin gives Claude one tool, `codex_computer`. Each call opens a new Codex app chat; press Enter there (see [How it works for you](#how-it-works-for-you)).

Settings (env vars): `CODEX_STEP_TIMEOUT` (default 1200 s for Codex to finish), `CODEX_RELAY_START_WAIT` (default 300 s to press Enter), `CODEX_RELAY_APP_DIR` (default `~/Documents/Codex/codex-relay`).

## Test

```
python3 tests/test_codex_mcp.py
```
