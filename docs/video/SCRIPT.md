# Kandu demo film — script

[فارسی](SCRIPT.fa.md)

Status: **approved and filmed.** The words actually spoken are in [demo/narration.en.txt](../../demo/narration.en.txt) and [demo/narration.fa.txt](../../demo/narration.fa.txt); the Persian is colloquial, so it differs from the Voice lines below. [demo/README.md](../../demo/README.md) says how to make the film again.

## What the film is

About 2 minutes 20 seconds in English and 2 minutes 45 in Persian. A screen recording of Kandu's real dashboard on a demo world: a made-up GitHub in memory, and a director that writes, step by step, the same events the supervisor writes in a real run. Nothing in it comes from a real setup: no real machine, account, project or screen.

Two films come from the same run: one with the dashboard in English and an English voice, one with the dashboard in Persian and a Persian voice.

## The demo world

- **Projects:** two made-up projects, so the meadow has two patches. `pocket-notes`, a small notes web app, carries the story. `tea-shop` has a few tasks in other states, so the meadow looks lived in.
- **GitHub:** a stand-in in memory ([demo/world.py](../../demo/world.py)) that answers the dashboard's questions: boards, issues, PRs and review comments. Nothing reaches github.com.
- **Agents:** none run. The director records each phase, review and finished run through Kandu's own `StateStore`, and writes each agent's stream the way `claude` writes it, so the run window fills live.
- **Didban:** scene 2 is a page that plays a Didban session from a script ([demo/terminal.html](../../demo/terminal.html)); no real session is filmed.
- **Usage:** fixed numbers that rise a little through the film.
- **Recording:** Chrome in kiosk mode on a virtual screen (`Xvfb`) of 1920×1080, with an empty profile, captured with `ffmpeg`. Nothing of the machine it runs on is in the picture.
- **Voice:** Gemini TTS, voice `Kore`, one call per language, cut into scenes on the silences.

## Scenes

Each scene lists what is on screen and the words spoken over it. Times are approximate.

### 1. The problem — 0:00 to 0:12
**Screen:** the meadow with two patches, quiet. Title: "Kandu".
**Voice:** "Coding agents write good code. But working with one still means sitting next to it: approving each step, watching it, checking it really finished. Kandu takes you out of that chair."

### 2. Say what you want — 0:12 to 0:35
**Screen:** a terminal. `didban` opens. The user types: "the notes list should be sortable by date". Didban reads the code, asks two numbered questions with a recommended answer each, shows the final task text, the user types "yes", and the issue appears on the board.
**Voice:** "You describe a task in your own words. Didban, Kandu's control session, reads the code first, asks only what it cannot find out, and writes a precise task with acceptance criteria. Nothing reaches the board without your yes."

### 3. The hive wakes — 0:35 to 0:55
**Screen:** the meadow. A new flower grows in the `pocket-notes` patch. Click **Wake the queen**. The splitter bee visits the flower and it takes its size. The queen glows, her brief appears, and the flower moves to ready.
**Voice:** "Every hour the hive wakes. A splitter bee measures each new task. The queen, the one agent that plans for all your projects, releases work at the pace each project can take. When there is nothing to decide, she costs nothing."

### 4. Workers build — 0:55 to 1:15
**Screen:** two worker bees fly to two flowers. Open the worker's run window: files read, a test written first, the test failing, the code, the test passing, a PR opened on a `kandu/` branch.
**Voice:** "Workers build the tasks side by side, each in its own copy of the repo. Tests come first. You can watch every file they read and every command they run, live."

### 5. A fresh review — 1:15 to 1:35
**Screen:** back to the meadow. A reviewer bee visits the PR's flower. A sign appears under it: sent back. Open the task: one blocker, explained in plain words, "the sort has no test for notes with the same date". The worker bee returns, then the reviewer again, and the sign clears.
**Voice:** "Every PR gets a fresh reviewer that knows nothing of how it was built. It checks the result against your criteria and verifies each problem before it reports it. The worker fixes it, and the second review only checks those fixes."

### 6. Done means live — 1:35 to 1:50
**Screen:** the flower leaves the field and fills a cell in the comb. Open the cell: the task, its outcome, and what each role cost in time and tokens.
**Voice:** "When every condition holds on the exact commit that was reviewed, Kandu merges and deploys. Then it opens the live software and checks the task is really there. Only then is it done."

### 7. When it needs you — 1:50 to 2:10
**Screen:** in the `tea-shop` patch, a flower glows: its PR changes the database, a one-way door. Open its PR page: what it does, the review's result, a one-line recommendation. Click **Approve and merge**.
**Voice:** "Some changes are hard to undo, like a database migration. Those always wait for you. You read what the change does in plain words, decide in seconds, and Kandu does the rest."

### 8. You stay in control — 2:10 to 2:28
**Screen:** **Daily usage**: tokens per task, by role. The usage bars with their limit lines. Flip the pause switch on one patch and it fades. Point at **Stop**.
**Voice:** "You see what every task cost. Work stops at the usage limits you set, so your own sessions still have room. Pause a project, or stop the whole hive, with one click."

### 9. Close — 2:28 to 2:40
**Screen:** the meadow, busy. Text: "Kandu — open source. github.com/25mordad/kandu".
**Voice:** "You write tasks. Kandu builds, reviews, ships and checks them, and asks you when it should. Kandu is open source. The guide is in the repo. And no, real queen bees don't boss anyone around. Ours plays boss, just a little."
