# HTTP API

Version 1. Kandu's own dashboard uses only this API, so anything it shows is open to any other system. A change that breaks a response moves to `/api/v2`.

Start it with `.venv/bin/python -m kandu.dashboard`. It binds to `dashboard.bind` and `dashboard.port` from `local/kandu.yaml`. All responses are JSON, except `/stream`. An error is `{"error": "message"}`.

Reads are open on the bound address. Every `POST` needs `Authorization: Bearer <token>`, where the token is `KANDU_DASHBOARD_TOKEN` in `local/.env`. With no token set, controls are refused.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/api/v1/state` | `current.json` (see [STATE.md](STATE.md)), plus `stop_requested`, `stopping` (after Stop, how many bees still finish their step: the tasks the cycle drives, its queen run and Bazras; 0 once none is at work or while not stopped), `paused_projects`, `paused_in_config`, `project_order`, `project_settings`, `pace`, `over_limit`, `timer_on`, `language`, `limits`, `queen`, `flowers`, `waits_for_you`, `bazras`, `backup`, `drop_blockers`, `notify` (`adapter`, `events`, `kinds`: every kind there is, `language`, and `telegram`: null unless the adapter is Telegram, else `token_set`, `chat_set`, `bot` (the bot's name, once known), `buttons`, `listening` (it takes button presses now) and `error` (what Telegram last answered when it failed, or null); never the bot's token or the chat), `show_requests`, `default_look`, `looks` (every look the dashboard has, in the Look menu's order), `clean_merged`, `upstream`, `version`, `version_badge`, `upgrade`, `machines` (each with `name`, `always_on`, `down_since`: when it stopped answering, as the supervisor last saw it, or null, `run_tmp`: whether each run there gets its own run temporary folder, and `run_tmp_dir`: the folder they go in, or null), `unreachable_machines` (from `current.json`: each machine that does not answer, with `machine`, `since` and the `tasks` that wait for it, to be sized or worked) and `github_refused`: while GitHub refuses Kandu for a rate limit, `{"at": ISO time Kandu calls it again, "why": GitHub's answer, first line}`, otherwise null |
| GET | `/api/v1/queue` | each project's board columns, with `Done` holding only what closed in the last `done_days` days, read from GitHub at most every 15 minutes, however many pages ask, and sooner once a task finishes or moves |
| GET | `/api/v1/boards` | every board in the config, with its projects, columns, fields and cards, read from Kandu's copy of GitHub, so it costs GitHub nothing; `?fresh=1` reads each board into the copy first, at most once a minute |
| GET | `/api/v1/days` | dates that have events |
| GET | `/api/v1/days/{day}` | one day's tokens by task, model and role, and its finished tasks |
| GET | `/api/v1/usage` | tokens over a range: by project with its tasks under it, Kandu's own runs, the sessions, and by role and model |
| GET | `/api/v1/bazras` | every project in priority order: whether Bazras looks at it (`enabled`), its hour (`at`), `paused_in_config`, and its newest reports, newest first, each with `date`, `all_well`, `findings` (a count) and `text`; `default_at` is `bazras.at` |
| GET | `/api/v1/didban` | the last Didban sessions: when, title, tokens and what each did; counted in no limit |
| GET | `/api/v1/requests` | the open requests by project, in priority order, then those whose project is not named yet (`project` null): each with `id`, `text` and its decision `map` (`decided`, `asked`, `later`, `out`), or null when it has none; read from local state only, so it costs GitHub nothing. While `show_requests` is off: `{"show_requests": false, "projects": []}` |
| POST | `/api/v1/requests` | `enabled=true` or `false`: show the open requests on the dashboard, or hide them, in `kandu.yaml`; off is the default |
| POST | `/api/v1/default-look` | `look=classic`, `classic-new` or `clean`: the look a browser opens in until it chooses its own, written as `dashboard.look` in `kandu.yaml`; Classic is the default; answers `{"default_look": "clean"}`. A browser's own choice always wins over it |
| GET | `/api/v1/flights` | the sessions opened from this page, each with its status (`opening`, `open`, `failed`) and link, and the last free flights that ended, with tokens; counted in no limit |
| POST | `/api/v1/flights/didban` | open Didban on the brain, or keep the one already open; `?language=en` or `fa` is the language it speaks; `&project=NAME` opens it on that project, for Bazras's reports |
| POST | `/api/v1/flights/zanbourdar` | open Zanbourdar on the brain: a session with full access tied to no task, or keep the one already open; `?language=` as above |
| POST | `/api/v1/flights/kanduban/{project}` | open a Kanduban on that project's machine, in a new worktree, with full access, or keep the one already open; `?language=` as above. `404` for an unknown project; `503` with the reason when its machine does not answer, and then no session is recorded |
| POST | `/api/v1/flights/tasks/{owner}/{repo}/{number}` | open a free flight on a task, with full access, or keep the one already open; `?language=` as above |
| POST | `/api/v1/flights/{key}/close` | end one of those sessions |
| GET | `/api/v1/events` | a page of events; `day` (default: the last) and `after` (default: 0) |
| GET | `/api/v1/stream` | new events as they happen, as Server-Sent Events |
| GET | `/api/v1/runs/{run_id}` | what one agent run did: files read, commands, edits |
| GET | `/api/v1/tasks/{owner}/{repo}/{number}` | a task's issue text, its outcome, its runs, time and tokens by role, and its `steps`: the task's events so far, oldest first, each as recorded without its tokens |
| POST | `/api/v1/tasks/{owner}/{repo}/{number}/to-queen` | stop Kandu's tries on a task in its own error, as if the last had failed: a task never sized goes to `Needs info` as `sizing_failed`, any other to `Blocked` as `failed`, with Kandu's usual comment, and a cycle starts unless Kandu is stopped, so the queen looks at it now; answers `{"task", "to", "cycle_started"}`. `409` for a task in any other state or while a try runs |
| POST | `/api/v1/tasks/{owner}/{repo}/{number}/send-back` | the user sends a task in `Blocked` or `Needs info` back to work; a cycle starts. Its repo is read from GitHub first: `409` when its issue is closed or its PR merged, `503` when GitHub cannot be read |
| GET | `/api/v1/reviews/{owner}/{repo}/{number}` | the PR of a task and each review round |
| POST | `/api/v1/reviews/{owner}/{repo}/{number}/approve` | the user's yes to merge the PR's head, given as `?head=SHA`; a cycle starts |
| GET | `/api/v1/splits/{owner}/{repo}/{number}` | the proposal to split a task that waits for the user, or `null` |
| POST | `/api/v1/splits/{owner}/{repo}/{number}/approve` | the user's yes to that proposal; its slices become issues |
| POST | `/api/v1/drop-blockers/{owner}/{repo}/{number}` | the user's yes to the queen's ask to drop a blocker from a task, given as `?blocker=OWNER/REPO%23N`; the blocker leaves the issue's `## Blocked by` and a cycle starts |
| POST | `/api/v1/drop-blockers` | `enabled=true` or `false`: let the queen ask to drop a blocker, or stop her, in `kandu.yaml`; off is the default |
| POST | `/api/v1/notify` | `events=` a comma list of the kinds to send (`notify.events`), or `none`: written in `kandu.yaml`, and the next cycle uses it; answers `{"notify": {...}}` as in the state |
| POST | `/api/v1/telegram/token` | the bot's token, in the request's form body as `token=`, never in the URL: checked with Telegram's `getMe`, then written in `local/.env` under `notify.telegram.bot_token_env`; answers `{"notify": {...}}` as in the state, which says only that it is set. `400` with Telegram's answer when it is refused |
| POST | `/api/v1/telegram/chat` | the chat Kandu writes to and takes button presses from, in the body as `chat_id=`: a whole number, written in `local/.env` under `notify.telegram.chat_id_env`; answers as above |
| POST | `/api/v1/telegram/chats` | the chats that wrote to the bot since the dashboard started, newest first, as `{"chats": [{"id", "name"}], "bot"}`, so the user picks theirs; changes nothing |
| POST | `/api/v1/telegram/test` | sends Kandu's fixed test message to the chat; answers `{"sent": true}` |
| POST | `/api/v1/telegram/buttons` | `enabled=true` or `false`: put an Approve merge button under the message about a PR that waits for the user's yes (`notify.telegram.buttons` in `kandu.yaml`); off is the default. Each button carries the reviewed head the message is about; a press asks once more, naming it, and the second press approves only that head, never one pushed since the message, as `/reviews/.../approve` does; only presses from the configured chat count |
| POST | `/api/v1/machines` | add a machine the brain reaches by SSH: `?name=laptop&ssh=laptop&always_on=false`, and `&run_tmp_dir=~/kandu-tmp` to give each run there its own run temporary folder; answers `{"machine": "laptop", "reachable": true}` |
| POST | `/api/v1/machines/{name}/run-tmp` | turn a machine's run temporary folder on or off and set its folder in `kandu.yaml`: `?enabled=true&folder=~/kandu-tmp` (`folder` may be left out once one is set; it is absolute or starts with `~/`); answers the machine as in `machines`. The next run there uses it |
| POST | `/api/v1/projects/detect` | read the copy at `?machine=&path=` (and optional `repo=owner/name`) and propose a project entry; changes nothing |
| POST | `/api/v1/projects` | add a project from the fields of a proposal, with `run` (`clone` or `in-place`) chosen by the user; clones the copy and creates the family's board when missing, then runs the doctor's checks for it |
| POST | `/api/v1/clean-merged` | `enabled=true` or `false`: turn the cleanup after a merge on or off, in `kandu.yaml`; on is the default; answers `{"clean_merged": false}` |
| POST | `/api/v1/stop` | stop the supervisor; running agents are killed |
| POST | `/api/v1/resume` | allow the next cycle to run |
| POST | `/api/v1/limits` | set the 5-hour and weekly usage limits, as `?session=85&week=60`; the next cycle uses them |
| POST | `/api/v1/parallel` | set how many workers build at once: in all as `?total=6`, and per project as `&project=website:2`, repeated; either may be left out, and the next cycle uses them |
| POST | `/api/v1/wake` | start a cycle now, as the timer would |
| POST | `/api/v1/restart` | apply the config: write the units again, re-arm the timer, restart the dashboard; a running cycle finishes |
| POST | `/api/v1/workers/{run_id}/stop` | stop one running agent; its task moves to `Blocked` |
| POST | `/api/v1/projects/{name}/pause` | pause one project: no new work on it, across cycles, until it is resumed |
| POST | `/api/v1/projects/{name}/resume` | resume a paused project |
| POST | `/api/v1/backup` | `enabled=true` or `false`, and optionally `at=HH:MM`, `destination=PATH` and `keep=N`: turn the daily backup on or off and set its hour, folder and copies kept, in `kandu.yaml`; off is the default |
| POST | `/api/v1/upstream/check` | fetch the upstream branch and count, now; answers the same object as `upstream` in `/state`. It never merges, pushes or changes a branch or file of the install. `409` while `upstream` is off |
| POST | `/api/v1/upstream/bring-in` | create one task on the board of `upstream.project`, Kandu's own project, to bring the upstream commits into `main` through a PR; answers `task`, `url` and `created`. While that task is open, it creates nothing and answers the open one with `created: false`. `409` while `upstream` is off, or when the last good check found `main` behind by nothing |
| GET | `/api/v1/release` | what the next release would be, from `python -m kandu.release` run with no yes, which changes nothing: `version`, the version now; `next`, the proposed one, or null when nothing changed since the last release; `text`, what the command printed, the changes and the changelog entry; and `proposal`, the fingerprint of that text. `502` with the command's message when it fails |
| POST | `/api/v1/upgrade/read` | `version=X.Y.Z`: «Mark as read» on the upgrade notice. Keeps `version` as read in `state/version-read` and logs `upgrade_read`, so `upgrade` is null for every browser until a newer version is installed; answers `read`. `409` when `version` is not the notice's `read`, the newest version it shows, or there is no notice |
| POST | `/api/v1/release` | `proposal=FINGERPRINT`: open the release PR with `python -m kandu.release --yes`, only when the proposal read again now is the one whose fingerprint came back; answers `version` and `url`. `409` with no change since the last release, or when the proposal changed since it was shown, answering the new one beside the error, and nothing is opened. Nothing is tagged or published |
| POST | `/api/v1/projects/{name}/remove` | take the project out of Kandu: offer its memory and brief as a PR to its own repo (none for a public repo), stop its running work, remove Kandu's worktrees for it, cut only its entry out of `projects.yaml`, then archive its memory and brief, used again if the project is added back. Its copy, repo, board and issues stay. `without_memory_pr=true` goes on without that PR, once the user has decided to; logged as `project_removed` |
| POST | `/api/v1/projects/{name}/settings` | `queen_answers_one_way=true` or `false`: whether the queen's yes to a merge answers its one-way door and the review's questions; `split_approval=user` or `queen`: who accepts this project's split proposals, both kept in Kandu's state, where they win over the registry. `priority`, `daily_token_limit` (whole numbers of 1 or more), `auto_merge` and `frozen` (`true` or `false`) are written in the project's entry in `projects.yaml`, and the next cycle uses them. Several may go in one call; `400` naming the first problem changes nothing. Each change is logged as `project_setting_changed`. `name=NEW` renames the project, in a call of its own, and answers `{"name": NEW}`: Kandu then knows it only by the new name ([DESIGN §1](../DESIGN.md)). A name that is empty, not lowercase letters, digits and dashes, or another project's answers `400`; a supervisor cycle running, or a worker or a free flight running on it, answers `409` `stop or wait for the running work first`; either changes nothing. It is logged as `project_renamed` |
| POST | `/api/v1/pace` | `wake_every_minutes` and `max_cycle_minutes`, whole numbers of 1 or more, written in `kandu.yaml`'s `supervisor` block after the config passes its check on a copy; answers `{"pace": {...}}`. The next cycle uses the cycle length; a new wake interval reaches the timer on `POST /restart`. Logged as `pace_changed` |
| POST | `/api/v1/projects/{name}/inspect` | `enabled=true` or `false`, and optionally `at=HH:MM`: turn Bazras on or off for a project, and set its hour, in `projects.yaml`; off is the default |

## Examples

`GET /api/v1/usage?range=week` answers
```json
{"from": "2026-11-02", "to": "2026-11-08", "today": "2026-11-04", "total": 800,
 "roles": {"worker": 370, "reviewer": 400, "queen": 10, "didban": 20}, "models": {"opus": 380, "sonnet": 400},
 "projects": [{"project": "website", "total": 770, "limit": null, "counted": null,
               "tasks": [{"task": "acme-org/website#12", "total": 770, "outcome": "merged"}]}],
 "kandu": 10, "sessions": 20,
 "tasks": [{"task": "acme-org/website#12", "project": "website", "total": 770, "outcome": "merged"}]}
```
`range` is `today` (the default), `week` (Monday to Sunday), `last7` (the last 7 days, today included) or `month`; `from=2026-10-01&to=2026-10-31` asks for any range instead, both days included, and `400` names a bad one. Tokens are counted as the daily limit counts them: everything but cache reads. Projects and their tasks come from most to fewest tokens, and `tasks` lists every task the same way. `kandu` is what Kandu spent outside any task's work: the queen, the supervisor, Bazras and sizing (every `splitter` run, though it names a task). `sessions` is Didban's sessions and the free flights, each on the day it started, under the role `didban` or `flight`; they name no model, so `models` leaves them out. The projects, `kandu` and `sessions` add up to `total`. `limit` is the project's daily token limit when the range is just today, otherwise null; `counted` is then what counts against it today, as Kandu counts it to stop the project, its sizing included, so it can be more than `total`. `outcome` is the task's last outcome in the range, or null.

`POST /api/v1/projects/detect?machine=laptop&path=~/code/website` answers `{"proposal": {"machine": "laptop", "path": "~/code/website", "repo": "acme-org/website", "name": "website", "family": "website", "run": "", "description": "", "test": "npm test", "setup": "npm ci", "setup_inputs": "package-lock.json", "run_local": "npm run dev", "deploy": "", "live_url": "", "secrets_from": "~/code/website/.env", "repo_rules": "Follow AGENTS.md.", "forbidden": "", "test_task": false, "deploy_task": true, "priority": 1, "max_parallel_workers": 1, "daily_token_limit": 1000000, "auto_merge": false}, "warnings": [], "copy": "present", "board": "a new board"}`. `copy` is `will be cloned` when the path does not exist yet. `run` is always empty: the user chooses it. Send the proposal's fields back, changed or not, to `POST /api/v1/projects`; it answers `{"project": "website", "board": {"owner": "acme-org", "number": 4, "created": true}, "cloned": false, "memory_restored": false, "test_task": null, "deploy_task": "https://github.com/acme-org/website/issues/1", "checks": [{"level": "ok", "text": "..."}]}`, or `400` naming the first problem, before anything is written or created. Both config files get one more entry at the end, so their comments stay.

`GET /api/v1/state`
```json
{"v": 1, "supervisor": "awake", "last_reason": null, "next_wake": null,
 "usage": {"session": 28, "week": 41}, "limits": {"session": 70, "week": 60},
 "workers": [{"task": "acme-org/website#12", "project": "website", "machine": "server",
              "started": "2026-10-30T12:00:00+01:00", "phase": "implement", "run_id": "r-20261030120000-a1b2c3"}],
 "waiting_for_machine": [], "queen_run": null, "stop_requested": false,
 "flowers": {"acme-org/website#12": {"size": "Medium", "door": "two-way", "sent_back": false, "slice": false,
                                     "pr": null, "questions": 0, "yours": false, "waited": []}}}
```
`backup` holds the daily backup's `enabled`, `at`, `destination` and `keep`, its `copies` by file name, newest first, and `last`, its newest `backup_made` or `backup_failed` event, or null. `POST /api/v1/backup` answers the same object after the change; turning it on also starts `kandu-backup.timer`, and the answer is `500` if `systemd` could not, though the config is saved. `version` is Kandu's version as the install's `VERSION` file holds it, such as `"1.0.0"`, or null when the file is missing. `project_order` lists every project by its `priority`, 1 first. `project_settings` is the settings section: one row per project in the registry's order, with `name`, `paused`, `paused_in_config`, `max_parallel_workers`, `split_approval`, `user` or `queen`, and `queen_answers_one_way`, true or false, each the dashboard's choice if it made one, else the registry's, and the registry's `priority`, `daily_token_limit`, `auto_merge` (the supervisor's default when the project sets none) and `frozen`. `pace` holds the supervisor's `wake_every_minutes` and `max_cycle_minutes`. `over_limit` maps each project whose tokens today reached its daily limit to `{"today": tokens, "limit": its limit}`, counted as the supervisor counts them to start nothing new there; a new day counts from zero, so a project leaves it by itself. `timer_on` says whether `systemd` wakes the supervisor by itself; while it is false, `next_wake` will not happen. `queen.questions` leaves out a task that the user or a cycle moved on since she asked. `flowers` holds what the page draws on a task beyond its column, read from the local log, so it costs GitHub nothing. `pr` is `"waits"` when the merge waits for the user, `"held"` when the merge gate's latest reasons are all tasks it is blocked by (`blocker OWNER/REPO#N is not merged yet`), which no yes would answer, `"approved"` after the user's yes, otherwise null. `waited` lists, in English, the reasons of the latest `merge_waits` decision; a pick, a yes or an end clears it. `yours` is true while the task waits for the user's decision: a cycle left it to the user, the loop guard stopped it, or the queen's last look asked about it. Anything the user or a cycle does to the task clears it. A task that ended is left out. Each `flowers` entry also has `status`, read from the local log: `state` is `fine`, `problem` (a review found blockers, or the merge is held by something a fix answers), `error` (Kandu's own error, which it tries again by itself; `tries` then says which try failed, `this`, of how many, `of`) or `stuck`: Kandu will not move the task on by itself, and only the user, the queen or a new head does (a round that pushed nothing, `round_no_commit`; a merge held after a yes or twice in a row; a task that stopped as `blocked`, `needs_info`, `sizing_failed`, `failed`, `deploy_failed` or `live_check_failed`; a loop stop). `why` names the reason the page puts in a sentence, or is null; `detail` is the log's own reason, or null. `cleaned` says what Kandu cleaned up in the task's folder before its last run, or is null. `wait` says what the task waits on from the user, or is null when it waits on nobody: `on` is `yes` (the user's yes), `check` (a failing check, each in `checks` with its `name` as on the PR and its details `url` or null), `blocker` (the `blockers` not merged yet), `decision` (the user's decision; `detail` lists the merge's problems, if any) or `unknown` (Kandu does not know why it waits). `held` is true when something holds the merge whatever the user says, so the user's yes counts only once it is cleared. Notifications say the same, in Kandu's language. The meadow draws a stuck task on its way as `Stuck`, unless an agent runs on it. `waits_for_you` counts the tasks that wait for the user, each once: those whose `flowers` mark is `yours`, and those `yours` in the last `GET /queue` read, so it costs GitHub nothing and knows no by-hand task before the first read. `bazras.running` is the project Bazras is looking at now (`project`, `run_id`, `machine`, `started`, and `judging` while an agent judges), or null; `bazras.last` maps each project it looks at to its newest report, as in `GET /bazras`, or null before the first.

`GET /api/v1/queue`
```json
{"projects": {"website": {"Ready": [{"task": "acme-org/website#13", "title": "Add search",
  "url": "https://github.com/acme-org/website/issues/13", "by_hand": false, "yours": false, "blockers": []}], "In progress": [],
  "In review": [{"task": "acme-org/website#14", "title": "Search page", "url": "https://github.com/acme-org/website/issues/14",
  "by_hand": false, "yours": false, "blockers": [{"task": "acme-org/website#13", "column": "Ready", "slices": []}]}],
  "Needs info": [], "Blocked": [], "Technical debt": []}}, "stale": false}
```
Each task in `Done` has `closed_at`, when its issue closed, mostly by its PR's merge. `by_hand` is true for a task without the `agent` label. `yours` is true for a by-hand task in an open column: not `Done` and not closed, so it waits for the user. A by-hand task also has `goal` and `criteria`: the text of its issue's `## Goal` and `## Acceptance criteria`, from the copy, or `""` when the issue has no such section. A task in `Ready`, `In progress` or `In review` has `blockers`: each open task in its `## Blocked by`, with its `column` on the board, or null. A split one also lists its `slices`, each with its column, and stays open until its last slice closes, as at the merge gate. All of it comes from the copy and the local log.
`?fresh=1` reads the board now, at most once a minute, and a task that finished or moved does the same. If GitHub fails, the answer is the last good one with `"stale": true`, and the next read waits 5 minutes; with no good one it is a `502`.

`GET /api/v1/boards`
```json
{"boards": [{"owner": "acme-org", "number": 1, "projects": ["website"], "project_id": "PVT_1",
  "columns": ["Backlog", "Ready", "In progress"],
  "fields": {"Status": {"id": "F_1", "options": {"Backlog": "o_1", "To do": "o_2", "In progress": "o_3"}}},
  "cards": [{"task": "acme-org/website#13", "title": "Add search", "url": "https://github.com/acme-org/website/issues/13",
             "status": "Ready", "labels": ["agent"], "priority": "P1", "kind": "Feature", "size": "Small",
             "deadline": "", "closed_at": "", "item_id": "PVTI_1"}]}]}
```
`columns` and a card's `status` use Kandu's column names; `fields` holds each field with its id and its options' ids as the board names them, which a change to a card on GitHub needs, together with `project_id` and the card's `item_id`. A board the copy does not hold yet is read from GitHub once. If GitHub fails, the answer is a `502`.

`GET /api/v1/days/2026-10-30`
```json
{"day": "2026-10-30", "total": {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0, "total": 15},
 "tasks": {"acme-org/website#12": {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0, "total": 15}},
 "no_task": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0},
 "models": {}, "roles": {},
 "finished": [{"ts": "2026-10-30T13:00:00+01:00", "task": "acme-org/website#12", "project": "website", "outcome": "merged"}]}
```

`GET /api/v1/events?day=2026-10-30&after=0`
```json
{"day": "2026-10-30", "events": [{"v": 1, "ts": "2026-10-30T12:00:00+01:00", "type": "supervisor_woke"}], "next": 1}
```

`GET /api/v1/stream` sends one line for each event in [STATE.md](STATE.md), and a comment line to keep the connection alive:
```
data: {"v": 1, "ts": "2026-10-30T12:00:00+01:00", "type": "supervisor_woke"}
```

`GET /api/v1/runs/r-20261030120000-a1b2c3`
```json
{"run_id": "r-20261030120000-a1b2c3", "actions": [{"tool": "Read", "detail": "src/a.py"}, {"tool": "Bash", "detail": "npm test"}]}
```

`GET /api/v1/reviews/acme-org/website/12`
```json
{"task": "acme-org/website#12",
 "pr": {"number": 7, "title": "Add search", "url": "https://github.com/acme-org/website/pull/7", "state": "OPEN", "head_sha": "abc123"},
 "rounds": [{"round": 1, "head_sha": "abc123", "door": "two-way", "summary": "...", "brief": "...", "user_brief": "...", "blast_radius": "...",
             "criteria": [{"criterion": "...", "result": "met"}], "open": [], "recommendation": "ready to merge"}]}
```
The `pr` object also has `approved`, true once the user said yes to this head, `can_approve`, true when the page may offer that, and `why_not`, which says in English why not, otherwise null. While a task it is blocked by is open, `why_not` is `it waits for OWNER/REPO#N to merge first`. Beside `pr`, `blockers` is the task's open blockers as in `GET /queue`, `waited` the latest reasons its merge waited, as in `flowers`, and `holds` what still holds the merge after a yes: each open blocker as the merge gate words it, then `waited`. The page promises a merge in the next cycle only when `holds` is empty. `kept` lists what stayed after the task's merge: `[{"what": "folder", "target": "12-add-search", "reason": "it has changes that are not committed"}]`, empty when nothing did.

`POST /api/v1/reviews/acme-org/website/12/approve?head=abc123` answers `{"approved": "abc123", "cycle_started": true}`. `cycle_started` is `null` if a cycle is already running, and `false` if `systemd` refused. Either way the yes is saved, and the next cycle uses it. The answer is `409` if `head` is no longer the PR's head, if the local log shows no review of it, an open blocker or an unmet criterion, if a task it is blocked by is still open, or if no cycle would pick the task up: its project is paused, or its column is not `Ready`, `In progress` or `In review`. The yes answers the review's questions and a one-way door. Every other merge condition still applies, including the tests.

`queen` in `GET /state` is her newest look, or null: its `brief`, `questions` (her `ask_user` questions from that look), `tokens`, `today`, `limit`, and `drops`, her asks to drop a blocker that wait for the user's yes, each with `task`, `project`, `blocker`, `reason` and `ts`. An ask leaves `drops` once the user says yes, once the issue no longer names its blocker, or once its task ends. `drop_blockers` is true when she may ask.

`POST /api/v1/drop-blockers/acme-org/website/12?blocker=acme-org/website%235` answers `{"task": "acme-org/website#12", "dropped": "acme-org/website#5", "cycle_started": true}`. Only that blocker leaves the issue; its other blockers stay, and the yes is saved as `blocker_dropped`. The next cycle checks the task's merge again. The answer is `409` if the queen has not asked to drop that blocker from that task, or if the issue no longer names it, and nothing changes. `POST /api/v1/drop-blockers?enabled=true` answers `{"drop_blockers": true}`.

`GET /api/v1/requests` answers `{"show_requests": true, "projects": [{"project": "website", "requests": [{"id": "3f9a2c", "text": "Fix the login", "map": {"decided": ["By email"], "asked": ["Welcome mail?"], "later": [], "out": ["Password reset"]}}]}, {"project": null, "requests": [{"id": "b71e04", "text": "Add dark mode", "map": null}]}]}`. The page shows each map read-only; a map changes only in Didban. `POST /api/v1/requests?enabled=true` answers `{"show_requests": true}`.

`POST /api/v1/stop` answers `{"stop_requested": true}`; it kills no run: each one at work finishes its step, and nothing new starts until the resume. `POST /api/v1/resume` answers `{"stop_requested": false}`, and `POST /api/v1/workers/{run_id}/stop` answers `{"stopping": "<run_id>"}`.

`POST /api/v1/wake` answers `{"cycle_started": true}` and starts a cycle at once, as the timer would; the timer keeps its own times. It answers `409` while Kandu is stopped or a cycle is already running, and starts nothing.

`POST /api/v1/restart` answers `{"restarting": true}`: it writes the `systemd` units again from the config, re-arms the timer if it runs, so a new `wake_every_minutes` counts, and restarts the dashboard a second later. A running cycle finishes. The answer is `409` if the config has a problem, and then nothing restarts. The dashboard also takes a changed `kandu.yaml` or `projects.yaml` by itself at the next request, so a new project shows without a restart.

`POST /api/v1/tasks/acme-org/website/12/send-back` answers `{"task": "acme-org/website#12", "to": "Ready", "cycle_started": true}`. The task goes to `Ready`, or to `Backlog` if it could not be sized, past the queen's limits and the loop guard: the user's call is the last one. With an open PR the next cycle reviews its head, or fixes what the last review found. It merges nothing. The answer is `409` if the task is not in `Blocked` or `Needs info`, has no `agent` label, its project is paused or its PR is merged.

`GET /api/v1/splits/acme-org/website/12` answers `{"task": "acme-org/website#12", "split": {"title": "...", "reason": "...", "fingerprint": "ab12cd34ef56", "slices": [{"title": "...", "size": "Medium", "files": 3, "door": "two-way", "body": "..."}], "user_text": {"reason": "...", "slices": [{"title": "...", "body": "..."}]}}}`. `user_text` is the proposal in the config's `language`, or `null`; only the English `slices` become issues. `POST .../approve?proposal=ab12cd34ef56` must name that fingerprint, and answers `{"children": ["acme-org/website#13", "acme-org/website#14"]}`, or `409` if `proposal` no longer names the proposal, the task is not waiting for a split or a slice fails its checks. It starts no cycle. Declining or changing a proposal happens in Didban.

`POST /api/v1/projects/website/remove` answers `{"removed": "website", "worktrees": ["/home/me/code/website.kandu-worktrees/12-fix-footer"], "memory": {"pr": "https://github.com/acme-org/website/pull/40", "why": "offered", "note": "..."}, "archived": ["local/memory/archive/website.md"]}`. `memory.why` is `offered`, `public` (no PR to a public repo), `declined` (`without_memory_pr=true`) or `none` (Kandu had no memory of it). It answers `404` for no such project, and `409` naming the problem when its work cannot be stopped, its machine cannot be reached or the config would not pass its check without it; then nothing is removed and the project is not left paused. When the PR cannot be made, the `409` also has `"memory_pr_failed": true`, and nothing is removed until the user tries again or sends `without_memory_pr=true`. A project added back gets its archived memory and brief again, with anything written meanwhile after them, and the add's answer has `"memory_restored": true`.

`POST /api/v1/projects/{name}/pause` answers `{"paused": true}` and `resume` answers `{"paused": false}`. A step already running finishes, and nothing after it starts. Resume answers `409` if the project is paused by `paused: true` in its config, which only a config change lifts.

`POST /api/v1/projects/website/settings?split_approval=queen` answers `{"split_approval": "queen"}`. The answer is `400` for no setting, an unknown one or a value it may not take, and nothing changes. A change reaches only proposals made after it: one already accepted and a split already made stay as they are. While it is `user`, every proposal waits in `Needs split` for the user's yes, also when every slice is two-way; while it is `queen`, a proposal still waits for the user's yes until the queen can accept one.
`POST /api/v1/projects/website/settings?queen_answers_one_way=false` answers `{"queen_answers_one_way": false}`; only `true` and `false` are values. While it is false, a merge with a one-way door or a question for the user waits for the user's yes, and the queen's `approve` of such a head is refused.

`upstream` is null while `upstream.enabled` is off in `kandu.yaml`. On, it holds `remote`, `branch` and `every_hours`; `behind` and `ahead`, the commits `main` lacks of `remote/branch` and its own, from the last good check, or null before one and while the last check failed; `failed`, true when the last check could not fetch or count; `checked_at`, when the last check ran; `last_good_at`, when the last good one ran, or null; and `task`, the open task `bring-in` made, as `task` and `url`, or null. The dashboard checks by itself every `every_hours`. It is read from the local log and Kandu's copy, so `/state` costs neither git nor GitHub.

`version_badge` is the header's version badge. `installed` is the install's version, from `VERSION` at the repo's root, or null without one; `upstream`, the public repo as `owner/repo`, or null while `upstream.enabled` is off or `upstream.remote` is not on GitHub; `latest` and `url`, the tag and page of its newest release from the last read, or null without an upstream and while the last read failed; `up_to_date`, true when `installed` is that release or newer, false when it is older, null when `latest` is not known; `tried_at`, when the last read ran, or null; and `failed`, true when the last read could not read a release. The dashboard reads the release once a day by itself and keeps it in the log, so `/state` never asks GitHub for it. Nothing updates the install.

`upgrade` is the upgrade notice, or null when there is nothing to show: no version newer than the one marked as read (only the install's own version before the first mark), no `CHANGELOG.md`, or no entry in it for any unread version. Otherwise `version` is the install's version; `entries`, each unread version that has an entry, newest first, with its `version`, its `text` exactly as the changelog writes it, from its `## X.Y.Z` line to the next entry, and `url`, its release on the public upstream, or null while it is not known to be published; and `read`, the newest version shown, which `POST /api/v1/upgrade/read` takes.
