# Kandu — design

Status: **early development, Kandu does not run yet.** This file is the full specification. The overview is in [README.md](README.md), the build roadmap in [TASKS.md](TASKS.md).

Everything that belongs to one person's setup (machines, paths, accounts, projects, limits) lives in config files under `local/`, which is gitignored. Examples: [config.example/](config.example/).

## Contents
1. Setup and configuration by conversation
2. Task intake
3. Boards
4. Project registry
5. Supervisor, workers, reviewer
6. Rhythm
7. Dashboard
8. Notifications
9. Isolation
10. GitHub identity
11. Safety rules
12. Config and updates
13. Didban — control from your own machine
14. Harness, context and memory
15. Model review

## 1. Setup and configuration — by conversation, no forms

Everything is configured by talking to an LLM in a Claude Code session. There are no forms. The LLM **first inspects the system itself** and only asks what it cannot find out. For example, given a project path it reads the git remote, looks for existing boards, finds the test command in the project's files and works out how the project deploys, then asks the user to confirm. The result is plain YAML files in `local/` that can also be edited by hand. Each step ends with a real check that everything works.

### A. Initial setup — once per install (`/kandu-setup`)
1. Where does Kandu's brain (the supervisor) run: this machine or another one? Is that machine always on?
2. Which other machines are on the network, how are they reached (SSH alias), and which of them are always on?
3. Claude access: subscription or API key? Usage limits: 5-hour window %, weekly %.
4. GitHub account, and the owners (users / orgs) Kandu may work in.
5. Where notifications go (Telegram or nowhere), and in which language.
6. Defaults: number of parallel workers, wake interval, auto-merge on or off.
7. Prerequisites: `claude`, `gh`, `git`, `python` installed and logged in; `gh` needs the `project` scope for boards. Kandu says what is missing.
8. Didban (§13): which other sources may it read, and on which machine? On the machine you sit at, how is the brain reached?

The dashboard is not a setup question: it is always Kandu's own (§7).

### B. Adding a project — each time (`/kandu-add`)
1. Name, one line on what it is, which family it belongs to.
2. Which repos, and who owns them.
3. Where exactly it is: which machine, which path.
4. Permission to run: may Kandu clone it into its own work folder and run it there (`clone`), or must it work on that machine, in a separate `git worktree` (`in-place`)? **Always asked, never inferred.**
5. Does it have a board? Which one, and how do its columns map to Kandu's standard columns? If none, should one be created?
6. How is it tested and run locally? (The LLM proposes, the user confirms.)
7. How is it deployed: `direct` or `preview`? Which live URL is used for the final check?
8. Which secrets does it need and where are they? Kandu only stores the location, never copies or logs secret values.
9. Forbidden areas: e.g. migrations, payments, files that must never be touched.
10. Existing repo rules: CI, PR bots, other people working on the repo. Kandu respects them.
11. Merge in this project: automatic or with approval?
12. Priority relative to the other projects.

### C. Other conversational commands
- `/kandu-task` — define a task; writes the issue with the standard template onto the right board (§2).
- `/kandu-config` — change config: "pause project X for a week", "set the weekly limit to 50%", "remove this project".
- `/kandu-doctor` — health check: access, machines, `gh`, `claude`, each project's test command.

## 2. Task intake
- The user describes a task in a Claude session (`/kandu-task`). Claude writes it as a GitHub **issue** with a fixed template:
  - Goal
  - Acceptance criteria
  - How to test
  - `Blocked by` (if any)
- Claude adds the issue to the right board with status `Ready` and label `agent`. Only `agent`-labelled issues enter the loop.
- On a public repo the issue is public too: no private details in the task text.
- A precise task (clear acceptance criteria) and real tests in the project matter more for the result than the loop itself. That is why defining tasks is a conversation, not a form.

