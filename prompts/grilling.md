# Grilling

Adapted from Matt Pocock's `grilling` skill (github.com/mattpocock/skills, MIT). Called by `/kandu-grill-with-docs`, in any session. Inside Didban it takes the place of step 3 of `prompts/defining.md` for the request; `/kandu-task` is unchanged.

Interview the user until you both understand the request the same way, working its **design tree** one **frontier** at a time. No decision lives only in this session: each is saved with the request as soon as it is made.

1. Do steps 1 and 2 of `prompts/defining.md`: the project, its brief, what already exists or was rejected, the code the request touches and its forbidden areas. Finish when they are done, or have stopped you.
2. If `.venv/bin/python -m kandu.intake list` on the brain shows this request, take its id and its map, show the map as a tree, and go on from it: what it marks ✅ is decided and is not asked again. Otherwise save the request with `.venv/bin/python -m kandu.intake add` on the brain, its text on stdin through a quoted heredoc (`<<'END'`). Finish when you have its id.
3. Ask the whole frontier at once, however many questions it holds, in the user's language, in this form:

   ```
   ❓ **Q1** - **<title>**: <body: why it matters to the user, the choices>

   ➡️ <your recommended answer>

   ---
   ```

   - Number questions on from the last one; no round headings.
   - Word each so "yes" accepts the recommendation.
   - A question whose answer depends on another open question waits for a later turn.
   - Facts are yours to find, never the user's: send a read-only sub-agent with a fresh context, and ask the questions that do not wait on it meanwhile.
   - Ask about outcomes. A technical choice you make yourself is a node of the tree too, shown as an assumption the user can reject.

   Finish when every question of the frontier is asked.
4. When the user stops to clarify or pushes back instead of answering, answer that first. Finish when they have what they asked for; then go on from the tree as it stands.
5. After each answer, show the design tree, each branch with its marks: ✅ decided, ❓ asked now, ⏳ waits, ✂️ out of scope. Mark `(ADR)` on a decision that `prompts/domain-modeling.md` says is one. Then save the whole tree with `.venv/bin/python -m kandu.intake map ID --project NAME` on the brain, on stdin through a quoted heredoc. One line a node, no indent: its mark, then its full path from the top of the tree joined by ` › `, with `(ADR)` at the end when it is one, as `✅ Login › Locked after five failures › For 15 minutes`. Each settled glossary word is a line too, `✅ Glossary › Term: meaning. Avoid: other words`. Finish when it exits 0 and every ✅ of the tree is a line of the map; if it refuses the map, fix the line it names and save again.
6. A confirmed decision that will shape later tasks of the project also goes into its brief, as the last point of step 3 of `prompts/defining.md` says. Finish when each such decision is saved there.
7. Recompute the frontier and go on from step 3. When the map holds no ❓ or ⏳, show a short summary: what the request does, the decisions, your assumptions, what is out of scope, the glossary words, the testing seams and the planned tasks.
   - The testing seams: where the work will be tested and what is checked there, in plain words; existing ones first, the highest one possible, as few as possible.
   - The planned tasks: the slices of step 4 of `prompts/defining.md`, numbered, each with its title, `Blocked by`, `Priority`, `Kind`, `Size` and `Deadline`, and whether the user does it by hand or it must really call a service with the project's `task_tokens`. The task that step 2 of `prompts/to-ticket.md` adds is one of them when it applies.

   Save both in the map as step 5 says, as `✅ Seams › What is checked › Where` and `✅ Tasks › 1. Title › Blocked by none › Priority Medium › Kind Feature › Size Small › Deadline none › By hand no › Tokens no`. When the user changes anything in the summary, change the tree, save it and show the summary again. Finish when the user has said yes to the summary and the map holds its seams and tasks. Then say that `/kandu-to-issue` turns it into the spec and these tasks without asking anything. Create nothing yourself.
