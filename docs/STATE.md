# State files

Kandu keeps its runtime record in `state/` (gitignored, readable only by its user). This format is part of the integration interface (DESIGN §7). A change that breaks it raises `v`.

```
state/
  current.json            what is happening now
  bazras.json             the project Bazras is looking at, only while it looks
  events/2026-10-30.jsonl what happened that day, one JSON event per line
  requests/<id>.json      what the user asked for, until it is tasks
  didban/<session>.json   what one Didban session spent and did
  flights/<key>.json      a session opened from the dashboard: its link, with its tmux socket and prompt beside it
  flown/<session>.json    what one free flight or Zanbourdar session spent and did, after it ended
  gh/2026-11-03.jsonl     each gh call Kandu and its agents made that day, and samples of GitHub's limit
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
| `queen_finished` | `run_id`, `brief`, `backlog`, `by_hand` |
| `queen_step_done` | `task`, `project`, `action`, `decided`, `result`, `summary`, `head_sha`, `run_id` |
| `queen_opened` | `task`, `project`, `url` |
| `loop_stopped` | `task`, `project`, `action`, `reason` |
| `task_picked` | `task`, `project` |
| `phase_changed` | `task`, `project`, `phase` |
| `decision` | `task`, `project`, `decision`, `reason` |
| `merge_held` | `task`, `project`, `head_sha`, `blockers`: recorded after a `merge_waits` decision whose only reasons are open blockers |
| `run_finished` | `task`, `project`, `run_id`, `role`, `models`, `duration_ms`, `is_error` |
| `review_finished` | `task`, `project`, `run_id`, `round`, `head_sha`, `findings`, `blockers`, `door`, `criteria_met`, `wait_for_user`, `user_brief` |
| `user_approved` | `task`, `project`, `head_sha` |
| `user_resumed` | `task`, `project`, `to` |
| `queen_approved` | `task`, `project`, `head_sha`, `note` |
| `copy_not_read` | `detail` |
| `priority_raised` | `task`, `project` |
| `task_sized` | `task`, `project`, `run_id`, `size`, `files`, `door`, `criteria`, `slices` |
| `slice_created` | `task`, `project`, `parent` |
| `task_split` | `task`, `project`, `children` |
| `blocker_drop_asked` | `task`, `project`, `blocker`, `reason`: the queen asks the user to drop this blocker from the task's issue; nothing changes until the user says yes |
| `blocker_dropped` | `task`, `project`, `blocker`: the user said yes, and the blocker left the issue's `## Blocked by` |
| `blocker_rewritten` | `task`, `project`, `old`, `new`, `when` (`split` or `defined`) |
| `split_declined` | `task`, `project` |
| `task_held` | `task`, `project`, `areas`, `reason` |
| `hold_released` | `task`, `project` |
| `question_answered` | `task`, `project`, `head_sha`, `question`, `answer` |
| `notification_sent` | `kind`, `key` |
| `notification_failed` | `kind`, `key`, `reason` |
| `project_paused` | `project` |
| `project_resumed` | `project` |
| `limits_changed` | `session`, `week` |
| `drop_blockers_changed` | `enabled`: the user let the queen ask to drop a blocker, or stopped her, from the dashboard |
| `requests_shown_changed` | `enabled`: the user showed the open requests on the dashboard, or hid them |
| `parallel_changed` | `total` (or null), `projects`: name → number, for those changed |
| `kandu_restarted` | none |
| `wake_requested` | none |
| `group_closed` | `task`, `project` |
| `spec_created` | `task`, `project`: `task` is the spec issue |
| `spec_named` | `task`, `project`, `spec`: the task was created as part of this spec |
| `spec_closed` | `task`, `project`: the last task that names the spec closed, so the spec did too |
| `deployed` | `task`, `project`, `run_id` |
| `project_checked` | `project`, `passed`, `detail` |
| `project_inspected` | `project`, `run_id`, `passed`, `findings`, `judged` |
| `inspect_changed` | `project`, `enabled`, `at` |
| `backup_made` | `copy`, `files`, `removed` |
| `backup_failed` | `reason` |
| `backup_changed` | `enabled`, `at`, `destination`, `keep` |
| `task_finished` | `task`, `project`, `outcome` |

`role` is `supervisor`, `queen`, `bazras`, `splitter`, `worker`, `reviewer` or `live_check`; a `bazras` run has no task, and its `project` is the one it looked at. `models` maps each model a run used to its tokens:

```json
{"v": 1, "ts": "2026-10-30T12:00:00+01:00", "type": "run_finished",
 "task": "acme-org/website#12", "project": "website", "run_id": "r-41",
 "role": "worker", "duration_ms": 94000, "is_error": false,
 "models": {"claude-sonnet-5-5": {"input": 2100, "output": 8400, "cache_read": 91000, "cache_creation": 12000}}}
```

The numbers come from `modelUsage` in the `result` event of `claude --output-format stream-json`.

`review_finished` is written for every review round, even with no findings. `findings` and `blockers` are counts, `round` starts at 1 and `door` is `two-way` or `one-way`. The text of a finding is not logged here. The merge gate reads this event, not the PR comment, because a worker uses the same account and could write one. `wait_for_user` holds the reviewer's one-line questions; `project_checked` is the end-of-cycle check. `project_inspected` is Bazras's daily look: `findings` is a count, `judged` says whether an agent judged them, and the report itself stays in `local/reports/bazras/`, never in the log; `log-offsets.json` there remembers how far each of the project's log files was read. `inspect_changed` is the user turning Bazras on or off for a project in the dashboard; `at` is null when the project takes `bazras.at`. `backup_made` is a daily backup (DESIGN §12): `copy` is the new copy's file name, `files` how many files it holds and `removed` the names of the old copies it removed. `backup_failed` holds a reason that names no file's content; no copy was made or removed. `backup_changed` is the user setting the backup in the dashboard.

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

## gh/<day>.jsonl
`meter/gh` stands first on the PATH of Kandu, its agents, Didban and dashboard sessions. It runs the real `gh` and writes one line per call:

| Field | Meaning |
|---|---|
| `type` | `call` |
| `caller` | the process that ran it: `supervisor`, `dashboard`, `didban`, `flight-<key>`, … |
| `run` | the agent run it came from, if any; `run_finished` names its role and task |
| `command` | the first two words, such as `pr view`; `api graphql` or `api rest` for `gh api` |
| `kind` | `graphql` or `rest`: the hourly limit it counts against |
| `exit` | the exit code of `gh` |

No other argument is kept. The dashboard adds a `limit` line every five minutes with `graphql_used`, `graphql_reset`, `rest_used` and `rest_reset` as GitHub reports them, so points that no counted call explains came from outside Kandu. `.venv/bin/python -m kandu.ghmeter` shows a day by hour, caller and command.

## Other files
`state/stopped` exists while the user has stopped the supervisor. `state/paused/<project>` exists while the user has paused that project. `state/bazras.json` names the project Bazras is looking at, only while it looks, and `state/bazras.lock` is held during its run. `state/supervisor.lock` is held during a cycle, `state/split.lock` while a split is accepted. The full stream of each agent run is in `logs/<run_id>.jsonl`, readable only by Kandu's user, and is not part of this interface.
