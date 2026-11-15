# To ticket

Adapted from Matt Pocock's `to-tickets` skill (github.com/mattpocock/skills, MIT). The second half of `/kandu-to-issue`, after `prompts/to-spec.md` printed the spec's URL. What Matt calls a ticket is a task here.

1. Draft the tasks as slices, split as step 4 of `prompts/defining.md` says and shaped as "What makes a task good" in DESIGN §2 says. Finish when every user story of the spec belongs to a task.
2. If the spec's `## Glossary` or an `(ADR)` decision holds anything, add one task: "Record the glossary and decisions of spec #N". It adds each word to the project's `GLOSSARY.md`, in the form the file already uses, and each `(ADR)` decision to `docs/adr/` as `NNNN-slug.md`: one short paragraph of context, decision and why. One acceptance criterion names all the words, and one each ADR. It is a `Chore` of `Low` priority, blocked by nothing, and these are not asked; its `Size` follows the scale of step 4 of `prompts/defining.md`. Finish when it is drafted, or the spec holds nothing to record.
3. Show the breakdown as a numbered list: title, `Blocked by`, and the behavior it makes work. Then ask, in the form of `prompts/grilling.md`:
   - Is the granularity right, or too coarse or too fine?
   - Does each `Blocked by` keep to the rule of step 4 of `prompts/defining.md`?
   - Should any be merged or split?
   - For each other task, every field step 4 of `prompts/defining.md` names that the interview did not settle, and whether the user does it by hand or it must really call a service with the project's `task_tokens`.

   Finish when the user has said yes to the breakdown and every field.
4. Write each task as step 5 of `prompts/defining.md` says, from `prompts/templates/issue.md`, leaving `Out of scope` out: the spec's goes in. Show the final text of all of them, as step 6 says. Finish when the user has said yes to the text of every task.
5. Create them in order, each before any task that names it under `Blocked by`, so every name is a real issue: run the command of step 7 of `prompts/defining.md`, with `--spec #N`. Finish when every task has printed its URL.
6. Run `.venv/bin/python -m kandu.intake close ID` on the brain for the request. Finish when it exits 0; then tell the user the spec and its tasks.
