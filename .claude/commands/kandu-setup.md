---
description: First setup of Kandu, by conversation
---
Read `docs/COMMANDS.md`, then set up this install, once. The result is `local/kandu.yaml` and `local/.env`, in the shape of `config.example/`.

1. Check the prerequisites: `claude`, `gh`, `git`, `python3`, `gh auth status` with the `project` scope. Finish when each is present, or the user knows what to install.
2. Create `.venv`, install `requirements.txt` and run `git config core.hooksPath .githooks`.
3. Ask, as one numbered list, what you could not find out:
   1. Which machine runs the brain, and is it always on?
   2. Which other machines exist, how are they reached, and which are always on? Should any machine, the brain's too, give each run its own temporary folder on its own disk (`run_tmp: true` and `run_tmp_dir`, off by default)? Recommend it where `/tmp` is small or in memory (`df -h /tmp` shows `tmpfs`), with a folder outside the install such as `~/kandu-tmp`. It can be changed later from the dashboard's settings.
   3. Subscription or API key? The 5-hour and weekly limits, in percent. Point out that a shared subscription needs a margin.
   4. The GitHub account, and the owners Kandu may work in.
   5. Notifications: Telegram or none? In which language should Kandu talk to the user (`language`, default `en`)? Should notifications use another language (`notify.language`)? Which of `notify.events` should be sent (default all)? With Telegram: should the message about a PR that waits for the user's yes carry an Approve merge button (`notify.telegram.buttons`, off by default)? A press asks once more and approves only the head it showed.
   6. Parallel workers, parallel sizers (`max_parallel_sizers`, default 4: the splitter only reads, so these run side by side), wake interval, how often Kandu syncs its copy of GitHub (`github.sync_every_minutes`, default 10: reads GitHub and sends queued changes), auto-merge, and the queen. Recommend leaving auto-merge off and the queen on: she costs tokens only when there is something to decide. Ask for her daily token limit (`queen_daily_token_limit`), and how many hours a machine may stay unreachable while tasks wait for it before the user hears (`unreachable_alert_hours`, default 12).
   7. Which sources may Didban read, and on which machine? From the user's own machine, how is the brain reached?
   8. Which port does the dashboard use? The default is 8790; `ss -ltn` on the brain's machine must not show it in use.
   9. Which model does each role use (`claude.models`)? Recommend a cheaper model for the splitter and the live check, and Claude's default for the worker, reviewer and queen, who judge the work.
   10. Should Kandu back up `local/` and `state/` once a day (`backup`, off by default)? If so: the hour, a folder on the brain's machine outside the install, and how many copies to keep. Point out that a copy holds the secrets of `local/.env`, and that it stays on this machine.
   11. May the queen ask the user to drop a blocker a task does not need (`supervisor.queen_drop_blockers`, off by default)? She only asks: the blocker leaves the issue only on the user's yes in the dashboard, where it can also be turned on later.
   12. Should the dashboard list the open requests with their decision maps (`dashboard.show_requests`, off by default)? It only reads, and can be turned on later from the dashboard.
   13. Should a merged task's folders and `kandu/` branches be removed after its merge (`supervisor.clean_merged`, on by default, as an exception)? A folder with work that is not in the PR stays. It can be turned off later from the dashboard's settings.
   14. Should the dashboard show how far this install's `main` is behind or ahead of the public Kandu it came from (`upstream`, off by default)? If so: the remote of the public repo (default `upstream`), its branch (default `main`), how often to check (`every_hours`, default 4), and which project in `local/projects.yaml` is this install's own repo, for the one task "Bring in updates" creates. The check only fetches and counts: updates arrive only as that task's PR, on the user's yes.
   15. Which look should the dashboard open in for a browser that has not chosen one (`dashboard.look`): Classic, the default, Classic new (Classic made compact) or Kandu Clean? Each browser can still choose its own, and the default can be changed later from the Settings tab of either look.
   16. May the agents do everything, or should some roles be limited (`permissions`)? Without `permissions` every role may run any command, use the network and SSH, and read secrets files. To limit a role, start from the commented set in `config.example/kandu.example.yaml`.
4. Write `local/kandu.yaml`. For Didban, write `local/didban.yaml` on the user's own machine, from `config.example/didban.example.yaml`.
5. Write `local/.env` from `config.example/example.env`, mode 600. Fill `KANDU_DASHBOARD_TOKEN` with `openssl rand -hex 32`, without showing it. The user fills in the Telegram values themselves, here or later from the dashboard's Telegram settings, which check the token with Telegram and find the chat.
6. Ask for the words that must never reach a public repo, and write them to `local/private-words.txt`, one per line. If a project or repo is named after an ordinary word the code uses too, ask whether to write it to `local/public-words.txt`: the guard then lets that exact word through.
7. Finish when `.venv/bin/python -m kandu.config` passes and `.venv/bin/python -m kandu.doctor` has no `FAIL`.
8. Tell the user the dashboard alone starts, and comes back after a restart, with `.venv/bin/python -m kandu.timer --dashboard`, and the timer and dashboard together with `--enable`. Leave both to them. On a server, user units start at boot only after `loginctl enable-linger`.
