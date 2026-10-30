---
description: Define a task and put it on the board
---
Read `docs/COMMANDS.md`, then turn what the user wants into one issue, using `prompts/templates/issue.md`. The quality rules are in DESIGN §2.

1. Find the project in `local/projects.yaml`. If it is not registered, stop and point to `/kandu-add`.
2. Read the repo and its open and closed issues. Check that the behavior does not already exist, and that `local/memory/<project>.md` does not list it as rejected. If it does, tell the user and stop.
3. For a large or vague task, interview the user in rounds: numbered questions, each with a recommended answer, and only what can be decided now. Look facts up yourself. Skip this for a small, clear task.
4. If the task is more than one slice, split it into issues, each delivering a complete path, and name the order in `Blocked by`.
5. Write each issue: behavior instead of steps, no file paths or line numbers, and acceptance criteria that can each be checked alone.
6. Show the text and wait for the user's yes.
7. Save the body to a temporary file and run `.venv/bin/python -m kandu.tasks --project NAME --title TITLE --body-file FILE`, adding `--repo` for a project with several repos. Finish when it prints the issue's URL.