## 3. Boards
- **One GitHub Project board per project family**, owned by the GitHub user or org the repos live in. One family can hold many repos; the built-in `Repository` field tells the supervisor which repo an issue is in.
- Standard columns on every board, so the supervisor handles them all the same way: `Ready`, `In progress`, `In review`, `Needs info`, `Blocked`, `Done`. An existing board with other columns is mapped onto these during `/kandu-add`.
- Kandu creates a board only when the user says so.

## 4. Project registry
One entry per project in `local/projects.yaml`, written by `/kandu-add`. **A project not in the registry is never touched.** Fields (see [config.example/projects.example.yaml](config.example/projects.example.yaml)):
- repos and board
- **run location**, set by the user per project, never inferred:
  - `clone` — Kandu may clone it into its own work folder on the brain's machine and run it there;
  - `in-place` — must run on a named machine, in a separate `git worktree`, never in the user's own working copy.
- machine, path, test command, local run command, deploy command, live URL
- `deploy: direct` or `deploy: preview`
- where its secrets are, forbidden areas, repo rules, merge mode, priority, max parallel workers, paused

**Machines that are not always on** (e.g. a laptop): at the start of each cycle the supervisor checks the machine over SSH. Reachable → run the worker there. Not reachable → skip that project's tasks; they show as "waiting for machine" and are picked up the next cycle it is reachable. Leaving the machine on is enough to let Kandu work on it.

## 5. Supervisor, workers, reviewer

```
supervisor ─┬─ worker 1  →  reviewer
            ├─ worker 2  →  reviewer
            └─ ...
                 ↓
   supervisor: final check → merge → deploy → check in the live software
```

- **Supervisor:** runs on the always-on machine as a headless `claude -p` session. It **does not write code**. It picks tasks, hands them to workers, judges results and decides. It never grades its own work.
- **Workers:** separate subagents, each in its own `git worktree`, so they never write over each other. Independent tasks (by `Blocked by`) run in parallel, up to `max_parallel_workers`; dependent ones run in order.
- **Reviewer:** a fresh-context reviewer for every PR checks the diff against the issue's acceptance criteria. Findings go back to the same worker. After `max_review_rounds` (default 2) the task goes to `Blocked`.
- **Merge:** if `auto_merge` is on and the supervisor concludes the PR is right (tests pass, reviewer approves, acceptance criteria met), it merges and moves on to the next task. The user supervises the supervisor and corrects it when it is wrong. Default in this template: **off**.
- **Deploy:** after merge, per the project's `deploy` setting.
- **"Done" means seen in the live software:** after deploy the supervisor opens the live version (screenshot or test) and checks that what the issue asked for is really there. Green tests alone are not enough.
- **End-of-cycle check:** after several merges, rerun all tests and a smoke test on the whole project.
- **No automatic revert** (`auto_revert: false`). If something breaks in the live software, the supervisor only reports it. The user decides.
- **Vague task:** no guessing. The task moves to `Needs info` and the user gets a question.
- **No hidden memory:** all state lives on the board and in issue comments. A cycle that is cut off resumes where it stopped, and progress is visible on the board at any time.

## 6. Rhythm — usage-based, not a fixed number of tasks
- A `systemd timer` wakes the supervisor every `wake_every_minutes` (default 60). A lock file prevents two supervisors running at once.
- No `Ready` task, or usage over a limit → sleep immediately, at no cost.
- Otherwise work until the tasks run out, or `max_cycle_minutes` (default 50) pass, or usage hits a limit, whichever comes first.
- **Limits** (`claude.limits`): 5-hour window % and weekly %. With a subscription shared with your own interactive sessions, leave a margin (example values: 70% and 60%). On a subscription, usage is read at no cost with `claude -p "/usage"`.
- Usage is recorded per task, and reported per day and per project, so it is clear what each task cost and how much of the limit each project used.

