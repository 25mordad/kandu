# State files

Kandu keeps its runtime record in `state/` (gitignored, readable only by its user). This format is part of the integration interface (DESIGN §7). A change that breaks it raises `v`.

```
state/
  current.json            what is happening now
  events/2026-10-30.jsonl what happened that day, one JSON event per line
  requests/<id>.json      what the user asked for, until it is tasks
  didban/<session>.json   what one Didban session spent and did
```

## Rules
- The event log is append-only, and nothing is ever edited. Reports such as tokens by day are computed from it.
- Each event type has a fixed set of fields, enforced in `kandu/state.py`. The values are not inspected, so a caller must never pass a task's text, command output, a prompt or a secret.
- Timestamps are ISO 8601 with the machine's offset. A day's file is that machine's local date.
- A reader splits lines on `\n` only, and skips any line that does not parse: a crash can cut one off. The next event starts on a new line.

## Common fields

| Field | Meaning |
|---|---|
| `v` | format version, currently `1` |
| `ts` | time of the event |
| `type` | one of the types below |

`task` is `owner/repo#number` and `project` is the name in the registry. Both are `null` on a `run_finished` of the supervisor that belongs to no task; reports count such a run under `no_task`.

## Event types

| Type | Fields |
|---|---|
| `supervisor_woke` | none |
| `supervisor_slept` | `reason`, `next_wake` |
| `queen_decided` | `task`, `project`, `action`, `note` |
| `notes_sized` | `task`, `project`, `run_id`, `head_sha`, `notes` |
| `debt_reported` | `task`, `project`, `finding`, `title`, `body`, `size`, `reason` |
| `debt_parked` | `task`, `project`, `finding`, `url`, `note` |
| `queen_finished` | `run_id`, `brief`, `backlog` |
| `task_picked` | `task`, `project` |
| `phase_changed` | `task`, `project`, `phase` |
| `decision` | `task`, `project`, `decision`, `reason` |
| `run_finished` | `task`, `project`, `run_id`, `role`, `models`, `duration_ms`, `is_error` |
| `review_finished` | `task`, `project`, `run_id`, `round`, `head_sha`, `findings`, `blockers`, `door`, `criteria_met`, `wait_for_user`, `user_brief` |
| `user_approved` | `task`, `project`, `head_sha` |
| `priority_raised` | `task`, `project` |
| `task_sized` | `task`, `project`, `run_id`, `size`, `files`, `door`, `criteria`, `slices` |
| `slice_created` | `task`, `project`, `parent` |
| `task_split` | `task`, `project`, `children` |
| `split_declined` | `task`, `project` |
| `question_answered` | `task`, `project`, `head_sha`, `question`, `answer` |
| `notification_sent` | `kind`, `key` |
| `notification_failed` | `kind`, `key`, `reason` |
| `project_paused` | `project` |
| `project_resumed` | `project` |
| `limits_changed` | `session`, `week` |
| `group_closed` | `task`, `project` |
| `deployed` | `task`, `project`, `run_id` |
| `project_checked` | `project`, `passed`, `detail` |
| `task_finished` | `task`, `project`, `outcome` |

`role` is `supervisor`, `queen`, `splitter`, `worker`, `reviewer` or `live_check`. `models` maps each model a run used to its tokens:

```json
{"v": 1, "ts": "2026-10-30T12:00:00+01:00", "type": "run_finished",
 "task": "acme-org/website#12", "project": "website", "run_id": "r-41",
 "role": "worker", "duration_ms": 94000, "is_error": false,
 "models": {"claude-sonnet-5-5": {"input": 2100, "output": 8400, "cache_read": 91000, "cache_creation": 12000}}}
```

The numbers come from `modelUsage` in the `result` event of `claude --output-format stream-json`.

`review_finished` is written for every review round, even with no findings. `findings` and `blockers` are counts, `round` starts at 1 and `door` is `two-way` or `one-way`. The text of a finding is not logged here. The merge gate reads this event, not the PR comment, because a worker uses the same account and could write one. `wait_for_user` holds the reviewer's one-line questions; `project_checked` is the end-of-cycle check.

## current.json
A snapshot, replaced atomically, so a reader never sees half a file.

| Field | Meaning |
|---|---|
| `v` | format version |
| `supervisor` | `awake`, `asleep` or `stopped` |
| `last_reason` | why the last cycle ended |
| `next_wake` | expected time of the next cycle, if the timer is on |
| `usage` | `{"session": percent, "week": percent}`, or `null` if not read |
| `workers` | one object per active task: `task`, `project`, `machine`, `phase`, `started` |
| `queen_run` | the run id of the queen while she is awake, otherwise null |
| `waiting_for_machine` | tasks whose machine was off in the last cycle |

`phase` is `implement`, `review round N`, `sizing notes` or `fix round N`.

## Other files
`state/stopped` exists while the user has stopped the supervisor. `state/paused/<project>` exists while the user has paused that project. `state/supervisor.lock` is held during a cycle, `state/split.lock` while a split is accepted. The full stream of each agent run is in `logs/<run_id>.jsonl`, readable only by Kandu's user, and is not part of this interface.
