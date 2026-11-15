# To spec

Adapted from Matt Pocock's `to-spec` skill (github.com/mattpocock/skills, MIT). The first half of `/kandu-to-issue`; `prompts/to-ticket.md` follows.

Turn the interview into a spec issue. Do not interview again: write what is settled. A request defined this way always gets a spec, even one that becomes a single task, because the spec is where its decisions are kept once the request closes.

1. Take the interview from this conversation, or from the request's map in `.venv/bin/python -m kandu.intake list` on the brain. If neither holds a finished one, a map with no ❓ or ⏳, say so and point to `/kandu-grill-with-docs`. If the map holds a `✅ Spec issue › ` line, the spec exists already: go on with `prompts/to-ticket.md` for it. Finish when you have the request's id, its project and every ✅ line.
2. Read the code the request touches, read-only, if you have not, using the project's glossary words. Finish when every decision maps to code you have seen.
3. Sketch the seams where the work will be tested: existing ones first, the highest one possible, as few as possible. Tell the user, in plain words, what will be checked and where, as an assumption they can reject. Finish when they have said yes to it or changed it.
4. Write the spec in English with the sections of `prompts/templates/spec.md`, plus `## Glossary`:
   - `## Problem`: the user's problem, from their side.
   - `## Solution`: the result, from their side, then a numbered list of user stories, "As a <actor>, I want <feature>, so that <benefit>", covering every part of the request.
   - `## Decisions`: every ✅ line of the map that is not a glossary word, one line each, keeping its `(ADR)` mark, then the testing seams.
   - `## Out of scope`: what none of its tasks may change, from the ✂️ lines.
   - `## Glossary`: each glossary word, `**Term**: meaning. Avoid: ...`, or `none`.

   No file paths or code. A snippet from a prototype that pins a decision better than words may stay, trimmed and marked as such. Finish when every ✅ line of the map is in the spec.
5. Show the text and create nothing until the user says yes to it. Then create it as step 4 of `prompts/defining.md` says for a spec, and save the map again with one more line, `✅ Spec issue › URL`, as step 5 of `prompts/grilling.md` saves it, so a later run does not make a second one. Finish when it prints the spec's URL and the map holds it.
