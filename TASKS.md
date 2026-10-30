# TASKS — build roadmap

Design: [DESIGN.md](DESIGN.md). Early development; Kandu does not run yet.

## [P1] Core
1. [x] Config format + loader for `local/kandu.yaml` and `local/projects.yaml`; examples in `config.example/` (DESIGN §4, §12)
2. [ ] `/kandu-setup` — initial setup by conversation, with prerequisites check (§1A)
3. [ ] `/kandu-add` — add a project by conversation (§1B)
4. [ ] `/kandu-doctor` — health check (§1C)
5. [ ] `/kandu-task` — define a task by conversation; issue template; standard board columns (§2, §3)
6. [x] Didban: one command from the user's own machine, control role, reads `local/` over SSH (§13)
7. [ ] State files + event log in a documented format (§7)
8. [ ] Dashboard with live stream and stop / resume controls; documented HTTP API (§7)
9. [ ] Supervisor: usage check, lock file, task selection, `systemd timer` (§5, §6)
10. [ ] Workers in `git worktree`s, local and over SSH, behind a swappable runner layer (§4, §5, §9)
11. [ ] Reviewer + review rounds (§5)
12. [ ] Merge, deploy, live check, end-of-cycle check (§5)
13. [ ] Notifier with a Telegram adapter, important events only (§8)
14. [ ] Claude login watch: expiry per machine on the dashboard, early warning, works when `claude` is down (§7, §8)
15. [ ] Move the running install to a private copy with pushes to `upstream` disabled, before the first board (§12)

## [P2] Next
16. [ ] `/kandu-config` — change config by conversation (§1C)
17. [ ] Usage report per project (§6)
18. [ ] Pilot on one real project, then roll out

## [P3] Later
19. [ ] Optional isolation runners: separate Linux user, Docker (§9)
20. [ ] Guide for a bot account / GitHub App (§10)