## 7. Dashboard — Kandu's own
The user must be able to see exactly what is happening: the supervisor and every worker. The dashboard is part of Kandu, standalone, and **interactive**.
- **Supervisor state:** awake or asleep, next wake time, why the last cycle stopped; two usage bars (5-hour, weekly) with the limit lines.
- **Claude login:** when the login expires on each machine Kandu uses, with days left. Read from `refreshTokenExpiresAt` in `~/.claude/.credentials.json` (only that field; tokens are never read into logs or state). The field is internal to Claude Code, so if it is missing the dashboard shows "unknown" instead of guessing.
- **Active workers:** task number, project, machine, phase (implement / test / review round N / merge / deploy / live check), elapsed time.
- **Live stream:** workers run with `--output-format stream-json`, so every file read, command and edit is logged and shown live, along with the reviewer's notes and the supervisor's decisions ("sent back because…").
- **Queue per project:** `Ready`, waiting for machine, `Needs info`, `Blocked`.
- **Done:** PR link, live link, screenshot of the final check, tokens used.
- **By day:** what was done on each day and how many tokens each task used, split by model and by worker, reviewer and supervisor. Tokens come from the `result` event of each run's `stream-json` output and are saved with the task.
- **Controls from day one:** stop / resume the supervisor, stop one worker.
- **Integration interface:** Kandu's own dashboard uses only the public HTTP API, so everything it shows is available to any other system. The API is versioned (`/api/v1`) and covers state, tasks, tokens by day, an event stream and stop / resume. `docs/API.md` documents every endpoint and event with real examples, and a change to the API must update it. Tests check the two against each other.
- **Access control:** reading is open on the bound address. Stop and resume need a token from `local/.env`.
- Binds to `127.0.0.1` by default. From another machine use an SSH tunnel: `ssh -L 8790:127.0.0.1:8790 server`, then open `http://127.0.0.1:8790`. A system on the same machine calls the API directly. Exposing it further (private network, tunnel, reverse proxy) is the user's choice.

## 8. Notifications
- Only the important things: the supervisor stopped, something broke, a task is stuck and needs a decision, a usage limit was hit, the Claude login expires soon (`claude.login_warn_days`, default 7) or has stopped working, and things of similar weight. Everything else only on the dashboard.
- The login check never calls `claude`, so the warning still goes out when Claude itself is down.
- The notifier is pluggable; Telegram is the first adapter. Messages are sent in the language chosen at setup.

## 9. Isolation — none in this version
Considered and deliberately left out of v1.

Note on the trade-off: a container does not by itself use more tokens, since token use depends on what the agent reads and does. The real cost is setup and upkeep: every tool has to be passed into the container (`gh`, SSH to other machines, deploy tools such as `wrangler`, a browser for screenshots, each project's secrets).

v1:
- No container, no separate OS user. Kandu runs as the user.
- A separate work folder for Kandu's clones, never mixed with the user's own working copies.
- Free guards: a per-project command allowlist in Claude's settings, a list of forbidden paths (e.g. `~/.ssh`, other projects' private data), and never `--dangerously-skip-permissions`.
- In the code, running a worker sits behind its own layer, so isolation can be added later by swapping only that layer.

If you want isolation now, two options:
- **Separate Linux user:** a `kandu` user that owns the work folder and only has access to the registered repos and their secrets. Simple, little overhead; isolation is only as good as file permissions.
- **Docker (or a devcontainer):** each worker in a container with only its project mounted. Strongest isolation; you must provide every tool and credential inside the container and keep the image up to date.

## 10. GitHub identity — the user's own account
- v1 pushes, opens PRs and merges with the user's own GitHub account (through `gh`).
- Everything Kandu does is marked: branch `kandu/<issue>-<slug>`, label `kandu`, and a `Kandu-Run:` line in the commit message, so Kandu's work is easy to tell apart in the history.
- If you want Kandu's work separated from your own: create a bot account or a GitHub App, give it a fine-grained token limited to the registered repos, and point `gh` for Kandu at that token. Repo rules that count PRs per author then stay unaffected by Kandu.

