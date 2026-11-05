# Kandu on Windows

Kandu's brain can run on Windows 10 or 11, with no WSL and no admin rights. This page lists only what differs from [the guide](GUIDE.md). Everything else is the same.

## What is different

| On Linux | On Windows |
|---|---|
| `systemd` timers wake the supervisor and sync | Task Scheduler tasks in the folder `\Kandu\` |
| The dashboard is a user service | A task that starts at logon and comes back if it fails |
| The journal keeps the output | `logs\supervisor.log`, `logs\sync.log`, `logs\dashboard.log` |
| A session from the dashboard runs in `tmux` | It opens in a console window of its own on the brain |
| `.venv/bin/python` | `.venv\Scripts\python.exe` |
| `bin/didban` | `bin\didban.cmd` |

The tasks run as you, while you are logged on. If you log off or the PC sleeps, Kandu waits. It picks the work up at the next wake.

## What you need

- [Git for Windows](https://git-scm.com/download/win). Kandu runs its shell lines, the private guard and the `gh` meter with the `sh` that comes with it.
- Python 3.11 or newer, from python.org.
- [Claude Code](https://claude.com/claude-code) and the [GitHub CLI](https://cli.github.com/), both logged in, as in the guide.

Check: `git --version`, `python --version`, `claude --version` and `gh auth status` all answer in PowerShell.

## Install

In PowerShell, in the folder where Kandu should live:

```
git clone https://github.com/25mordad/kandu.git kandu
cd kandu
git config core.hooksPath .githooks
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The repo's `.gitattributes` keeps Unix line endings in every script, also when Git is set to `core.autocrlf=true`. If you cloned before that file existed, check the scripts out again: `kandu.doctor` fails on a script with Windows line endings.

Then follow the guide from step 3. Write each command with the Windows path of the venv:

```
.venv\Scripts\python.exe -m kandu.config
.venv\Scripts\python.exe -m kandu.doctor
.venv\Scripts\python.exe -m kandu.timer --dashboard
.venv\Scripts\python.exe -m kandu.timer --enable
```

`kandu.timer` with no flag prints the three tasks as XML and changes nothing. `--install` writes them switched off. `--dashboard` switches on the dashboard alone. `--enable` switches on all three. To see them, open Task Scheduler and look in the folder `Kandu`.

## Projects on Windows

- A project's `test` and `deploy` lines run in Git's `sh`, not in PowerShell. Write them as on Linux, for example `npm test`.
- Write paths in config with forward slashes or `~`, for example `~/projects/site`.

## Didban

On the machine you sit at, if it is Windows, put `bin` on your `PATH` or pin a shortcut to `bin\didban.cmd`. It opens Claude Code in the Didban role, as `bin/didban` does on Linux.

## Sessions from the dashboard

A session you open from the dashboard starts in a new console window on the brain. Windows gives Kandu no way to read that window. So the page links to [claude.ai/code](https://claude.ai/code), where the session is listed by its name: `Kandu` for Didban, `Kandu <repo>#<number>` for a task. If Claude asks whether you trust the folder, answer in that window.

## Not supported yet

- A Windows brain reached over SSH from Didban. Didban runs `.venv/bin/python` on a remote brain, so a remote brain must be Linux.
- Isolation by a separate Windows user.
