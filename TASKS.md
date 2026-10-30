# TASKS — build roadmap

Design: [DESIGN.md](DESIGN.md). Early development: the queen, workers and review rounds have run on a real project. Merge, deploy and live check have not yet.

Every task is designed and checked against harness, context and memory (§14).

## [P1] Core
1. [x] Config format + loader for `local/kandu.yaml` and `local/projects.yaml`; examples in `config.example/` (DESIGN §4, §12)
2. [x] `/kandu-setup` — initial setup by conversation, with prerequisites check (§1A)
3. [x] `/kandu-add` — add a project by conversation (§1B)
4. [x] `/kandu-doctor` — health check (§1C)
5. [x] `/kandu-task` — define a task by conversation, with the existing-work check and the interview for large tasks; uses `prompts/templates/issue.md`; standard board columns (§2, §3)
6. [x] Didban: one command from the user's own machine, control role, reads `local/` over SSH (§13)
7. [x] State files + event log in a documented format, including tokens per task and model (§7)
8. [x] Dashboard with live stream, by-day view with tokens per task, PR page in plain words, stop / resume controls; versioned HTTP API documented in `docs/API.md` (§7)
9. [x] Supervisor: usage check, lock file, task selection, `systemd timer` (§5, §6)
10. [x] Workers as `claude` processes in `git worktree`s, local and over SSH, behind a swappable runner layer; connect `prompts/worker.md` and put `prompts/templates/pr.md` in its prompt (§4, §5, §9)
11. [x] Reviewer + review rounds, the worker fixes every finding; report validated in code, a later round only on a new `SHA` with no LLM gate, companion PRs, every round logged; connect `prompts/reviewer.md` (§5, §15)
12. [x] Merge on the reviewed `SHA` with all merge conditions in code, deploy, live check, end-of-cycle check; a one-way door blocks auto-merge; the merge gate reads the review from the local event log, because a worker uses the same account and could write a PR comment; replaces the current `MERGED` → `Done` step (§5)
13. [ ] Notifier with a Telegram adapter, important events only (§8)
14. [ ] Claude login watch: expiry per machine on the dashboard, early warning, works when `claude` is down (§7, §8)
15. [x] Move the running install to a private copy with pushes to `upstream` disabled, before the first board (§12)

## [P2] Next
16. [x] `/kandu-config` — change config by conversation (§1C)
17. [ ] Usage report per task, day and project (§6)
18. [ ] Pilot on one real project, then roll out
19. [ ] Project memory in `local/`: lessons, glossary, decisions, rejected requests and do-not-flag list, read by the next worker and the reviewer (§14)
20. [ ] Periodic model and environment review with a report on the dashboard, including calibration: offered, kept and dropped per review round (§15)

## [P3] Later
21. [ ] Optional isolation runners: separate Linux user, Docker (§9)
22. [ ] Guide for a bot account / GitHub App (§10)
23. [ ] Integration PR check: blockers of a PR still open must not reach `main` through a branch that merges it (§5)
24. [x] Hive design for the dashboard: the meadow, with bees for agents and flowers for tasks, all from real state (§7)
25. [ ] Show the dashboard inside another private app in a frame, through that app's server, opening no new port
26. [ ] A film of the dashboard for the README with a voice explaining what Kandu does, only after the first real round works
27. [ ] Every project gets a backup of what it cannot rebuild as its first task (`/kandu-add`)
28. [x] The queen: one LLM that paces work and judges what stopped for all projects, under locks kept in code (§5)
29. [x] A splitter agent that sizes every task against a scale in code and proposes a split for a large one. The user approves it, the slices run as a group, and the queen may not start a task without a size (§2)
30. [x] Approve a PR from the dashboard, for one `SHA`, with every other merge condition still in code (§5, §7)
31. [x] A modern dashboard that reads well in Persian: its own font, Persian digits, a folding table per project, details in a dialog (§7)
32. [x] What a review could not settle from the task stays an open question, answered through Didban and kept in the project's memory (§13)
33. [x] Pause one project from the dashboard: its board stays, and no agent, queen or merge touches it until it is resumed (§7)
34. [x] See a split proposal on the dashboard and accept it there; declining or changing it stays in Didban (§2, §7)
35. [x] Choose a model per role in `claude.models` (§1A)
36. [x] Read a split proposal whole in the user's language on the dashboard; the issues stay in English (§2, §7)

## Technical debt
37. [ ] News of a paused project can be lost: if another project wakes the queen during the pause, her look moves past that news, and after a resume she never judges it. A failed task then stays `Blocked` until the user sees it on the board (§5, §7)
