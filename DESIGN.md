# Kandu — design

Status: **early development; the main loop is built, but has not run on a real project yet.** This file is the full specification. The overview is in [README.md](README.md), the build roadmap in [TASKS.md](TASKS.md).

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
9. Which port does the dashboard use (default 8790)?

The dashboard itself is always Kandu's own (§7); only its port is a choice.

### B. Adding a project — each time (`/kandu-add`)
1. Name, one line on what it is, which family it belongs to.
2. Which repos, and who owns them.
3. Where exactly it is: which machine, which path.
4. Permission to run: may Kandu clone it into its own work folder and run it there (`clone`), or must it work on that machine, in a separate `git worktree` (`in-place`)? **Always asked, never inferred.**
5. Does it have a board? Which one, and how do its columns map to Kandu's standard columns? If none, should one be created?
6. How is it tested and run locally, and which extra tools do its workers need (`allowed_tools`)? (The LLM proposes, the user confirms.)
7. How is it deployed: `direct` or `preview`? Which live URL is used for the final check?
8. Which secrets does it need and where are they? Kandu only stores the location, never copies or logs secret values.
9. Forbidden areas: e.g. migrations, payments, files that must never be touched.
10. Existing repo rules: CI, PR bots, other people working on the repo. Kandu respects them.
11. Merge in this project: automatic or with approval?
12. Priority relative to the other projects.
13. Project terms: the LLM proposes a short glossary from what it read, the user corrects it (§14).

### C. Other conversational commands
- `/kandu-task` — define a task; writes the issue with the standard template onto the right board (§2).
- `/kandu-config` — change config: "pause project X for a week", "set the weekly limit to 50%", "remove this project".
- `/kandu-doctor` — health check in code (`python -m kandu.doctor`, no tokens): access, machines, `gh`, `claude`, the private guard, each project's copy, test tool and board columns.

The rules shared by these commands are in [docs/COMMANDS.md](docs/COMMANDS.md). What must hold for a task is enforced in `kandu/tasks.py`: the template's sections, and no private word in a public repo.

## 2. Task intake
- The user describes a task in a Claude session (`/kandu-task`). Claude writes it as a GitHub **issue** with a fixed template ([prompts/templates/issue.md](prompts/templates/issue.md)):
  - Goal
  - Current behavior
  - Acceptance criteria
  - How to test
  - Out of scope
  - `Blocked by` (if any)
- Claude adds the issue to the right board with status `Ready` and label `agent`. Only `agent`-labelled issues enter the loop.
- On a public repo the issue is public too: no private details in the task text.
- A precise task (clear acceptance criteria) and real tests in the project matter more for the result than the loop itself. That is why defining tasks is a conversation, not a form.

What makes a task good:
- **Behavior, not steps.** It says what the software must do, not how to change it. No file paths or line numbers: they go stale before the worker starts.
- **Each acceptance criterion can be checked on its own**, and "Out of scope" stops the worker from growing the task.
- **One slice.** A task is sized to one fresh context and delivers a complete path that can be verified alone, not one layer of a feature. Refactoring that makes the work easier comes first, as its own task.
- **A wide mechanical change** (a rename across the whole codebase) is the exception: add the new form beside the old, move callers over in batches, then delete the old form. Each is a task, and each leaves the tests green.
- **The order is explicit.** `Blocked by` names real issues, so the supervisor can always see which tasks are ready. A task whose blockers are all done can start.

Before writing the issue, `/kandu-task` checks that the behavior does not already exist and that it was not rejected earlier (§14). For a large or vague task it interviews the user in rounds: numbered questions, each with a recommended answer, asking only what can be decided now. It looks up facts itself and asks the user only for decisions. A small, clear task skips the interview.

## 3. Boards
- **One GitHub Project board per project family**, owned by the GitHub user or org the repos live in. One family can hold many repos; the built-in `Repository` field tells the supervisor which repo an issue is in.
- Standard columns on every board, so the supervisor handles them all the same way: `Ready`, `In progress`, `In review`, `Needs info`, `Blocked`, `Done`. An existing board with other columns is mapped onto these during `/kandu-add`, under `board.columns`.
- Kandu creates a board only when the user says so.
- A task only a person can do stays on the board without the `agent` label, so it is visible and Kandu leaves it alone.

## 4. Project registry
One entry per project in `local/projects.yaml`, written by `/kandu-add`. **A project not in the registry is never touched.** Fields (see [config.example/projects.example.yaml](config.example/projects.example.yaml)):
- repos and board
- **run location**, set by the user per project, never inferred:
  - `clone` — Kandu may clone it into its own work folder on the brain's machine and run it there;
  - `in-place` — must run on a named machine, in a separate `git worktree`, never in the user's own working copy.
