# HTTP API

Version 1. Kandu's own dashboard uses only this API, so anything it shows is open to any other system. A change that breaks a response moves to `/api/v2`.

Start it with `.venv/bin/python -m kandu.dashboard`. It binds to `dashboard.bind` and `dashboard.port` from `local/kandu.yaml`. All responses are JSON, except `/stream`. An error is `{"error": "message"}`.

Reads are open on the bound address. Every `POST` needs `Authorization: Bearer <token>`, where the token is `KANDU_DASHBOARD_TOKEN` in `local/.env`. With no token set, controls are refused.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/api/v1/state` | `current.json` (see [STATE.md](STATE.md)), plus `stop_requested`, `paused_projects`, `paused_in_config`, `project_order`, `project_settings`, `timer_on`, `language`, `limits`, `queen`, `flowers`, `waits_for_you`, `bazras`, `backup`, `drop_blockers`, `show_requests` and `clean_merged` |
| GET | `/api/v1/queue` | each project's board columns, with `Done` holding only what closed in the last `done_days` days, read from GitHub at most every 15 minutes, however many pages ask, and sooner once a task finishes or moves |
| GET | `/api/v1/boards` | every board in the config, with its projects, columns, fields and cards, read from Kandu's copy of GitHub, so it costs GitHub nothing; `?fresh=1` reads each board into the copy first, at most once a minute |
| GET | `/api/v1/days` | dates that have events |
| GET | `/api/v1/days/{day}` | one day's tokens by task, model and role, and its finished tasks |
| GET | `/api/v1/bazras` | every project in priority order: whether Bazras looks at it (`enabled`), its hour (`at`), `paused_in_config`, and its newest reports, newest first, each with `date`, `all_well`, `findings` (a count) and `text`; `default_at` is `bazras.at` |
| GET | `/api/v1/didban` | the last Didban sessions: when, title, tokens and what each did; counted in no limit |
| GET | `/api/v1/requests` | the open requests by project, in priority order, then those whose project is not named yet (`project` null): each with `id`, `text` and its decision `map` (`decided`, `asked`, `later`, `out`), or null when it has none; read from local state only, so it costs GitHub nothing. While `show_requests` is off: `{"show_requests": false, "projects": []}` |
| POST | `/api/v1/requests` | `enabled=true` or `false`: show the open requests on the dashboard, or hide them, in `kandu.yaml`; off is the default |
| GET | `/api/v1/flights` | the sessions opened from this page, each with its status (`opening`, `open`, `failed`) and link, and the last free flights that ended, with tokens; counted in no limit |
| POST | `/api/v1/flights/didban` | open Didban on the brain, or keep the one already open; `?language=en` or `fa` is the language it speaks; `&project=NAME` opens it on that project, for Bazras's reports |
| POST | `/api/v1/flights/zanbourdar` | open Zanbourdar on the brain: a session with full access tied to no task, or keep the one already open; `?language=` as above |
| POST | `/api/v1/flights/tasks/{owner}/{repo}/{number}` | open a free flight on a task, with full access, or keep the one already open; `?language=` as above |
| POST | `/api/v1/flights/{key}/close` | end one of those sessions |
| GET | `/api/v1/events` | a page of events; `day` (default: the last) and `after` (default: 0) |
| GET | `/api/v1/stream` | new events as they happen, as Server-Sent Events |
| GET | `/api/v1/runs/{run_id}` | what one agent run did: files read, commands, edits |
| GET | `/api/v1/tasks/{owner}/{repo}/{number}` | a task's issue text, its outcome, and its runs, time and tokens by role |
| POST | `/api/v1/tasks/{owner}/{repo}/{number}/send-back` | the user sends a task in `Blocked` or `Needs info` back to work; a cycle starts |
| GET | `/api/v1/reviews/{owner}/{repo}/{number}` | the PR of a task and each review round |
| POST | `/api/v1/reviews/{owner}/{repo}/{number}/approve` | the user's yes to merge the PR's head, given as `?head=SHA`; a cycle starts |
| GET | `/api/v1/splits/{owner}/{repo}/{number}` | the proposal to split a task that waits for the user, or `null` |
| POST | `/api/v1/splits/{owner}/{repo}/{number}/approve` | the user's yes to that proposal; its slices become issues |
| POST | `/api/v1/drop-blockers/{owner}/{repo}/{number}` | the user's yes to the queen's ask to drop a blocker from a task, given as `?blocker=OWNER/REPO%23N`; the blocker leaves the issue's `## Blocked by` and a cycle starts |
| POST | `/api/v1/drop-blockers` | `enabled=true` or `false`: let the queen ask to drop a blocker, or stop her, in `kandu.yaml`; off is the default |
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
| POST | `/api/v1/projects/{name}/settings` | `queen_answers_one_way=true` or `false`: whether the queen's yes to a merge answers its one-way door and the review's questions; `split_approval=user` or `queen`: who accepts this project's split proposals, kept in Kandu's state, where it wins over the registry; logged as `project_setting_changed` |
| POST | `/api/v1/projects/{name}/inspect` | `enabled=true` or `false`, and optionally `at=HH:MM`: turn Bazras on or off for a project, and set its hour, in `projects.yaml`; off is the default |

