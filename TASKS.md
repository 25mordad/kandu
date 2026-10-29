# TASKS — build roadmap

Design: [DESIGN.md](DESIGN.md). Nothing is built yet.

## [P1] Core
1. [ ] Config format + loader for `local/kandu.yaml` and `local/projects.yaml`; examples in `config.example/` (DESIGN §4, §12)
2. [ ] `/kandu-setup` — initial setup by conversation, with prerequisites check (§1A)
3. [ ] `/kandu-add` — add a project by conversation (§1B)
4. [ ] `/kandu-doctor` — health check (§1C)
5. [ ] `/kandu-task` — define a task by conversation; issue template; standard board columns (§2, §3)
6. [ ] State files + event log in a documented format (§7)
7. [ ] Dashboard with live stream and stop / resume controls; documented HTTP API (§7)
8. [ ] Supervisor: usage check, lock file, task selection, `systemd timer` (§5, §6)
9. [ ] Workers in `git worktree`s, local and over SSH, behind a swappable runner layer (§4, §5, §9)
10. [ ] Reviewer + review rounds (§5)
11. [ ] Merge, deploy, live check, end-of-cycle check (§5)
12. [ ] Notifier with a Telegram adapter, important events only (§8)

## [P2] Next
13. [ ] `/kandu-config` — change config by conversation (§1C)
14. [ ] Usage report per project (§6)
15. [ ] Pilot on one real project, then roll out

## [P3] Later
16. [ ] Optional isolation runners: separate Linux user, Docker (§9)
17. [ ] Guide for a bot account / GitHub App (§10)
