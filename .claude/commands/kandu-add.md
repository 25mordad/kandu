---
description: Add a project to Kandu, by conversation
---
Read `docs/COMMANDS.md`, then add one project to `local/projects.yaml`, in the shape of `config.example/projects.example.yaml`. A project that is not in this file is never touched.

1. Read what you can: the git remote, existing boards, the test command, how it deploys, CI, PR bots, branch protection. `.venv/bin/python -m kandu.add project --machine MACHINE --path PATH --dry-run` reads the repo, test command, deploy script, secrets file, forbidden folders and repo rules from the copy and changes nothing; start from its proposal and its warnings.
2. Show the findings, then ask as one numbered list:
   1. Name, one line on what it is, and its family.
   2. Which repos, and who owns them?
   3. Which machine and path? A machine not yet in `kandu.yaml` is added first with `.venv/bin/python -m kandu.add machine --name NAME --ssh ALIAS [--always-on] [--run-tmp-dir FOLDER]`: ask whether it is always on, and whether each run there should get its own temporary folder on its own disk (`--run-tmp-dir`, off unless the user says yes; recommend it where `/tmp` is small or in memory). It can be changed later from the dashboard's settings.
   4. May Kandu clone it into its work folder (`clone`), or must it work on that machine in a separate `git worktree` (`in-place`)? Always ask this.
   5. Which board? An existing board may have other column names: map them under `board.columns`. If there is none, should one be created?
   6. The test and local run commands, the `setup` a fresh checkout needs before the tests (such as `npm ci`, with `inputs: [package-lock.json]`; none if the tests run in a fresh checkout as they are), and, if `permissions` in `kandu.yaml` limits the workers, any extra tools they need here (`allowed_tools`).
   7. `direct` or `preview` deploy, and the live URL. Leave the URL out if the live software is behind a login. A deploy that pulls into a live checkout runs `<kandu>/.venv/bin/python -I <kandu>/kandu/pull.py <checkout>`, with Kandu's own path on that machine, never a plain `git pull`, so a conflict leaves the copy as it was.
   8. Where are its secrets? Store the location only. Which tokens may its tasks use, and what is each for (`task_tokens`)?
   9. Forbidden areas, such as migrations or payments.
   10. Repo rules: CI, PR bots, other people on the repo, and any freeze on merges (`frozen`).
   11. Automatic merge, or approval? Recommend approval until calibration has run.
   12. A daily token limit (`daily_token_limit`), and any rules for how the queen paces this project (`queen_rules`).
   13. Priority against the other projects.
   14. A short glossary of project terms: propose it, the user corrects it.
   15. Does the project keep its own reports for Kandu, such as a lessons file in its repo? List them under `reports`, as paths on its machine; a relative path starts at `path`. Didban reads their newest lines, read-only.
   16. Its brief for Didban: draft it from what you read, in English, under `# NAME` with the sections `## What it is`, `## Main parts`, `## Rules and decisions`, `## Incidents`, `## Reports`, short, and the user corrects it.
   17. Turn on the daily Bazras? It is off unless the user says yes, and can be turned on later in the dashboard's Bazras tab. Only a live project needs it. If so, `inspect.enabled: true`, its hour (`inspect.at`, default `bazras.at`), whether to run the tests, which systemd units on its machine to watch (`user:` for a user unit), which crontab lines must stay, which log files to read for new errors (`logs`), its own check commands, and files whose later change is not rework (`ignore`). It is read-only and spends this project's daily limit only when it finds something.
   18. Who accepts a split proposal for a task too large to run: the user (`split_approval: user`, the default) or the queen (`queen`)? Recommend the user. Either way every proposal waits for the user's yes until the queen can accept one, and the dashboard's settings section changes it later.
   19. May the queen's yes to a merge answer its one-way door and the review's questions for the user (`queen_answers_one_way: true`)? It is off unless the user says yes: such a merge then waits for the user's yes. The dashboard's settings section changes it later.
   20. Do several people share this board, each with a Kandu of their own (`multi_user: true`)? Then this install works only the tasks assigned on GitHub to its `github.account`, and each person assigns a task to themselves before their Kandu picks it up. It is off unless the user says yes; the dashboard's settings pop-up changes it later.
   21. Should Kandu hold the project when its `main` is red (`watch_main: true`)? When a PR's tests fail, Kandu tests `main`; if two runs on one commit fail, the project's merges wait and only one task that fixes `main` starts. It is off unless the user says yes; the dashboard's settings pop-up changes it later.
   22. Does it need its own model for any role (`models`): worker, reviewer, splitter, live check or Bazras? Show the global `claude.models` from `kandu.yaml`; a role not named keeps it, and the queen's is global only. Leave `models` out unless the user names one; the settings pop-up changes it later.
3. Create a board only if the user said yes: `.venv/bin/python -m kandu.boards --owner OWNER --title TITLE`. It prints the number. Before the first board of this install, stop and remind the user that direct pushes to the Kandu repo end at that moment.
4. Write the entry. Put the glossary in `local/memory/<project>.md`. Save the brief the user said yes to with `.venv/bin/python -m kandu.brief set NAME --file -`, the brief on stdin; finish when it exits 0.
5. Name a backup of what the project cannot rebuild as its first task, and refine it with `/kandu-task`.
6. Show the repo note, `.venv/bin/python -m kandu.note NAME`, which only prints, and offer it as a task for this project: add the note as `KANDU.md` and its pointer line to the repo's `CLAUDE.md` and `AGENTS.md`. Refine and file it with `/kandu-task` only if the user said yes. Never write the note into the repo yourself.
7. Finish when `.venv/bin/python -m kandu.doctor` shows no `FAIL` for this project.