## Examples

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
`backup` holds the daily backup's `enabled`, `at`, `destination` and `keep`, its `copies` by file name, newest first, and `last`, its newest `backup_made` or `backup_failed` event, or null. `POST /api/v1/backup` answers the same object after the change; turning it on also starts `kandu-backup.timer`, and the answer is `500` if `systemd` could not, though the config is saved. `project_order` lists every project by its `priority`, 1 first. `project_settings` is the settings section: one row per project in the registry's order, with `name`, `paused`, `paused_in_config`, `split_approval`, `user` or `queen`, and `queen_answers_one_way`, true or false, each the dashboard's choice if it made one, else the registry's. `timer_on` says whether `systemd` wakes the supervisor by itself; while it is false, `next_wake` will not happen. `queen.questions` leaves out a task that the user or a cycle moved on since she asked. `flowers` holds what the page draws on a task beyond its column, read from the local log, so it costs GitHub nothing. `pr` is `"waits"` when the merge waits for the user, `"held"` when the merge gate's latest reasons are all tasks it is blocked by (`blocker OWNER/REPO#N is not merged yet`), which no yes would answer, `"approved"` after the user's yes, otherwise null. `waited` lists, in English, the reasons of the latest `merge_waits` decision; a pick, a yes or an end clears it. `yours` is true while the task waits for the user's decision: a cycle left it to the user, the loop guard stopped it, or the queen's last look asked about it. Anything the user or a cycle does to the task clears it. A task that ended is left out. `waits_for_you` counts the tasks that wait for the user, each once: those whose `flowers` mark is `yours`, and those `yours` in the last `GET /queue` read, so it costs GitHub nothing and knows no by-hand task before the first read. `bazras.running` is the project Bazras is looking at now (`project`, `run_id`, `machine`, `started`, and `judging` while an agent judges), or null; `bazras.last` maps each project it looks at to its newest report, as in `GET /bazras`, or null before the first.

`GET /api/v1/queue`
```json
{"projects": {"website": {"Ready": [{"task": "acme-org/website#13", "title": "Add search",
  "url": "https://github.com/acme-org/website/issues/13", "by_hand": false, "yours": false, "blockers": []}], "In progress": [],
  "In review": [{"task": "acme-org/website#14", "title": "Search page", "url": "https://github.com/acme-org/website/issues/14",
  "by_hand": false, "yours": false, "blockers": [{"task": "acme-org/website#13", "column": "Ready", "slices": []}]}],
  "Needs info": [], "Blocked": [], "Technical debt": []}}, "stale": false}
```
`by_hand` is true for a task without the `agent` label. `yours` is true for a by-hand task in an open column: not `Done` and not closed, so it waits for the user. A by-hand task also has `goal` and `criteria`: the text of its issue's `## Goal` and `## Acceptance criteria`, from the copy, or `""` when the issue has no such section. A task in `Ready`, `In progress` or `In review` has `blockers`: each open task in its `## Blocked by`, with its `column` on the board, or null. A split one also lists its `slices`, each with its column, and stays open until its last slice closes, as at the merge gate. All of it comes from the copy and the local log.
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

`POST /api/v1/stop` answers `{"stop_requested": true}`, `POST /api/v1/resume` answers `{"stop_requested": false}`, and `POST /api/v1/workers/{run_id}/stop` answers `{"stopping": "<run_id>"}`.

`POST /api/v1/wake` answers `{"cycle_started": true}` and starts a cycle at once, as the timer would; the timer keeps its own times. It answers `409` while Kandu is stopped or a cycle is already running, and starts nothing.

`POST /api/v1/restart` answers `{"restarting": true}`: it writes the `systemd` units again from the config, re-arms the timer if it runs, so a new `wake_every_minutes` counts, and restarts the dashboard a second later. A running cycle finishes. The answer is `409` if the config has a problem, and then nothing restarts. The dashboard also takes a changed `kandu.yaml` or `projects.yaml` by itself at the next request, so a new project shows without a restart.

`POST /api/v1/tasks/acme-org/website/12/send-back` answers `{"task": "acme-org/website#12", "to": "Ready", "cycle_started": true}`. The task goes to `Ready`, or to `Backlog` if it could not be sized, past the queen's limits and the loop guard: the user's call is the last one. With an open PR the next cycle reviews its head, or fixes what the last review found. It merges nothing. The answer is `409` if the task is not in `Blocked` or `Needs info`, has no `agent` label, its project is paused or its PR is merged.

`GET /api/v1/splits/acme-org/website/12` answers `{"task": "acme-org/website#12", "split": {"title": "...", "reason": "...", "fingerprint": "ab12cd34ef56", "slices": [{"title": "...", "size": "Medium", "files": 3, "door": "two-way", "body": "..."}], "user_text": {"reason": "...", "slices": [{"title": "...", "body": "..."}]}}}`. `user_text` is the proposal in the config's `language`, or `null`; only the English `slices` become issues. `POST .../approve?proposal=ab12cd34ef56` must name that fingerprint, and answers `{"children": ["acme-org/website#13", "acme-org/website#14"]}`, or `409` if `proposal` no longer names the proposal, the task is not waiting for a split or a slice fails its checks. It starts no cycle. Declining or changing a proposal happens in Didban.

`POST /api/v1/projects/{name}/pause` answers `{"paused": true}` and `resume` answers `{"paused": false}`. A step already running finishes, and nothing after it starts. Resume answers `409` if the project is paused by `paused: true` in its config, which only a config change lifts.

`POST /api/v1/projects/website/settings?split_approval=queen` answers `{"split_approval": "queen"}`. The answer is `400` for no setting, an unknown one or a value it may not take, and nothing changes. A change reaches only proposals made after it: one already accepted and a split already made stay as they are. While it is `user`, every proposal waits in `Needs split` for the user's yes, also when every slice is two-way; while it is `queen`, a proposal still waits for the user's yes until the queen can accept one.
`POST /api/v1/projects/website/settings?queen_answers_one_way=false` answers `{"queen_answers_one_way": false}`; only `true` and `false` are values. While it is false, a merge with a one-way door or a question for the user waits for the user's yes, and the queen's `approve` of such a head is refused.
