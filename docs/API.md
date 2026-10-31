# HTTP API

Version 1. Kandu's own dashboard uses only this API, so anything it shows is open to any other system. A change that breaks a response moves to `/api/v2`.

Start it with `.venv/bin/python -m kandu.dashboard`. It binds to `dashboard.bind` and `dashboard.port` from `local/kandu.yaml`. All responses are JSON, except `/stream`. An error is `{"error": "message"}`.

Reads are open on the bound address. Every `POST` needs `Authorization: Bearer <token>`, where the token is `KANDU_DASHBOARD_TOKEN` in `local/.env`. With no token set, controls are refused.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/api/v1/state` | `current.json` (see [STATE.md](STATE.md)), plus `stop_requested`, `paused_projects`, `paused_in_config`, `timer_on`, `language`, `limits`, `queen` and `flowers` |
| GET | `/api/v1/queue` | each project's board columns, read from GitHub at most every 15 minutes, however many pages ask, and sooner once a task finishes |
| GET | `/api/v1/days` | dates that have events |
| GET | `/api/v1/days/{day}` | one day's tokens by task, model and role, and its finished tasks |
| GET | `/api/v1/events` | a page of events; `day` (default: the last) and `after` (default: 0) |
| GET | `/api/v1/stream` | new events as they happen, as Server-Sent Events |
| GET | `/api/v1/runs/{run_id}` | what one agent run did: files read, commands, edits |
| GET | `/api/v1/reviews/{owner}/{repo}/{number}` | the PR of a task and each review round |
| POST | `/api/v1/reviews/{owner}/{repo}/{number}/approve` | the user's yes to merge the PR's head, given as `?head=SHA`; a cycle starts |
| GET | `/api/v1/splits/{owner}/{repo}/{number}` | the proposal to split a task that waits for the user, or `null` |
| POST | `/api/v1/splits/{owner}/{repo}/{number}/approve` | the user's yes to that proposal; its slices become issues |
| POST | `/api/v1/stop` | stop the supervisor; running agents are killed |
| POST | `/api/v1/resume` | allow the next cycle to run |
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
                                     "pr": null, "questions": 0}}}
```
`timer_on` says whether `systemd` wakes the supervisor by itself; while it is false, `next_wake` will not happen. `queen.questions` leaves out a task that the user or a cycle moved on since she asked. `flowers` holds what the page draws on a task beyond its column, read from the local log, so it costs GitHub nothing. `pr` is `"waits"` when the merge waits for the user, `"approved"` after the user's yes, otherwise null. A task that ended is left out.

`GET /api/v1/queue`
```json
{"projects": {"website": {"Ready": [{"task": "acme-org/website#13", "title": "Add search",
  "url": "https://github.com/acme-org/website/issues/13"}], "In progress": [], "In review": [],
  "Needs info": [], "Blocked": []}}, "stale": false}
```
`?fresh=1` reads the board now, at most once a minute, and a task that finished does the same. If GitHub fails, the answer is the last good one with `"stale": true`, and the next read waits 5 minutes; with no good one it is a `502`.

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

`GET /api/v1/splits/acme-org/website/12` answers `{"task": "acme-org/website#12", "split": {"title": "...", "reason": "...", "fingerprint": "ab12cd34ef56", "slices": [{"title": "...", "size": "Medium", "files": 3, "door": "two-way", "body": "..."}], "user_text": {"reason": "...", "slices": [{"title": "...", "body": "..."}]}}}`. `user_text` is the proposal in the config's `language`, or `null`; only the English `slices` become issues. `POST .../approve?proposal=ab12cd34ef56` must name that fingerprint, and answers `{"children": ["acme-org/website#13", "acme-org/website#14"]}`, or `409` if `proposal` no longer names the proposal, the task is not waiting for a split or a slice fails its checks. It starts no cycle. Declining or changing a proposal happens in Didban.

`POST /api/v1/projects/{name}/pause` answers `{"paused": true}` and `resume` answers `{"paused": false}`. A step already running finishes, and nothing after it starts. Resume answers `409` if the project is paused by `paused: true` in its config, which only a config change lifts.
