---
description: Add a project to Kandu, by conversation
---
Read `docs/COMMANDS.md`, then add one project to `local/projects.yaml`, in the shape of `config.example/projects.example.yaml`. A project that is not in this file is never touched.

1. Read what you can: the git remote, existing boards, the test command, how it deploys, CI, PR bots, branch protection.
2. Show the findings, then ask as one numbered list:
   1. Name, one line on what it is, and its family.
   2. Which repos, and who owns them?
   3. Which machine and path?
   4. May Kandu clone it into its work folder (`clone`), or must it work on that machine in a separate `git worktree` (`in-place`)? Always ask this.
   5. Which board? An existing board may have other column names: map them under `board.columns`. If there is none, should one be created?
   6. The test and local run commands, and any extra tools its workers need (`allowed_tools`).
   7. `direct` or `preview` deploy, and the live URL. Leave the URL out if the live software is behind a login.
   8. Where are its secrets? Store the location only.
   9. Forbidden areas, such as migrations or payments.
   10. Repo rules: CI, PR bots, other people on the repo, and any freeze on merges (`frozen`).
   11. Automatic merge, or approval? Recommend approval until calibration has run.
   12. A daily token limit (`daily_token_limit`), and any rules for how the queen paces this project (`queen_rules`).
   13. Priority against the other projects.
   14. A short glossary of project terms: propose it, the user corrects it.
3. Create a board only if the user said yes: `.venv/bin/python -m kandu.boards --owner OWNER --title TITLE`. It prints the number. Before the first board of this install, stop and remind the user that direct pushes to the Kandu repo end at that moment.
4. Write the entry. Put the glossary in `local/memory/<project>.md`.
5. Name a backup of what the project cannot rebuild as its first task, and refine it with `/kandu-task`.
6. Finish when `.venv/bin/python -m kandu.doctor` shows no `FAIL` for this project.
