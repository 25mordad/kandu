# Domain modeling

Adapted from Matt Pocock's `domain-modeling` skill (github.com/mattpocock/skills, MIT). Used throughout `prompts/grilling.md`.

Sharpen the project's language while you interview. Write no file: what you settle is saved with the request's map (`prompts/grilling.md` step 5), and `/kandu-to-issue` carries it into the spec and the repo.

- **The project's words:** read its `GLOSSARY.md`, or `GLOSSARY-MAP.md` and the files it lists, and `docs/adr/`, read-only on the machine the registry names, and the glossary and decisions in `local/memory/<project>.md` on the brain. Without any, take the words of the brief.
- **Challenge a clash.** When the user uses a word against its meaning there, say so at once: "The glossary says X means A; you seem to mean B. Which is it?"
- **Sharpen a fuzzy word.** For a vague or overloaded word, propose one precise term.
- **Test with scenarios.** Invent concrete edge cases that force the boundary between two concepts.
- **Check against the code.** When the user says how something works and the code disagrees, show the clash.
- **A glossary word** is a term of this project, never a general programming word, said in English in one or two sentences on what it is, with the words it replaces under `Avoid`.
- **An ADR** is a decision that is hard to reverse, would surprise a reader without context, and was a real choice between alternatives. All three must hold; any other decision is kept in the spec only.

Finish when every word and decision the user confirmed is in the tree.
