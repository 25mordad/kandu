## Reading GitHub
Kandu keeps its own copy of GitHub, and its API serves it at no cost. Every `gh` read spends the account's hourly GitHub limit, which Kandu's agents need too.

- A task's issue: `curl -s {api}/tasks/OWNER/REPO/NUMBER`. Its PR and review rounds: `curl -s {api}/reviews/OWNER/REPO/NUMBER`.
- Boards, their columns, fields and cards: `curl -s {api}/boards`. A card's `status` is in Kandu's column names; `fields` are as the board names them.
- Read GitHub with `gh` only for what these do not hold, such as an issue's comments. Read a PR's diff with `git` in its worktree.
