# HTTP API

Version 1. Kandu's own dashboard uses only this API, so anything it shows is open to any other system. A change that breaks a response moves to `/api/v2`.

Start it with `.venv/bin/python -m kandu.dashboard`. It binds to `dashboard.bind` and `dashboard.port` from `local/kandu.yaml`. All responses are JSON, except `/stream`. An error is `{"error": "message"}`.

Reads are open on the bound address. Every `POST` needs `Authorization: Bearer <token>`, where the token is `KANDU_DASHBOARD_TOKEN` in `local/.env`. With no token set, controls are refused.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/api/v1/state` | `current.json` (see [STATE.md](STATE.md)), plus `stop_requested`, `paused_projects`, `paused_in_config`, `project_order`, `timer_on`, `language`, `limits`, `queen` and `flowers` |
| GET | `/api/v1/queue` | each project's board columns, with `Done` holding only what closed in the last `done_days` days, read from GitHub at most every 15 minutes, however many pages ask, and sooner once a task finishes or moves |
| GET | `/api/v1/days` | dates that have events |
| GET | `/api/v1/days/{day}` | one day's tokens by task, model and role, and its finished tasks |
| GET | `/api/v1/didban` | the last Didban sessions: when, title, tokens and what each did; counted in no limit |
| GET | `/api/v1/flights` | the sessions opened from this page, each with its status (`opening`, `open`, `failed`) and link, and the last free flights that ended, with tokens; counted in no limit |
| POST | `/api/v1/flights/didban` | open Didban on the brain, or keep the one already open; `?language=en` or `fa` is the language it speaks |
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
| POST | `/api/v1/stop` | stop the supervisor; running agents are killed |
| POST | `/api/v1/resume` | allow the next cycle to run |
| POST | `/api/v1/limits` | set the 5-hour and weekly usage limits, as `?session=85&week=60`; the next cycle uses them |
| POST | `/api/v1/parallel` | set how many workers build at once: in all as `?total=6`, and per project as `&project=website:2`, repeated; either may be left out, and the next cycle uses them |
| POST | `/api/v1/wake` | start a cycle now, as the timer would |
| POST | `/api/v1/restart` | apply the config: write the units again, re-arm the timer, restart the dashboard; a running cycle finishes |
| POST | `/api/v1/workers/{run_id}/stop` | stop one running agent; its task moves to `Blocked` |
| POST | `/api/v1/projects/{name}/pause` | pause one project: no new work on it, across cycles, until it is resumed |
| POST | `/api/v1/projects/{name}/resume` | resume a paused project |

## Examples

`GET /api/v1/state`
```json
{"v": 1, "supervisor": "awake", "last_reason": null, "next_wake": null,
 "usage": {"session": 28, "week": 41}, "limits": {"session": 70, "week": 60},
 "workers": [{"task": "acme-org/website#12", "project": "website", "machine": "server",
              "started": "2026-10-30T12:00:00+01:00", "phase": "implement", "run_id": "r-20261030120000-a1b2c3"}],
 "waiting_for_machine": [], "queen_run": null, "stop_requested": false,
 "flowers": {"acme-org/website#12": {"size": "Medium", "door": "two-way", "sent_back": false, "slice": false,
                                     "pr": null, "questions": 0, "yours": false}}}
```
`project_order` lists every project by its `priority`, 1 first. `timer_on` says whether `systemd` wakes the supervisor by itself; while it is false, `next_wake` will not happen. `queen.questions` leaves out a task that the user or a cycle moved on since she asked. `flowers` holds what the page draws on a task beyond its column, read from the local log, so it costs GitHub nothing. `pr` is `"waits"` when the merge waits for the user, `"approved"` after the user's yes, otherwise null. `yours` is true while the task waits for the user's decision: a cycle left it to the user, the loop guard stopped it, or the queen's last look asked about it. Anything the user or a cycle does to the task clears it. A task that ended is left out.

`GET /api/v1/queue`
```json
{"projects": {"website": {"Ready": [{"task": "acme-org/website#13", "title": "Add search",
  "url": "https://github.com/acme-org/website/issues/13"}], "In progress": [], "In review": [],
  "Needs info": [], "Blocked": [], "Technical debt": []}}, "stale": false}
```
`?fresh=1` reads the board now, at most once a minute, and a task that finished or moved does the same. If GitHub fails, the answer is the last good one with `"stale": true`, and the next read waits 5 minutes; with no good one it is a `502`.

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
The `pr` object also has `approved`, true once the user said yes to this head, `can_approve`, true when the page may offer that, and `why_not`, which says in English why not, otherwise null.

`POST /api/v1/reviews/acme-org/website/12/approve?head=abc123` answers `{"approved": "abc123", "cycle_started": true}`. `cycle_started` is `null` if a cycle is already running, and `false` if `systemd` refused. Either way the yes is saved, and the next cycle uses it. The answer is `409` if `head` is no longer the PR's head, if the local log shows no review of it, an open blocker or an unmet criterion, or if no cycle would pick the task up: its project is paused, or its column is not `Ready`, `In progress` or `In review`. The yes answers the review's questions and a one-way door. Every other merge condition still applies, including the tests.

`POST /api/v1/stop` answers `{"stop_requested": true}`, `POST /api/v1/resume` answers `{"stop_requested": false}`, and `POST /api/v1/workers/{run_id}/stop` answers `{"stopping": "<run_id>"}`.

`POST /api/v1/wake` answers `{"cycle_started": true}` and starts a cycle at once, as the timer would; the timer keeps its own times. It answers `409` while Kandu is stopped or a cycle is already running, and starts nothing.

`POST /api/v1/restart` answers `{"restarting": true}`: it writes the `systemd` units again from the config, re-arms the timer if it runs, so a new `wake_every_minutes` counts, and restarts the dashboard a second later. A running cycle finishes. The answer is `409` if the config has a problem, and then nothing restarts. The dashboard also takes a changed `kandu.yaml` or `projects.yaml` by itself at the next request, so a new project shows without a restart.

`POST /api/v1/tasks/acme-org/website/12/send-back` answers `{"task": "acme-org/website#12", "to": "Ready", "cycle_started": true}`. The task goes to `Ready`, or to `Backlog` if it could not be sized, past the queen's limits and the loop guard: the user's call is the last one. With an open PR the next cycle reviews its head, or fixes what the last review found. It merges nothing. The answer is `409` if the task is not in `Blocked` or `Needs info`, has no `agent` label, its project is paused or its PR is merged.

`GET /api/v1/splits/acme-org/website/12` answers `{"task": "acme-org/website#12", "split": {"title": "...", "reason": "...", "fingerprint": "ab12cd34ef56", "slices": [{"title": "...", "size": "Medium", "files": 3, "door": "two-way", "body": "..."}], "user_text": {"reason": "...", "slices": [{"title": "...", "body": "..."}]}}}`. `user_text` is the proposal in the config's `language`, or `null`; only the English `slices` become issues. `POST .../approve?proposal=ab12cd34ef56` must name that fingerprint, and answers `{"children": ["acme-org/website#13", "acme-org/website#14"]}`, or `409` if `proposal` no longer names the proposal, the task is not waiting for a split or a slice fails its checks. It starts no cycle. Declining or changing a proposal happens in Didban.

`POST /api/v1/projects/{name}/pause` answers `{"paused": true}` and `resume` answers `{"paused": false}`. A step already running finishes, and nothing after it starts. Resume answers `409` if the project is paused by `paused: true` in its config, which only a config change lifts.
