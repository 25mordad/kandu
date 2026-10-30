# Kandu — Claude guide

This repo is public. Read [DESIGN.md](DESIGN.md) for the specification and [TASKS.md](TASKS.md) for the roadmap.

## Keep the public repo free of personal data
- Everything that belongs to one person's setup lives in `local/`, which is gitignored: machines, paths, accounts, projects, notes, secrets.
- Code, commands, docs and examples read from config and use generic examples only. Never hard-code a name, host, path or project of a real setup.
- The same applies to commit messages and to issues and PRs on this repo: they are public too.
- Enable the guard once per clone: `git config core.hooksPath .githooks`. It blocks commits containing a term from `local/private-words.txt`. Never bypass it with `--no-verify`.

## Keep it usable by others
Anyone must be able to clone this and reach the same setup by conversation. A new option needs a generic example in `config.example/` and a question in `/kandu-setup` or `/kandu-add`.
