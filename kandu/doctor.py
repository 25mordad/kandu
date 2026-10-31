"""Checks that a Kandu install can work (DESIGN §1C). Uses no tokens and changes nothing."""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .boards import missing_columns, missing_fields
from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .dashboard import TOKEN_ENV
from .github import GitHub, GitHubError
from .runner import Runner, RunnerError, repo_path
from .usage import UsageError, read_usage

REPO_DIR = Path(__file__).resolve().parent.parent
REQUIRED_TOOLS = ("claude", "gh", "git")
SESSION_TOOLS = ("tmux", "systemd-run")
OK, WARN, FAIL = "ok", "warn", "FAIL"


def check_tools(which=shutil.which):
    results = [(OK if which(t) else FAIL, f"tool {t}") for t in REQUIRED_TOOLS]
    results += [(OK if which(t) else WARN, f"tool {t}: the dashboard opens sessions with it") for t in SESSION_TOOLS]
    return results


def check_gh_scope(auth_text):
    if "'project'" in auth_text:
        return OK, "gh has the project scope"
    return FAIL, "gh lacks the project scope; run: gh auth refresh -s project"


def check_guard(hooks_path, local_dir):
    results = [(OK if hooks_path == ".githooks" else FAIL, "private guard: core.hooksPath is .githooks")]
    has_words = (Path(local_dir) / "private-words.txt").exists()
    results.append((OK if has_words else WARN, "private guard: local/private-words.txt exists"))
    return results


def _env_is_set(name, local_dir):
    env_file = Path(local_dir) / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    return bool(os.environ.get(name)) or any(re.fullmatch(rf"{name}=\S+", line) for line in lines)


def check_dashboard_token(local_dir):
    if _env_is_set(TOKEN_ENV, local_dir):
        return OK, f"{TOKEN_ENV} is set"
    return WARN, f"{TOKEN_ENV} is not set; the dashboard's controls are refused"


def check_notify(notify, local_dir):
    if notify.adapter == "none":
        return OK, "notifications are off"
    missing = [name for name in (notify.telegram.bot_token_env, notify.telegram.chat_id_env)
               if not _env_is_set(name, local_dir)]
    if missing:
        return WARN, f"{', '.join(missing)} not set; notifications cannot be sent"
    return OK, "notifications: Telegram is set"


def check_usage(auth, reader=read_usage):
    if auth == "api_key":
        return OK, "usage: api_key has no percent limits"
    try:
        usage = reader()
    except UsageError as e:
        return FAIL, f"usage cannot be read: {e}"
    return OK, f"usage: {usage.session}% of the 5-hour window, {usage.week}% of the week"


def check_machine(machine, runner):
    if runner.reachable(machine.name):
        return OK, f"machine {machine.name} is reachable"
    level = FAIL if machine.always_on else WARN
    return level, f"machine {machine.name} is not reachable"


def check_project(project, runner, github):
    results = []
    host = runner.hosts[project.machine]
    test_tool = project.test.split()[0]
    try:
        for repo in project.repos:
            path = host.absolute(repo_path(project, repo))
            if host.succeeds(["test", "-e", ".git"], cwd=path):
                results.append((OK, f"{project.name}: copy of {repo}"))
            elif project.run == "clone":
                results.append((OK, f"{project.name}: {repo} will be cloned on first use"))
            else:
                results.append((FAIL, f"{project.name}: no git copy of {repo} on {project.machine}"))
        present = host.succeeds(["sh", "-c", 'command -v "$1"', "sh", test_tool])
        results.append((OK if present else WARN, f"{project.name}: test tool {test_tool}"))
    except RunnerError:
        results.append((WARN, f"{project.name}: machine {project.machine} did not answer"))
    if project.board is None:
        results.append((WARN, f"{project.name}: no board"))
        return results
    try:
        missing = missing_columns(project.board, github.board_columns(project.board))
        missing += missing_fields(github.board_fields(project.board))
    except GitHubError:
        results.append((FAIL, f"{project.name}: board cannot be read"))
        return results
    level = FAIL if missing else OK
    results.append((level, f"{project.name}: board columns" + (f"; missing: {', '.join(missing)}" if missing else "")))
    return results


def run_checks(config, local_dir, runner, github):
    settings = config.settings
    results = check_tools()
    auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    results.append(check_gh_scope(auth.stdout + auth.stderr))
    hooks = subprocess.run(["git", "config", "core.hooksPath"], capture_output=True, text=True, cwd=REPO_DIR)
    results += check_guard(hooks.stdout.strip(), local_dir)
    results.append(check_usage(settings.claude.auth))
    results.append(check_dashboard_token(local_dir))
    results.append(check_notify(settings.notify, local_dir))
    results += [check_machine(m, runner) for m in settings.machines]
    for project in config.projects:
        if runner.reachable(project.machine):
            results += check_project(project, runner, github)
    return results


def main():
    os.environ["PATH"] = f"{Path.home() / '.local' / 'bin'}:{os.environ['PATH']}"  # As the timer and the runner do.
    parser = argparse.ArgumentParser(description="Check a Kandu install.")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"{FAIL} config: {len(e.problems)} problem(s); run: python -m kandu.config")
        return 1
    results = run_checks(config, args.local, Runner(config), GitHub())
    for level, message in results:
        print(f"{level:4} {message}")
    return 1 if any(level == FAIL for level, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