## 11. Safety rules
- Never `--dangerously-skip-permissions`.
- Never touch an unregistered project, and never touch a user's own working copy.
- Never copy or log secret values.
- Never take external actions (sending email or messages, payments, paid services) without the user's approval.
- Never revert, force-push or delete branches other than its own `kandu/` branches.
- Respect each repo's own rules (CI, PR bots, protected branches).

## 12. Config and updates
- `local/` (gitignored) holds everything personal: `kandu.yaml`, `projects.yaml`, `.env` (secrets) and any personal notes.
- [config.example/](config.example/) has complete examples. `/kandu-setup` and `/kandu-add` write `local/` for you.
- Updating Kandu is `git pull`; nothing in `local/` is ever touched by an update.
- **Recommended install:** a private repo of your own with this public repo as `upstream` and pushes to `upstream` disabled, so issues, PRs and mistakes stay private. A GitHub fork of a public repo cannot be private. Steps are in [README.md](README.md#recommended-run-kandu-from-your-own-private-copy).

## 13. Didban — control from your own machine
The brain runs on an always-on machine, which is usually not the machine the user sits at. **Didban** (Persian for "lookout", the one at the top of the tower) is how the user talks to Kandu from their own machine: one command opens a Claude Code session in a clone of this repo, in the control role. It is a role, not a service: no daemon, no new connection, nothing to keep running.

- **Defining tasks:** `/kandu-task` writes the issue directly with `gh`. The brain picks it up on its next wake, so this works even when the brain's machine is off.
- **Seeing state:** reads the boards from GitHub and live state from the HTTP API (§7) over SSH to the brain. Answers `Needs info` tasks from here.
- **Giving commands:** stop, resume and config changes (`/kandu-config`) run on the brain over SSH.
- **Knowledge:** project locations come from the registry. Other sources the user lists in `local/` are read on the machine that holds them, read-only, and never copied.
- **Source of truth:** `local/` on the brain's machine. Didban reads and writes it over SSH, so there are never two diverging copies. The only local file is `local/didban.yaml`, which says how to reach the brain ([example](config.example/didban.example.yaml)). The list of sources is in `didban:` in the brain's `kandu.yaml`.
- **Limits:** it does not write code, does not change the knowledge sources it reads and keeps no memory of its own; all state stays on the board. Its sessions count against the same usage limits as the supervisor (§6).
- **When the brain's machine is off:** tasks can still be defined; live state and commands wait until it is back.

## 14. Harness, context and memory
Every part of Kandu is designed and reviewed against these three. The result depends on them as much as on the model.

- **Harness:** everything around the model that keeps it safe and on track: gates, lock file, permissions, tests, the fresh reviewer, limits, stop controls. A rule that matters is enforced in code, not only written in a prompt. Checks that need no judgment run without an LLM, so they cost nothing and still work when Claude is down.
- **Context:** each agent gets the smallest context that lets it do its job, and nothing else. A worker gets its issue and its project's facts, not the history of other tasks. A reviewer starts fresh. Prompts live in files, so they can be read, reviewed and improved. Output that is not needed stays in logs, out of the model's context.
- **Memory:** nothing important lives only in a session. State is on the board, in issues and in state files. What is learned is saved as plain files per project in `local/`, read by the next worker and open to the user to correct. Memory is private: it never enters the public repo, and it holds facts and lessons, never secrets. An entry that proves wrong is removed.

## 15. Model review
Kandu must be able to show how well the model is doing, from its own records.

- Periodically (default weekly) a fresh-context reviewer goes through the tasks of that period and writes a report for the dashboard.
- Measures per model and task type: tokens per task, review rounds, tasks that reached `Blocked`, PRs the user corrected afterwards, and breakage seen after merge.
- It compares prompts and models over time, so it is clear whether a change helped.
- It recommends; the user decides. It never changes a model, prompt or limit on its own.
