# Glossary

One word for each concept. Use these words in code, docs, prompts and issues. A project keeps its own glossary in its memory (DESIGN §14).

| Term | Meaning |
|---|---|
| **Brain** | The always-on machine where the supervisor runs |
| **Supervisor** | The code loop that picks tasks, starts agents and enforces the rules; it is not an LLM session and writes no code |
| **Queen** | The one LLM that runs the cycle for all projects: she releases tasks from `Backlog` and judges what stopped; code checks each decision, and she cannot merge |
| **Worker** | An agent that does one task in its own `git worktree` |
| **Technical debt** | The column for tasks the user parked without refining them; no agent reads it, and Didban brings them up |
| **Multi-user project** | A project whose board several people share, each with a Kandu of their own (`multi_user`); an install works only the tasks assigned to its own GitHub account |
| **Project model** | The model one role runs on for one project (`models` in its entry); a role it does not name runs on the global `claude.models`, and the queen's is global only |
| **Run temporary folder** | A machine's option, off by default (`run_tmp`): each agent run, test and setup there gets a fresh folder inside `run_tmp_dir` on the machine's own disk as its temporary directory, removed when the run ends; a deploy gets none |
| **Reference copy** | A read-only copy of the paths a task's `Reference` names in another registered project's repo, in a folder of the run's own on the worker's machine, removed when the run ends. Off by default (`supervisor.reference_copy`); never from a private repo into a run on a public one. |
| **Splitter** | A read-only agent that sizes every task in `Backlog` against the scale and proposes how to split a `Large` one |
| **Base branch** | The branch of a project's repo that Kandu works on and merges into; by default the repo's default branch |
| **Red main** | A project's state when its tests fail twice on one commit of its default branch (`watch_main`, off by default): its merges wait, no task starts but the one Kandu opens to fix it, and the user is told once until `main` passes |
| **Queen answers one-way merges** | A project's switch, off by default: on, the queen's yes to a merge answers its one-way door and the review's questions for the user; off, those wait for the user's yes |
| **Split approval** | Who accepts a project's split proposals, the user or the queen; the user by default, set in the registry or the dashboard's settings section |
| **Held task** | A task in `Backlog` that the splitter found must change a forbidden area of its project; no agent works on it until the user releases it in Didban |
| **Group** | The slices of one split task: chained by `Blocked by`, each with its own PR; built side by side, merged one after another |
| **Reviewer** | A fresh agent that reviews a PR, knowing nothing of how it was built |
| **Didban** | The control role, run from the machine the user sits at |
| **Free flight** | A session the user opens on a task from the dashboard, with full access; Kandu's agents keep running beside it |
| **Zanbourdar** | Persian for "beekeeper": a session the user opens beside Didban from the dashboard, with full access and tied to no task; it may change Kandu itself |
| **Kanduban** | A session the user opens on one project from the dashboard, with full access, on the project's own machine in a worktree of its own; it works only in that project and brings its change in through a PR |
| **Upgrade notice** | The dashboard's notice at the top of every look, «Kandu was upgraded to X.Y.Z», with the changelog entry of each version the user has not marked as read, until «Mark as read» |
| **Upstream check** | The dashboard's count, off by default, of how far the install's `main` is behind its public upstream and ahead of it with its own changes; it only fetches and counts, and tells the user on Telegram, as the `upstream` kind of notification, when the counts change or it starts failing |
| **Version badge** | The header's mark of the install's version, in every look, and whether it is the newest release of the public upstream, read once a day; it never updates the install |
| **Handoff** | A job Didban hands to Zanbourdar, or to a project's Kanduban, on the user's yes, typed into that session by `kandu.handoff`, and the short report it sends back to Didban |
| **Brief** | One short private file per project, `local/brief/<project>.md`, that Didban reads when the user names the project; it changes only through `kandu.brief` |
| **Repo note** | `KANDU.md` in a project's repo, rendered by `kandu.note`: it says Kandu changes the repo, where its tasks are and how a breakage goes back to Kandu |
| **Report file** | A file of the project's own, listed under `reports` in the registry, that Didban reads with the brief and never changes |
| **Bazras** | The inspector bee: a daily, read-only look at a live project's health; its findings go in a private report, never into a task by itself |
| **Finding** (Bazras) | Something Bazras's code found wrong: a reverted Kandu commit, rework, a failed unit or cron line, new error lines in a log, failing tests or a failing check |
| **Request** | What the user wants done, in their words, before it is a task; Didban keeps it until its tasks exist |
| **Spec** | The issue that holds a request of several tasks, or any request defined with `/kandu-to-issue`: its Problem, Solution, Decisions and Out of scope. It is never work: no `agent` label, no card, never sized; each of its tasks names it and takes its Out of scope, and it closes with the last of them |
| **Decision map** | The short list Didban shows after each round of refining a task: ✅ decided, ❓ asked now, ⏳ waits for another answer, ✂️ out of scope; refining ends when nothing is ❓ or ⏳. Didban saves it with the request after each round |
| **Design tree** | The decisions of a request, each nested under the one it hangs off, as `/kandu-grill-with-docs` shows it; its flat copy is saved as the decision map |
| **Frontier** | The decisions in a design tree whose parents are all settled: what can be asked now. `/kandu-grill-with-docs` asks the whole frontier at once |
| **Family** | A group of repos that share one board |
| **Board** | The GitHub Project of a family |
| **Registry** | `local/projects.yaml`; a project not in it is never touched |
| **Task** | A GitHub issue on a board; only those with the label `agent` enter the loop |
| **Token task** | An `agent` task labelled `uses-tokens`; its worker gets the project's `task_tokens` |
| **Version** | Kandu's version number, such as `1.0.0`, held in the repo's `VERSION` file; the install reads it and reports it in `GET /state` |
| **Cut a release** | The dashboard's action, in every look, and Didban's flow that show the next release's proposal, its version and changelog entry, and open the release PR only on the user's yes |
| **Release** | A new version of Kandu: a PR on the private copy that `kandu.release` opens on the user's yes, with the next `VERSION` and its changelog entry |
| **Changelog** | `CHANGELOG.md`, one entry for each release, newest first, its changes grouped as Breaking, Features, Fixes and Other |
| **Breaking mark** | The `breaking` label, given to a task when it is defined (`kandu.tasks --breaking`) and carried to its PR, so a merged PR still says it was a breaking change |
| **Publish** | The last step of Kandu's own deploy, `kandu.publish`: once a release is live and `main` is on the public upstream, it tags it `vX.Y.Z` there and creates its GitHub release with the changelog entry, after the private guard checked both |
| **Ready task** | An `agent` task in `Ready` whose blockers are all done |
| **Slice** | A task that delivers a complete path that can be verified alone |
| **Blocker** | A review finding that the worker fixes and that stops the merge until it is gone |
| **Blocking task** | A task named under another task's `Blocked by`; its merge must come first. Avoid: blocker (that is a review finding), dependency |
| **Note** | A review finding that does not stop a merge. The splitter sizes it: a `Small`, `two-way` one is fixed in the same PR, and the queen parks any other in `Technical debt` |
| **Second review** | The last review of a PR: it checks only the items the first review left for the worker, and reports nothing new |
| **Open question** | Something a review met that the task never settled; it waits for the user's answer in Didban |
| **Calibration** | Comparing Kandu's review and merge decisions with what the user would have done |
| **Two-way door** | A change that is easy to undo |
| **One-way door** | A change that is hard to undo; it always waits for the user |
| **Blast radius** | What a merge can break if it is wrong |
| **Cycle** | One wake of the supervisor, from waking to sleep |
| **Stopping** | Kandu after Stop while runs still finish their step: nothing new starts, and once the last run ends it is stopped |
| **Live check** | Seeing the result in the deployed software, not only in tests |
| **Check** | A read-only run the queen sends to find one thing out about a task; the run is the checker |
| **Drop ask** | The queen's question to the user to drop one task from another's `Blocked by`, with her reason; only the user's yes on the dashboard takes it out of the issue. Off until the user turns it on |
| **Loop guard** | The per-task limit on each of the queen's steps; past it the task stops for the user |
| **Look** | One of the dashboard's appearances each browser chooses between, Classic, Classic new or Kandu Clean. Avoid: template, theme, skin |
| **Classic look** | The dashboard as this install had it before Kandu Clean. Avoid: old look, original look, cards look |
| **Classic new look** | Classic made compact: one header row, the numbers as a strip of chips, an explanation on every part. Avoid: new classic, classic 2 |
| **Token gauge** | In the Classic new look, the small gauge under a project's name on the meadow: its tokens today against its daily token limit, red once it reaches the limit |
| **Kandu Clean look** | The dashboard built to the Kandu Clean design. Avoid: new look, redesign |
| **Calm** | A switch in every look's header, each browser's own choice: nothing moves, the live stream closes and the state is read once a minute. Avoid: quiet mode, low-power mode |
| **View address** | A dashboard view's own URL, the same in every look: its tab, then the task's sheet, project's settings pop-up or run's log open over it, as `/tasks/task/<owner>/<repo>/<number>`. Avoid: route, deep link |