- machine, path, test command, local run command, extra `allowed_tools` for its workers
- a project with several repos keeps each at `<path>/<repo name>`
- `deploy`: `mode` (`direct` or `preview`), `command`, `live_url`
- where its secrets are, forbidden areas, repo rules, merge mode, priority, max parallel workers, paused

**Machines that are not always on** (e.g. a laptop): at the start of each cycle the supervisor checks the machine over SSH. Reachable → run the worker there. Not reachable → skip that project's tasks; they show as "waiting for machine" and are picked up the next cycle it is reachable. Leaving the machine on is enough to let Kandu work on it.

**Worktrees** sit beside the copy, in `<path>.kandu-worktrees/`, never inside it, so they never show in the user's `git status`. A worker's worktree is kept between runs, so a cut-off run leaves its work for the next one. A reviewer gets a separate checkout of exactly the head `SHA`.

## 5. Supervisor, workers, reviewer

```
supervisor ─┬─ worker 1  →  reviewer
            ├─ worker 2  →  reviewer
            └─ ...
                 ↓
   supervisor: final check → merge → deploy → check in the live software
```

- **Supervisor:** a code loop on the always-on machine ([kandu/supervisor.py](kandu/supervisor.py)), not an LLM session. Everything that needs no judgment runs there, at no token cost, and still works when Claude is down: gates, task selection, starting agents, review rounds, the `SHA` check. It **writes no code**. A headless `claude -p` session is started only where judgment is needed, such as the final check (task 12). It never grades its own work.
- **Workers:** separate `claude` processes, started by the runner layer (§9) on the project's machine, each in its own `git worktree`, so they never write over each other. Separate processes are what let Kandu stream each worker (§7), stop one alone and record its tokens. Independent tasks (by `Blocked by`) run in parallel, up to `max_parallel_workers`; dependent ones run in order. Their instructions are in [prompts/worker.md](prompts/worker.md):
  - Tests come first, at the project's public interfaces, and they check behavior rather than internals.
  - A bug starts with one fast command that fails on exactly that bug. If no such command can be built, the task goes to `Needs info` instead of a guess.
  - Every PR uses [prompts/templates/pr.md](prompts/templates/pr.md): a small summary, before and after evidence, and the blast radius. A worker runs inside another project, so the runner puts this template in its prompt.
- **Live checker:** a fresh `claude` process ([prompts/live-check.md](prompts/live-check.md)) opens the live version after deploy and reports `pass` or `fail`. It changes nothing.
- **Reviewer:** every PR gets one review by a fresh `claude` process ([prompts/reviewer.md](prompts/reviewer.md)). "Fresh" means it knows nothing of how the PR was built; it does get the project's rules and memory. It reads the diff, searches the whole repo and writes a report; it changes no code.
  - **Blockers:** a bug that breaks production, a security hole, a crash or unhandled error, an unmet or unproven acceptance criterion, a change out of scope or in a forbidden area, and new logic with no test that really runs it. There is no coverage percentage.
  - **Notes:** smells and broken documented rules that are not blockers. Cosmetics, small design issues and product decisions are not reported at all.
  - **Verified:** each blocker is pressure-tested before it is reported: the head is checked again, the bug is separated from the stated reason, every caller in the repo is traced, and a path that really reaches the bug is found.
  - **Companion PRs:** an open PR in the same family is a companion if a PR body names it or its branch has the same slug. The reviewer checks that the two sides really work together, not only that the names match. Nothing else is assumed to be a companion.
  - **Report:** one JSON object, validated in code. It holds the reviewed `SHA`, each criterion's result, each finding with path, line and kind, what was checked and found sound, the door, and a plain brief for the dashboard in the language chosen at setup. Nothing found is a valid result.
  - **Rounds:** every finding goes back to a fresh worker process on the same branch and worktree, which fixes all of them, blockers and notes alike. The kind only decides what stops a merge. A later review runs only if a code check sees a new head `SHA`, so waiting costs no tokens. It gives each earlier finding `fixed`, `not fixed` or `partly fixed`, judged from the code. After `max_review_rounds` fix rounds (default 2, so at most three reviews), or a fix round that adds no commit, an open blocker sends the task to `Blocked`, and an open note stays on the PR.
  - **Record:** each review is a PR comment with a hidden marker, and only comments by Kandu's own account count, so a restart loses nothing.
