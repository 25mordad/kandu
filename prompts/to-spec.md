# To spec

Adapted from Matt Pocock's `to-spec` skill (github.com/mattpocock/skills, MIT). The first half of `/kandu-to-issue`; `prompts/to-ticket.md` follows.

Turn the interview into a spec issue. Do not interview again: write what is settled. The user's yes to the interview's summary is the yes to the spec and every task: ask the user nothing and wait for no yes, here and in `prompts/to-ticket.md`, even where a step of `prompts/defining.md` they follow says to show a text or ask first. A request defined this way always gets a spec, even one that becomes a single task, because the spec is where its decisions are kept once the request closes.

1. Take the interview from this conversation, or from the request's map in `.venv/bin/python -m kandu.intake list` on the brain. If neither holds a finished one, a map with no ❓ or ⏳, say so, stop and point to `/kandu-grill-with-docs`; nothing else ends this command early. If the map holds a `✅ Spec issue › ` line, the spec exists already: do step 3, then go on with `prompts/to-ticket.md` for it. Finish when you have the request's id, its project and every ✅ line.
2. Read the code the request touches, read-only, if you have not, using the project's glossary words. Finish when every decision maps to code you have seen.
3. Take the testing seams and the planned tasks from the interview's summary, or from the map's `✅ Seams` and `✅ Tasks` lines. If it settled no seams, sketch them yourself: existing ones first, the highest one possible, as few as possible. If it planned no tasks, draft them as slices, split as step 4 of `prompts/defining.md` says and shaped as "What makes a task good" in DESIGN §2 says. A field it left open takes its default:
   - `Blocked by`: none, unless the task cannot work or merge correctly without another, by the rule of step 4 of `prompts/defining.md`.
   - `Priority`: `Medium`.
   - `Kind`: `Bug` for a task that mends broken behavior, `Feature` otherwise.
   - `Size`: by the scale of step 4 of `prompts/defining.md`, from its acceptance criteria and changed files; a `Large` task is split.
   - `Deadline`: none.
   - by hand: no.
   - tokens: no, since building a feature takes none.

   Finish when every task has every field, every user story of the request belongs to a task, and every choice you made yourself is noted.
4. Write the spec in English with the sections of `prompts/templates/spec.md`, plus `## Assumptions` and `## Glossary`:
   - `## Problem`: the user's problem, from their side.
   - `## Solution`: the result, from their side, then a numbered list of user stories, "As a <actor>, I want <feature>, so that <benefit>", covering every part of the request.
   - `## Decisions`: every ✅ line of the map that is not a glossary word, a seam or a task, one line each, keeping its `(ADR)` mark, then the testing seams.
   - `## Out of scope`: what none of its tasks may change, from the ✂️ lines.
   - `## Assumptions`: each choice made by a default in step 3, as the task, the field and the value, and the seams if you sketched them; or `none`.
   - `## Glossary`: each glossary word, `**Term**: meaning. Avoid: ...`, or `none`.

   No file paths or code, except in `## Reference` (add it between `## Solution` and `## Decisions`, as the template has it): the source the user asked to copy or match, as step 5 of `prompts/defining.md` says, or `none`. A snippet from a prototype that pins a decision better than words may stay, trimmed and marked as such. Finish when every ✅ line of the map is in the spec.
5. Create it with the command of step 4 of `prompts/defining.md` for a spec, adding `--request ID` for the request of step 1, without showing it first, and save the map again with one more line, `✅ Spec issue › URL`, as step 5 of `prompts/grilling.md` saves it, so a later run does not make a second one. Finish when it prints the spec's URL and the map holds it.
