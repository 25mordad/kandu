# State files

Kandu keeps its runtime record in `state/` (gitignored, readable only by its user). This format is part of the integration interface (DESIGN §7). A change that breaks it raises `v`.

```
state/
  current.json            what is happening now
  events/2026-10-30.jsonl what happened that day, one JSON event per line
```

## Rules
- The event log is append-only, and nothing is ever edited. Reports such as tokens by day are computed from it.
- Each event type has a fixed set of fields, enforced in `kandu/state.py`. A task's text, command output, prompts and secrets are never written here.
- Timestamps are ISO 8601 with the machine's offset. A day's file is that machine's local date.
- A reader must skip a last line that does not parse: a crash can cut one off.

## Common fields

| Field | Meaning |
|---|---|
| `v` | format version, currently `1` |
| `ts` | time of the event |
| `type` | one of the types below |

`task` is `owner/repo#number` and `project` is the name in the registry.

## Event types

| Type | Fields |
|---|---|
| `supervisor_woke` | none |
| `supervisor_slept` | `reason`, `next_wake` |
| `task_picked` | `task`, `project` |
| `phase_changed` | `task`, `project`, `phase` |
| `decision` | `task`, `project`, `decision`, `reason` |
| `run_finished` | `task`, `project`, `run_id`, `role`, `models`, `duration_ms`, `is_error` |
| `task_finished` | `task`, `project`, `outcome` |

`role` is `supervisor`, `worker` or `reviewer`. `models` maps each model a run used to its tokens:

```json
{"v": 1, "ts": "2026-10-30T12:00:00+01:00", "type": "run_finished",
 "task": "acme-org/website#12", "project": "website", "run_id": "r-41",
 "role": "worker", "duration_ms": 94000, "is_error": false,
 "models": {"claude-sonnet-5-5": {"input": 2100, "output": 8400, "cache_read": 91000, "cache_creation": 12000}}}
```

The numbers come from `modelUsage` in the `result` event of `claude --output-format stream-json`.

## current.json
A snapshot, replaced atomically, so a reader never sees half a file. It always has `v`. The supervisor (task 9) defines the remaining fields.
