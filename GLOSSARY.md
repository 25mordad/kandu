# Glossary

One word for each concept. Use these words in code, docs, prompts and issues. A project keeps its own glossary in its memory (DESIGN §14).

| Term | Meaning |
|---|---|
| **Brain** | The always-on machine where the supervisor runs |
| **Supervisor** | The headless session that picks tasks and decides; it writes no code |
| **Worker** | An agent that does one task in its own `git worktree` |
| **Reviewer** | A fresh agent that checks a PR on one axis, `Spec` or `Standards` |
| **Didban** | The control role, run from the machine the user sits at |
| **Family** | A group of repos that share one board |
| **Board** | The GitHub Project of a family |
| **Registry** | `local/projects.yaml`; a project not in it is never touched |
| **Task** | A GitHub issue on a board; only those with the label `agent` enter the loop |
| **Ready task** | An `agent` task in `Ready` whose blockers are all done |
| **Slice** | A task that delivers a complete path that can be verified alone |
| **Hard finding** | A review result that the worker fixes and that stops the merge until it is gone |
| **Judgment call** | A review result that the worker also fixes, but that does not stop a merge once the review rounds are used up |
| **Two-way door** | A change that is easy to undo |
| **One-way door** | A change that is hard to undo; it always waits for the user |
| **Blast radius** | What a merge can break if it is wrong |
| **Cycle** | One wake of the supervisor, from waking to sleep |
| **Live check** | Seeing the result in the deployed software, not only in tests |
