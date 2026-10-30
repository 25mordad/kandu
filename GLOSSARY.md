# Glossary

One word for each concept. Use these words in code, docs, prompts and issues. A project keeps its own glossary in its memory (DESIGN §14).

| Term | Meaning |
|---|---|
| **Brain** | The always-on machine where the supervisor runs |
| **Supervisor** | The code loop that picks tasks, starts agents and enforces the rules; it is not an LLM session and writes no code |
| **Queen** | The one LLM that runs the cycle for all projects: she releases tasks from `Backlog` and judges what stopped; code checks each decision, and she cannot merge |
| **Worker** | An agent that does one task in its own `git worktree` |
| **Splitter** | A read-only agent that sizes every task in `Backlog` against the scale and proposes how to split a `Large` one |
| **Group** | The slices of one split task: chained by `Blocked by`, each with its own PR, run one after another |
| **Reviewer** | A fresh agent that reviews a PR, knowing nothing of how it was built |
| **Didban** | The control role, run from the machine the user sits at |
| **Family** | A group of repos that share one board |
| **Board** | The GitHub Project of a family |
| **Registry** | `local/projects.yaml`; a project not in it is never touched |
| **Task** | A GitHub issue on a board; only those with the label `agent` enter the loop |
| **Ready task** | An `agent` task in `Ready` whose blockers are all done |
| **Slice** | A task that delivers a complete path that can be verified alone |
| **Blocker** | A review finding that the worker fixes and that stops the merge until it is gone |
| **Note** | A review finding that the worker also fixes, but that does not stop a merge once the review rounds are used up |
| **Open question** | Something a review met that the task never settled; it waits for the user's answer in Didban |
| **Calibration** | Comparing Kandu's review and merge decisions with what the user would have done |
| **Two-way door** | A change that is easy to undo |
| **One-way door** | A change that is hard to undo; it always waits for the user |
| **Blast radius** | What a merge can break if it is wrong |
| **Cycle** | One wake of the supervisor, from waking to sleep |
| **Live check** | Seeing the result in the deployed software, not only in tests |
