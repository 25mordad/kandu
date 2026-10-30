# HTTP API

Version 1. Kandu's own dashboard uses only this API, so anything it shows is open to any other system. A change that breaks a response moves to `/api/v2`.

Start it with `.venv/bin/python -m kandu.dashboard`. It binds to `dashboard.bind` and `dashboard.port` from `local/kandu.yaml`. All responses are JSON, except `/stream`. An error is `{"error": "message"}`.

Reads are open on the bound address. Every `POST` needs `Authorization: Bearer <token>`, where the token is `KANDU_DASHBOARD_TOKEN` in `local/.env`. With no token set, controls are refused.

## Endpoints

| Method | Path | Answer |
|---|---|---|
| GET | `/api/v1/state` | `current.json` (see [STATE.md](STATE.md)), plus `stop_requested` and `limits` |
| GET | `/api/v1/queue` | each project's board columns; cached for 60 seconds |
| GET | `/api/v1/days` | dates that have events |
| GET | `/api/v1/days/{day}` | one day's tokens by task, model and role, and its finished tasks |
| GET | `/api/v1/events` | a page of events; `day` (default: the last) and `after` (default: 0) |
| GET | `/api/v1/stream` | new events as they happen, as Server-Sent Events |
| GET | `/api/v1/runs/{run_id}` | what one agent run did: files read, commands, edits |
| GET | `/api/v1/reviews/{owner}/{repo}/{number}` | the PR of a task and each review round |
| POST | `/api/v1/stop` | stop the supervisor; running agents are killed |
| POST | `/api/v1/resume` | allow the next cycle to run |
| POST | `/api/v1/workers/{run_id}/stop` | stop one running agent; its task moves to `Blocked` |

## Examples

`GET /api/v1/state`
```json
{"v": 1, "supervisor": "awake", "last_reason": null, "next_wake": null,
 "usage": {"session": 28, "week": 41}, "limits": {"session": 70, "week": 60},
 "workers": [{"task": "acme-org/website#12", "project": "website", "machine": "server",
              "started": "2026-10-30T12:00:00+01:00", "phase": "implement", "run_id": "r-20261030120000-a1b2c3"}],
 "waiting_for_machine": [], "queen_run": null, "stop_requested": false}
```

`GET /api/v1/queue`
```json
{"projects": {"website": {"Ready": [{"task": "acme-org/website#13", "title": "Add search",
  "url": "https://github.com/acme-org/website/issues/13"}], "In progress": [], "In review": [],
  "Needs info": [], "Blocked": []}}}
```

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
 "rounds": [{"round": 1, "head_sha": "abc123", "door": "two-way", "summary": "...", "brief": "...", "blast_radius": "...",
             "criteria": [{"criterion": "...", "result": "met"}], "open": [], "recommendation": "ready to merge"}]}
```

`POST /api/v1/stop` answers `{"stop_requested": true}`, `POST /api/v1/resume` answers `{"stop_requested": false}`, and `POST /api/v1/workers/{run_id}/stop` answers `{"stopping": "<run_id>"}`.
