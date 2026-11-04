# Kandu guide

[فارسی](GUIDE.fa.md) · Before this: [what Kandu does for you](ONBOARDING.md)

This guide takes you from nothing to your first task done by the hive, then through the daily work. Every step ends on a check you can run. The full specification is in [DESIGN.md](../DESIGN.md), and the words used here are in [GLOSSARY.md](../GLOSSARY.md).

## Contents
1. What you need
2. Install your own private copy
3. First setup, by conversation
4. Start the dashboard
5. Set up Didban on the machine you sit at
6. Add your first project
7. Define your first task
8. Start the hive
9. Read the dashboard
10. When Kandu needs you
11. Day to day
12. Turning on auto-merge
13. Updating Kandu
14. When something is wrong

## 1. What you need

- **The brain:** an always-on Linux machine with `systemd`, such as a small home server. The supervisor runs there. It can also be a Windows PC: see [Kandu on Windows](WINDOWS.md) for what differs.
- **The machine you sit at:** your laptop. It can be the brain too, if it stays on.
- [Claude Code](https://claude.com/claude-code) (`claude`), logged in, with a subscription or an API key.
- [GitHub CLI](https://cli.github.com/) (`gh`), logged in with the `project` scope: `gh auth refresh -s project`.
- `git` and `python` 3.
- SSH from your laptop to the brain, and from the brain to any machine where a project must run `in-place`.

Check: `claude --version`, `gh auth status` and `python3 --version` all answer on the brain.

## 2. Install your own private copy

The public repo is a template. Keep your own private repo with the public one as `upstream`, so your tasks and PRs on Kandu itself stay private. On the brain:

```
git clone https://github.com/25mordad/kandu.git kandu
cd kandu
git remote rename origin upstream
git remote set-url --push upstream DISABLED
gh repo create your-login/kandu-private --private --source=. --remote=origin --push
git config core.hooksPath .githooks
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Kandu always runs from its own `.venv`, never from the system Python.

Check: `git remote -v` shows `upstream` with push `DISABLED`.

## 3. First setup, by conversation

In the `kandu` folder on the brain, open Claude Code and run:

```
/kandu-setup
```

There is no form. Kandu looks at your system first, then asks one numbered list of what it could not find out: where the brain runs, your machines, your usage limits, your GitHub owners, notifications and language, how many workers run at once, the dashboard's port, a model per role, and what each role may do.

Two answers matter most at the start:
- **Usage limits.** If you share your subscription with your own sessions, leave a margin, for example 70% for the 5-hour window and 60% for the week.
- **Permissions.** Without `permissions`, every agent may run any command. To limit them, start from the commented set in `config.example/kandu.example.yaml`.

It writes `local/kandu.yaml`, `local/.env` (with a dashboard token it does not show) and `local/private-words.txt`, the words that must never reach a public repo. All of `local/` is gitignored.

Check: `.venv/bin/python -m kandu.config` passes and `.venv/bin/python -m kandu.doctor` shows no `FAIL`.

## 4. Start the dashboard

```
.venv/bin/python -m kandu.timer --dashboard
```

This starts only the dashboard, as a user service that comes back after a restart. It does not start the supervisor yet. On a server, run `loginctl enable-linger` once, so user services start at boot.

The dashboard listens on `127.0.0.1:8790` on the brain. From your laptop, open a tunnel and then `http://127.0.0.1:8790`:

```
ssh -N -L 127.0.0.1:8790:127.0.0.1:8790 server
```

Reading is open on that address. Stop, resume and approve ask for `KANDU_DASHBOARD_TOKEN` from `local/.env` the first time you use them.

Check: the page opens and shows the meadow, empty.

## 5. Set up Didban on the machine you sit at

Didban is how you talk to Kandu from your own machine. It is a Claude Code session in the control role: it defines tasks, shows state and gives commands, and writes no code. On your laptop, in a clone of Kandu with its `.venv`:

```
mkdir -p local
cp config.example/didban.example.yaml local/didban.yaml
ln -s "$PWD/bin/didban" ~/.local/bin/didban
```

Edit `local/didban.yaml`: `reach` is the SSH alias of the brain and `install_dir` is where Kandu lives there. Everything else is read from the brain.

You can skip this step if you sit at the brain, or open Didban from the dashboard's button instead, which runs it on the brain with a Remote Control link.

Check: `didban` opens a session named `Kandu`, and it tells you what is waiting.

## 6. Add your first project

Pick a small project with real tests for the first one. In Didban, or in Claude Code on the brain:

```
/kandu-add
```

Kandu reads the project first, its remote, its test command, how it deploys, and then asks you to confirm. Some answers it never guesses:
- **Where it runs:** `clone` (Kandu clones it into its own work folder on the brain) or `in-place` (on a named machine, in a separate `git worktree`, never in your own working copy).
- **Merge mode:** leave auto-merge off for now.
- **Deploy:** `direct` or `preview`, and the live URL for the final check. Leave the URL out if the live software is behind a login.
- **Forbidden areas,** such as migrations or payments, and where its secrets are. Kandu stores where a secret is, never its value.

If the project has no board, Kandu creates one only when you say so, with the standard columns: `Backlog`, `Technical debt`, `Ready`, `In progress`, `In review`, `Needs info`, `Needs split`, `Blocked`, `Done`.

Check: `.venv/bin/python -m kandu.doctor` shows the project's copy, its test tool and its board columns as `OK`.

## 7. Define your first task

In Didban, say what you want in your own words, in any language. Or run `/kandu-task`.

Kandu first reads the code the task touches and checks that the behavior does not exist yet. For a large or vague task it asks numbered questions, each with a recommended answer. Then it shows you the final text. The task is created only after your yes.

A task is written in English on GitHub, whatever language you speak, and always has the same sections: goal, current behavior, acceptance criteria, how to test, out of scope, and `Blocked by`. Kandu sets its `Priority`, `Kind` and `Size`. A `Large` task is split into slices before it reaches the board.

What makes a task good:
- it says what the software must do, not which files to change,
- each acceptance criterion can be checked on its own,
- "out of scope" stops the worker from growing the task.

Check: the task is on the board in `Backlog`, with the label `agent`.

## 8. Start the hive

```
.venv/bin/python -m kandu.timer --enable
```

This starts the timer and the dashboard together. The supervisor now wakes every hour (`wake_every_minutes`). To start a cycle now, press **Wake the queen** on the dashboard. To see what a cycle would do without running any agent:

```
.venv/bin/python -m kandu.supervisor --dry-run
```

What happens to your task:
1. The **splitter** sizes it.
2. The **queen** releases it from `Backlog` to `Ready`, at the pace the project can take.
3. A **worker** builds it in its own worktree, tests first, and opens a PR on a `kandu/...` branch.
4. A fresh **reviewer** checks the PR against the criteria. The worker fixes what it found, for at most two rounds.
5. With every merge condition met on the reviewed commit, the PR **merges** if auto-merge is on, or waits for you if it is off.
6. Kandu **deploys** and runs the **live check**. Then the task is `Done`.

A cycle ends when the tasks run out, about 50 minutes pass, or a usage limit is reached. A quiet cycle costs no tokens.

## 9. Read the dashboard

**The meadow** is the first tab. Each project is a patch, each task a flower, each running agent a bee: worker, reviewer, scout and splitter each carry their own tool. The queen glows and speaks while she decides. Every state of a task has its own flower; its size is the task's size, and signs under it show a hard-to-undo change, a return from review, a slice, a missing size or an open question. A PR that waits for you stands out most. Finished tasks fill the comb under the meadow for five days. Weeds are tasks parked in `Technical debt`. The legend explains each sign as it appears.

The bees are a playful picture, not biology: a real queen supervises no one. See [why bees](ONBOARDING.md#why-bees).

**Tasks** is a table per project that folds and can be searched. What needs you comes first. A click opens a task, with its PR page in plain words: what it does, the review's result, a one-line recommendation and each finding explained.

**Daily usage** shows what was done each day and what each task cost in tokens, by model and by role, plus your Didban sessions and free flights.

**Live stream** shows every file an agent reads, every command and edit, the reviewer's notes and the supervisor's decisions as they happen.

At the top: the supervisor's state, the next wake, why the last cycle stopped, the usage bars with their limit lines, and the controls.

## 10. When Kandu needs you

Kandu asks instead of guessing. You hear about these on Telegram, if you set it up, and see them first on the dashboard:

| What you see | What it means | What you do |
|---|---|---|
| A glowing flower, a PR waiting | Auto-merge is off, or the change is hard to undo | Read its PR page, then **Approve and merge**, or merge on GitHub |
| `Needs split` | The splitter found the task too large and proposes slices | Read the proposal and **Accept this split**, or change it in Didban |
| `Needs info` | The task was too vague to start | Answer in Didban |
| An open question | The review met something the task never settled | Didban opens with it; your answer is saved in the project's memory |
| `Blocked` | The queen could not move it, or a deploy or live check failed | Read why in the task's dialog, then decide in Didban |

Your approval holds for one commit. If a new commit lands, the review and your yes start over.

## 11. Day to day

- **Something urgent:** tell Didban "this goes next". It sets `High`, and the queen starts it before any other waiting task in that project.
- **An idea for later:** tell Didban to park it. It goes to `Technical debt`, which no agent reads, and grows as a weed in the meadow so the column stays small.
- **Pause a project:** the switch on its patch in the meadow, or "pause this project for a week" in Didban. Nothing touches it until you resume it.
- **Stop everything:** **Stop** on the dashboard. The running step finishes, nothing after it starts. **Resume** continues.
- **Change the limits:** **change** beside the usage bars. The next cycle uses the new values.
- **Change anything else:** `/kandu-config`, by conversation.
- **Take the wheel yourself:** **Open a free flight** on any task. It opens a Claude Code session on the brain with full access, told where the task's worktree and logs are. The hive keeps running beside it.

## 12. Turning on auto-merge

Auto-merge starts off. Turn it on per project with `/kandu-config` once you have seen that Kandu decides as you would: you would have made the same review comments and merged without editing. Even with it on, a change that is hard to undo always waits for you.

## 13. Updating Kandu

```
git fetch upstream && git merge upstream/main
git push origin main
```

An update never touches `local/`. After an update that changes the units, press **Restart Kandu** on the dashboard.

## 14. When something is wrong

- Run `.venv/bin/python -m kandu.doctor`. It checks access, machines, `gh`, `claude`, the private guard and each project, at no token cost.
- `.venv/bin/python -m kandu.supervisor status` shows the supervisor's state.
- A machine that is off is not an error: its tasks show as waiting and run in the next cycle where it answers.
- If Kandu itself fails, such as a crashed run or a GitHub outage, the task stays where it was and the next cycle tries again. Only after three such failures in a row does it stop in `Blocked`.
- `.venv/bin/python -m kandu.sync --queue` shows changes still waiting to reach GitHub, and any it dropped.
- Nothing is ever reverted on its own. If the live software breaks, Kandu reports it, and you decide.
