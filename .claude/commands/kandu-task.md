---
description: Define a task and put it on the board
---
Read `docs/COMMANDS.md`, then turn what the user wants into one issue, using `prompts/templates/issue.md`. The quality rules are in DESIGN §2.

1. Find the project in `local/projects.yaml`. If it is not registered, stop and point to `/kandu-add`.
2. Read the repo and its open and closed issues. Check that the behavior does not already exist, and that `local/memory/<project>.md` does not list it as rejected. If it does, tell the user and stop. Then read the code the task touches, read-only on the machine the registry names, until you can name how it will be done, what it changes and what must stay out. Show the user this in plain words. It sets the criteria, `Out of scope` and the size, and it stays out of the issue.
3. Refine every task with the user, in rounds: numbered questions, each with a recommended answer, and only what can be decided now. Look facts up yourself.
4. If the task is more than one slice, split it into issues, each delivering a complete path, and name the order in `Blocked by`.
   Settle each task's fields in the same rounds: `Priority`, `Kind`, `Size` and a `Deadline` if it must be done by a date. The queen decides from these without reading the task, so getting them right is part of refining. `kandu.sizing` holds the scale: a `Small` task has up to 3 acceptance criteria and 5 changed files, a `Medium` one up to 5 and 15, and anything more is `Large`. Work that is hard to undo goes in a task of its own. A `Large` task does not go on the board: split it here, and `kandu.tasks` refuses it otherwise. The splitter still checks every size before the queen sees the task.
5. Write each issue in English, whatever language you and the user talk in: behavior instead of steps, no file paths or line numbers, and acceptance criteria that can each be checked alone.
6. Show the final text and create nothing until the user says yes to it. Naming a task, or saying what comes next, is not a yes.
7. Save the body to a temporary file and run `.venv/bin/python -m kandu.tasks --project NAME --title TITLE --body-file FILE --priority P --kind K --size S`, adding `--deadline YYYY-MM-DD` if there is one, `--repo` for a project with several repos and `--by-hand` if the user will do it themselves. Finish when it prints the issue's URL.

A task the user chooses not to refine now is parked instead, only on their word: run `.venv/bin/python -m kandu.debt add --project NAME --title TITLE --body-file FILE`. It needs an English title and a short text, and steps 3 and 4 wait until Didban takes it up.
