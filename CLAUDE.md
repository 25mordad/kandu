# Kandu — Claude guide

This repo is public. Read [DESIGN.md](DESIGN.md) for the specification and [TASKS.md](TASKS.md) for the roadmap.

## Keep the public repo free of personal data
- Everything that belongs to one person's setup lives in `local/`, which is gitignored: machines, paths, accounts, projects, notes, secrets.
- Code, commands, docs and examples read from config and use generic examples only. Never hard-code a name, host, path or project of a real setup.
- The same applies to commit messages and to issues and PRs on this repo: they are public too.
- Enable the guard once per clone: `git config core.hooksPath .githooks`. It blocks commits containing a term from `local/private-words.txt`. Never bypass it with `--no-verify`.

## Harness, context, memory
Check every design and every task against these three (DESIGN §14): is the rule enforced in code, does each agent get only the context it needs, and is what matters saved outside the session?

## Python
Kandu runs from its own `.venv`, never the system Python: `.venv/bin/python -m kandu.config`, and the same path in timers and services. New dependencies go in `requirements.txt`.

## Pushing to this repo
- **During development**, before the first board or project is connected: committing and pushing directly to `main` is fine.
- **From the moment the first board is connected:** direct pushes to `main` are forbidden, and no push to this repo happens at all without the owner's explicit permission for that push. Once Kandu can see real projects, every push is a possible leak into a public repo.

## Keep it usable by others
Anyone must be able to clone this and reach the same setup by conversation. A new option needs a generic example in `config.example/` and a question in `/kandu-setup` or `/kandu-add`.
