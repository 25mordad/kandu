# Kandu — Claude guide

This repo is public. Read [DESIGN.md](DESIGN.md) for the specification and [TASKS.md](TASKS.md) for the roadmap. Use the terms in [GLOSSARY.md](GLOSSARY.md). Before writing or changing a prompt, this file or a `/kandu-*` command, read [docs/WRITING-PROMPTS.md](docs/WRITING-PROMPTS.md).

## Keep the public repo free of personal data
- Everything that belongs to one person's setup lives in `local/`, which is gitignored: machines, paths, accounts, projects, notes, secrets.
- Code, commands, docs and examples read from config and use generic examples only. Never hard-code a name, host, path or project of a real setup.
- The same applies to commit messages and to issues and PRs on this repo: they are public too.
- Enable the guard once per clone: `git config core.hooksPath .githooks`. It blocks commits and pushes containing a term from `local/private-words.txt`, in content, file names and commit messages. Never bypass it with `--no-verify`.

## Harness, context, memory
Check every design and every task against these three (DESIGN §14): is the rule enforced in code, does each agent get only the context it needs, and is what matters saved outside the session?

## Python
Kandu runs from its own `.venv`, never the system Python: `.venv/bin/python -m kandu.config`, and the same path in timers and services. New dependencies go in `requirements.txt`.

## Pushing to this repo
- **During development**, before the first board or project is connected: committing and pushing directly to `main` is fine.
- **From the moment the first board is connected:** direct pushes to `main` are forbidden. Every change goes through a PR on the install's private copy. Once Kandu can see real projects, every push is a possible leak into a public repo.
- **The public repo** receives only the private copy's `main`, pushed after the change is live in the install, so the owner sees it first. An install that runs Kandu as its own project lets its deploy do this push (DESIGN §12); any other push to the public repo needs the owner's explicit permission for that push.
- The private guard checks every pushed commit against the private terms, including those code takes from `local/` (DESIGN §12). Never push past it, and never weaken it without the owner's yes.

## Keep it usable by others
Anyone must be able to clone this and reach the same setup by conversation. A new option needs a generic example in `config.example/` and a question in `/kandu-setup` or `/kandu-add`.
