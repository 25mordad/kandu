# Writing prompts

Rules for anything an agent reads: `prompts/`, `CLAUDE.md` and the `/kandu-*` commands. The aim is that an agent takes the same path on every run, using as little context as possible.

## Keep always-loaded text small
- Text that is always loaded costs tokens on every turn, whether it is used or not. `CLAUDE.md` holds rules that apply to all work, and pointers to the rest.
- Material only some paths need goes in its own file, behind a pointer.
- A pointer says what the material is and when to read it. If an agent does not reach something it must read, sharpen the pointer first. Copy the material inline only if that fails.
- Delete a line that does not change what the agent does.

## Steps
- Every step ends on a condition the agent can check, such as "every acceptance criterion has a result". A vague end such as "understand the code" invites stopping early.
- Prefer a condition that demands the whole job: "every changed file reviewed", not "review the changes".

## Rules
- A rule that matters is enforced in code, and the prompt only explains it (DESIGN §14).
- Keep a concept's rules and exceptions together, under one heading.
- Say a thing once. A rule repeated in two files will drift.
- Use the words in [GLOSSARY.md](../GLOSSARY.md), the same word every time.
- Prefer a word that already carries the meaning: "slice" instead of a sentence that describes one.

## Public text
Prompts in this repo are public. They hold no person's data: anything specific reaches the agent from `local/` when it runs.
