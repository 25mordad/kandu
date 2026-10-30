# Rules for the `/kandu-*` commands

Read this first in every command.

- Answer in the user's language. Number every question and every list of tasks.
- Look first, ask last. Find out what you can from the system, and ask only what you cannot. Show what you found, then ask the user to confirm it.
- Never infer a project's run location or a merge mode. Always ask.
- `local/` on the brain's machine is the only copy of the config. If `local/didban.yaml` exists here, you are on the user's own machine: run every step that reads or writes `local/` on the brain, with the `reach` and `install_dir` in that file.
- Secrets: store where a value is, never the value. Never print, log or commit one.
- Python runs from `.venv`: `.venv/bin/python -m kandu.<module>`.
- Finish on a real check: the step names the command that must pass.
