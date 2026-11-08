# Glossary

One word for each concept. Use these words in code, docs, prompts and issues. A project keeps its own glossary in its memory (DESIGN §14).

| Term | Meaning |
|---|---|
| **Brain** | The always-on machine where the supervisor runs |
| **Supervisor** | The code loop that picks tasks, starts agents and enforces the rules; it is not an LLM session and writes no code |
| **Queen** | The one LLM that runs the cycle for all projects: she releases tasks from `Backlog` and judges what stopped; code checks each decision, and she cannot merge |
| **Worker** | An agent that does one task in its own `git worktree` |
| **Technical debt** | The column for tasks the user parked without refining them; no agent reads it, and Didban brings them up |
| **Splitter** | A read-only agent that sizes every task in `Backlog` against the scale and proposes how to split a `Large` one |
| **Held task** | A task in `Backlog` that the splitter found must change a forbidden area of its project; no agent works on it until the user releases it in Didban |
| **Group** | The slices of one split task: chained by `Blocked by`, each with its own PR; built side by side, merged one after another |
| **Reviewer** | A fresh agent that reviews a PR, knowing nothing of how it was built |
| **Didban** | The control role, run from the machine the user sits at |
| **Free flight** | A session the user opens on a task from the dashboard, with full access; Kandu's agents keep running beside it |
| **Zanbourdar** | Persian for "beekeeper": a session the user opens beside Didban from the dashboard, with full access and tied to no task; it may change Kandu itself |
| **Brief** | One short private file per project, `local/brief/<project>.md`, that Didban reads when the user names the project; it changes only through `kandu.brief` |
| **Repo note** | `KANDU.md` in a project's repo, rendered by `kandu.note`: it says Kandu changes the repo, where its tasks are and how a breakage goes back to Kandu |
| **Report file** | A file of the project's own, listed under `reports` in the registry, that Didban reads with the brief and never changes |
| **Bazras** | The inspector bee: a daily, read-only look at a live project's health; its findings go in a private report, never into a task by itself |
| **Finding** (Bazras) | Something Bazras's code found wrong: a reverted Kandu commit, rework, a failed unit or cron line, new error lines in a log, failing tests or a failing check |
| **Request** | What the user wants done, in their words, before it is a task; Didban keeps it until its tasks exist |
| **Family** | A group of repos that share one board |
| **Board** | The GitHub Project of a family |
| **Registry** | `local/projects.yaml`; a project not in it is never touched |
| **Task** | A GitHub issue on a board; only those with the label `agent` enter the loop |
| **Token task** | An `agent` task labelled `uses-tokens`; its worker gets the project's `task_tokens` |
| **Ready task** | An `agent` task in `Ready` whose blockers are all done |
| **Slice** | A task that delivers a complete path that can be verified alone |
| **Blocker** | A review finding that the worker fixes and that stops the merge until it is gone |
| **Note** | A review finding that does not stop a merge. The splitter sizes it: a `Small`, `two-way` one is fixed in the same PR, and the queen parks any other in `Technical debt` |
| **Second review** | The last review of a PR: it checks only the items the first review left for the worker, and reports nothing new |
| **Open question** | Something a review met that the task never settled; it waits for the user's answer in Didban |
| **Calibration** | Comparing Kandu's review and merge decisions with what the user would have done |
| **Two-way door** | A change that is easy to undo |
| **One-way door** | A change that is hard to undo; it always waits for the user |
| **Blast radius** | What a merge can break if it is wrong |
| **Cycle** | One wake of the supervisor, from waking to sleep |
| **Live check** | Seeing the result in the deployed software, not only in tests |
| **Check** | A read-only run the queen sends to find one thing out about a task; the run is the checker |
| **Drop ask** | The queen's question to the user to drop one task from another's `Blocked by`, with her reason; only the user's yes on the dashboard takes it out of the issue. Off until the user turns it on |
| **Loop guard** | The per-task limit on each of the queen's steps; past it the task stops for the user |
