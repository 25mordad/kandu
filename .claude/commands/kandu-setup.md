---
description: First setup of Kandu, by conversation
---
Read `docs/COMMANDS.md`, then set up this install, once. The result is `local/kandu.yaml` and `local/.env`, in the shape of `config.example/`.

1. Check the prerequisites: `claude`, `gh`, `git`, `python3`, `gh auth status` with the `project` scope. Finish when each is present, or the user knows what to install.
2. Create `.venv`, install `requirements.txt` and run `git config core.hooksPath .githooks`.
3. Ask, as one numbered list, what you could not find out:
   1. Which machine runs the brain, and is it always on?
   2. Which other machines exist, how are they reached, and which are always on?
   3. Subscription or API key? The 5-hour and weekly limits, in percent. Point out that a shared subscription needs a margin.
   4. The GitHub account, and the owners Kandu may work in.
   5. Notifications: Telegram or none? In which language should Kandu talk to the user (`language`, default `en`)?
   6. Parallel workers, wake interval, auto-merge, and the queen. Recommend leaving auto-merge off and the queen on: she costs tokens only when there is something to decide. Ask for her daily token limit (`queen_daily_token_limit`).
   7. Which sources may Didban read, and on which machine? From the user's own machine, how is the brain reached?
   8. Which port does the dashboard use? The default is 8790; `ss -ltn` on the brain's machine must not show it in use.
   9. Which model does each role use (`claude.models`)? Recommend a cheaper model for the splitter and the live check, and Claude's default for the worker, reviewer and queen, who judge the work.
4. Write `local/kandu.yaml`. For Didban, write `local/didban.yaml` on the user's own machine, from `config.example/didban.example.yaml`.
5. Write `local/.env` from `config.example/example.env`, mode 600. Fill `KANDU_DASHBOARD_TOKEN` with `openssl rand -hex 32`, without showing it. The user fills in the Telegram values themselves.
6. Ask for the words that must never reach a public repo, and write them to `local/private-words.txt`, one per line.
7. Finish when `.venv/bin/python -m kandu.config` passes and `.venv/bin/python -m kandu.doctor` has no `FAIL`.
8. Tell the user the dashboard alone starts, and comes back after a restart, with `.venv/bin/python -m kandu.timer --dashboard`, and the timer and dashboard together with `--enable`. Leave both to them. On a server, user units start at boot only after `loginctl enable-linger`.