- **Merge danger:** the reviewer decides on its own whether a change is a **two-way door** (easy to undo) or a **one-way door** (hard to undo: data migrations, published APIs, sent messages), and states its blast radius. A one-way door waits for the user, even with `auto_merge` on.
- **Merge:** enforced in code, on the exact `SHA` that was reviewed. A new commit voids the review. If `auto_merge` is on, the supervisor merges only when all of this holds, then moves on to the next task:
  - the tests pass on that `SHA`, CI is not failing and the PR is not a draft
  - no blocker and no review thread is open, and every acceptance criterion is met
  - it is a two-way door
  - no companion PR is open and unreviewed. Whether a side is live cannot be told in code, so an unreviewed companion always waits
  - the project is not `frozen`

  These always wait for the user, with a one-line question: a blocker answered without a code change, work deferred to a later PR, a thread resolved by someone other than its author, a risky area (auth, payments, data migrations) with no tests, and a one-way door. Default in this template: `auto_merge` **off**. The user turns it on per project once calibration (§15) shows Kandu decides as they would.
- **Deploy:** after merge, whoever merged, per the project's `deploy` setting, from the default branch in a separate checkout. Only this step gets the project's secrets. A failed deploy sends the task to `Blocked`; nothing is reverted.
- **"Done" means seen in the live software:** after deploy the supervisor opens the live version (screenshot or test) and checks that what the issue asked for is really there. Green tests alone are not enough.
- **End-of-cycle check:** after two or more merges in a project in one cycle, rerun its tests on the default branch and request its live URL. The result goes in the event log as `project_checked`.
- **No automatic revert.** `auto_revert: true` is refused by the config loader. If something breaks in the live software, the supervisor only reports it. The user decides.
- **Who may be trusted:** a worker uses the same GitHub account as Kandu, so it could write a review comment. The merge gate therefore reads the review from the local event log, and `gh pr merge --match-head-commit` makes GitHub refuse any other head.
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
- **PR page:** for each review round, in the language chosen at setup and in plain words: the title, what the PR does, the result and a one-line recommendation, then each finding explained. The user can decide in seconds.
- **By day:** what was done on each day and how many tokens each task used, split by model and by worker, reviewer and supervisor. Tokens come from the `result` event of each run's `stream-json` output and are saved with the task.
- **Controls from day one:** stop / resume the supervisor, stop one worker.
- **Internal API:** the page reads and acts only through Kandu's own HTTP API (`/api/v1`), which covers state, tasks, tokens by day, an event stream and stop / resume. No outside system depends on it. To show the dashboard inside another app, that app's server proxies the page into a frame, so no new port opens. `docs/API.md` documents every endpoint and event with real examples, and tests check it against the code.
- **Look:** the page is a hive. The supervisor is the queen, each agent is a bee (worker, reviewer, scout), each task is a flower in its project's patch, usage is honey in two cells, and a day's tokens are a honeycomb. Every bee and flower comes from real state: a bee flies only while a real agent runs, and with reduced motion on nothing flies. It is inline `SVG` and `CSS`, with no library and no outside request.
- **Languages:** the page is in English by default, with a switch to Persian that goes right to left; each browser remembers its choice. The API stays in English and the page translates the values it knows. Tests check that both languages have every text. Agent-written text, such as a review brief, stays in the language chosen at setup.
- **Access control:** reading is open on the bound address. Stop and resume need `KANDU_DASHBOARD_TOKEN` from `local/.env`; with none set, controls are refused.
- **Port:** the dashboard takes `dashboard.port`, default 8790, on the brain's machine; keep it free for Kandu. `python -m kandu.timer --dashboard` starts it as a user service that comes back after a restart, without starting the supervisor's timer. The service needs `loginctl enable-linger` to start at boot.
- Binds to `127.0.0.1` by default. From another machine use an SSH tunnel: `ssh -L 8790:127.0.0.1:8790 server`, then open `http://127.0.0.1:8790`. To keep the tunnel up, run it as a user service on that machine. A system on the same machine calls the API directly. Exposing it further (private network, tunnel, reverse proxy) is the user's choice.

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
- Free guards: a command allowlist built in code for each run ([kandu/agents.py](kandu/agents.py)), `--permission-mode dontAsk` so anything else is refused, a list of forbidden paths (e.g. `~/.ssh`, other projects' private data), and never `--dangerously-skip-permissions`.
- In the code, running a worker sits behind its own layer ([kandu/runner.py](kandu/runner.py)), so isolation can be added later by swapping only that layer.
- Stopping an agent over SSH ends the local connection; the remote process dies at its next write to it, not at once.

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
- **Recommended install:** a private repo of your own with this public repo as `upstream` and pushes to `upstream` disabled, so issues, PRs and mistakes stay private. A GitHub fork of a public repo cannot be private. Steps are in [README.md](README.md#recommended-run-kandu-from-your-own-private-copy).
- Updating Kandu is `git fetch upstream && git merge upstream/main` in the recommended install, and `git pull` in a plain clone. Nothing in `local/` is ever touched by an update.
- **Private guard:** `.githooks` checks new content, file names and commit messages against `local/private-words.txt` of the main working tree, at commit and again at push, because a cherry-pick or a rebase passes no commit hook. Text in a compressed file or in UTF-16 is not seen.

## 13. Didban — control from your own machine
The brain runs on an always-on machine, which is usually not the machine the user sits at. **Didban** (Persian for "lookout", the one at the top of the tower) is how the user talks to Kandu from their own machine: one command opens a Claude Code session in a clone of this repo, in the control role. It is a role, not a service: no daemon, no new connection, nothing to keep running.

- **Defining tasks:** `/kandu-task` writes the issue directly with `gh`. The brain picks it up on its next wake, so this works even when the brain's machine is off.
- **Seeing state:** reads the boards from GitHub and live state from the HTTP API (§7) over SSH to the brain. Answers `Needs info` tasks from here.
- **Giving commands:** stop, resume and config changes (`/kandu-config`) run on the brain over SSH.
- **Knowledge:** project locations come from the registry. Other sources are listed in `didban.knowledge` in the brain's `kandu.yaml`. The role prompt tells Didban to read them there, on the machine that holds each one, read-only, and never to copy them.
- **Source of truth:** `local/` on the brain's machine. Didban reads and writes it over SSH, so there are never two diverging copies. The only local file is `local/didban.yaml`, which says how to reach the brain ([example](config.example/didban.example.yaml)). The list of sources is in `didban:` in the brain's `kandu.yaml`.
- **Limits:** it does not write code, does not change the knowledge sources it reads and keeps no memory of its own; all state stays on the board. `Edit`, `Write` and `NotebookEdit` are blocked in code. `Bash` stays open because Didban needs SSH, so these limits rest on the role prompt there, and a mistake is possible: do not treat Didban as a security boundary. Its sessions count against the same usage limits as the supervisor (§6).
- **When the brain's machine is off:** tasks can still be defined; live state and commands wait until it is back.

## 14. Harness, context and memory
Every part of Kandu is designed and reviewed against these three. The result depends on them as much as on the model.

- **Harness:** everything around the model that keeps it safe and on track: gates, lock file, permissions, tests, the fresh reviewer, limits, stop controls. A rule that matters is enforced in code, not only written in a prompt. Checks that need no judgment run without an LLM, so they cost nothing and still work when Claude is down. When an agent makes a mistake a fixed pattern could catch, the fix is an automated check, not a new line in a document; documents are for judgment calls.
- **Context:** each agent gets the smallest context that lets it do its job, and nothing else. A worker gets its issue and its project's facts, not the history of other tasks. A reviewer starts fresh. Project rules are checked at review, because the reviewer has the most room in its context and the worker the least. Prompts live in files, so they can be read, reviewed and improved, and they follow [docs/WRITING-PROMPTS.md](docs/WRITING-PROMPTS.md). Output that is not needed stays in logs, out of the model's context.
- **Memory:** nothing important lives only in a session. State is on the board, in issues and in state files. What is learned is saved as plain files per project in `local/`, read by the next worker and open to the user to correct. Memory is private: it never enters the public repo, and it holds facts and lessons, never secrets. An entry that proves wrong is removed. Each project's memory holds:
  - **Lessons:** what the next worker should know.
  - **Glossary:** one word for each project concept, so agents spend fewer tokens and name things the same way. Kandu's own is [GLOSSARY.md](GLOSSARY.md).
  - **Decisions:** one short paragraph for a choice that is hard to reverse, surprising without context and a real trade-off. Nothing else is worth recording.
  - **Rejected requests:** what the user said no to and why, so it is not proposed again.
  - **Do not flag:** what the reviewer must not report in this project: known contracts, intentional config, product decisions, and every wrong finding the user corrected. It is updated as soon as a correction happens.

## 15. Model review
Kandu must be able to show how well the model is doing, from its own records.

- Periodically (default weekly) a fresh-context reviewer goes through the tasks of that period and writes a report for the dashboard.
- Measures per model and task type: tokens per task, review rounds, tasks that reached `Blocked`, PRs the user corrected afterwards, and breakage seen after merge.
- It compares prompts and models over time, so it is clear whether a change helped.
- **Calibration:** every review round is logged, even with no findings, with what was offered, what the user kept and what they dropped. The target is about 100 PRs per project before full automation, and the measure is plain: the user would have made the same comments and merged without editing. This is the evidence for turning on `auto_merge`.
- It also looks at the environment, not only the model, and lists what would have prevented the problems it found, most severe first: an automated check that was missing, a rule the reviewer should enforce, a prompt line that changes nothing and can go, a fact the agent could not reach, a tool call that wasted tokens.
- It recommends; the user decides. It never changes a model, prompt or limit on its own.

## Sources
Several practices here (task slicing, merge danger, the check-over-document rule, the writing rules for prompts) are adapted from [mattpocock/skills](https://github.com/mattpocock/skills), MIT licensed. They are rewritten for Kandu; nothing from that repo is installed or run.
