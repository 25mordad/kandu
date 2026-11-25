"""Adds a machine or a project from a few facts: the machine, the path on it, the repo and the run location (DESIGN §1B).

Everything else is read from the copy on that machine and proposed with safe defaults, so the
dashboard's form and `python -m kandu.add` need no conversation; `/kandu-add` stays for the details.
Nothing is written until the user sends the proposal back. Both config files are changed by
appending text, so their comments stay, and a change is validated on a copy before it reaches `local/`.

    .venv/bin/python -m kandu.add machine --name laptop --ssh laptop [--always-on]
    .venv/bin/python -m kandu.add project --machine laptop --path ~/code/website --dry-run
    .venv/bin/python -m kandu.add project --machine laptop --path ~/code/website --set run=in-place
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from . import memory, tasks
from .config import BOARD_COLUMNS, DEFAULT_LOCAL_DIR, RUN_MODES, SSH_REACH, Board, ConfigError, Machine, load_config
from .github import GitHubError
from .runner import Host, Runner, RunnerError

MACHINE_NAME = re.compile(r"[A-Za-z0-9][\w.-]*")
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
REPO = re.compile(r"[\w.-]+/[\w.-]+")
# `run` is not here: where a project runs is always the user's choice, never inferred (DESIGN §1B).
# `auto_merge` is off, as in the template: a project merges by itself only once the user turns it on.
DEFAULTS = {"priority": 1, "max_parallel_workers": 1, "daily_token_limit": 1000000, "auto_merge": False}
FLAGS = ("auto_merge", "deploy_task", "test_task")

# Lists what is there, and changes nothing. `$1` is the path, already expanded on that machine.
LOOK = r'''
cd "$1" 2>/dev/null || { echo "missing"; exit 0; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 && echo "git"
echo "remote $(git remote get-url origin 2>/dev/null)"
for f in scripts/checks.sh package.json package-lock.json pyproject.toml pytest.ini setup.py requirements.txt Makefile tests \
         deploy/deploy.sh scripts/deploy.sh deploy.sh .env AGENTS.md CLAUDE.md; do
  [ -e "$f" ] && echo "file $f"
done
grep -q '"test"[[:space:]]*:' package.json 2>/dev/null && echo "npm-test"
grep -q '"dev"[[:space:]]*:' package.json 2>/dev/null && echo "npm-dev"
grep -q '^test:' Makefile 2>/dev/null && echo "make-test"
for py in python3 python; do
  "$py" -c "import sys" >/dev/null 2>&1 || continue
  echo "py $py"
  "$py" -c "import pytest" >/dev/null 2>&1 && echo "pytest $py"
done
[ -n "$(find tests -name 'test_*.py' 2>/dev/null | head -n 1)" ] && echo "pytests"
d=$(sed -n 's/^description *= *"\(.*\)"/\1/p' pyproject.toml 2>/dev/null | head -n 1)
[ -n "$d" ] || d=$(sed -n 's/^ *"description" *: *"\(.*\)",* *$/\1/p' package.json 2>/dev/null | head -n 1)
[ -n "$d" ] || d=$(sed -n 's/^> *//p' PROJECT.md README.md 2>/dev/null | head -n 1)
[ -n "$d" ] && echo "desc $d"
[ -f .env.example ] && echo "envnames $(sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' .env.example | tr '\n' ' ')"
for dir in data migrations secrets private; do [ -d "$dir" ] && echo "dir $dir"; done
exit 0
'''

DEPLOY_SCRIPT = "deploy/deploy.sh"
# Runs in a fresh checkout of the merged branch, so it starts working once the script lands.
DEPLOY_PENDING = (f'if [ -f {DEPLOY_SCRIPT} ]; then bash {DEPLOY_SCRIPT}; '
                  f'else echo "no {DEPLOY_SCRIPT} yet: nothing deployed"; fi')
CHECKS_SCRIPT = "scripts/checks.sh"
COMPILE = "-m compileall -q -x '(^|/)[.]venv' ."   # until there are tests: the code at least compiles
TEST_TASK_TITLE = "Add a checks script that runs the tests from a fresh checkout"
TEST_TASK_BODY = f"""## Goal
`bash {CHECKS_SCRIPT}` runs this project's tests from a fresh checkout, with nothing set up beforehand.

## Current behavior
There is no checks script. Kandu only checks that the code compiles, because the tests cannot run in a fresh checkout.

## Acceptance criteria
- [ ] `{CHECKS_SCRIPT}` exists and exits non-zero when a test fails.
- [ ] It sets up what the tests need inside the checkout when it is missing (for Python, a `.venv` with the dependencies and pytest), and reuses it when it is there.
- [ ] It works on the machine this project runs on, with its shell and its Python or Node command.
- [ ] With no tests yet it passes, and says that there are none.
- [ ] At least one real test of existing behavior runs in it.

## How to test
Clone the repo into a new folder and run `bash {CHECKS_SCRIPT}` there.

## Out of scope
Changing the app's behavior. Secrets: tests use fakes.

## Blocked by
none
"""
DEPLOY_TASK_TITLE = "Add a deploy script"
DEPLOY_TASK_BODY = f"""## Goal
After a merge, `{DEPLOY_SCRIPT}` brings the merged code into the live software of this project and restarts it.

## Current behavior
The project has no deploy script, so a merged change does not reach the live software.

## Acceptance criteria
- [ ] `{DEPLOY_SCRIPT}` exists, runs from the root of a checkout of the default branch, and exits non-zero on any failure.
- [ ] It updates the live copy with Kandu's `pull.py` (never a plain `git pull`), so a conflict leaves the live copy as it was.
- [ ] It installs or builds only what the project needs, then restarts the live app.
- [ ] It ends with a check that the live app is up, and fails if it is not.
- [ ] Running it twice in a row changes nothing the second time.

## How to test
Run `bash {DEPLOY_SCRIPT}` from a checkout and see the live app restart and answer.

## Out of scope
Anything in the app itself. If where or how the live software runs is not clear from the repo, ask: do not guess.

## Blocked by
none
"""


class AddError(Exception):
    pass


def _validate_copy(local_dir, kandu_text=None, projects_text=None):
    """Loads the config as it would be with these texts, in a folder of its own."""
    local_dir = Path(local_dir)
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (("kandu.yaml", kandu_text), ("projects.yaml", projects_text)):
            if text is None:
                shutil.copy(local_dir / name, Path(tmp) / name)
            else:
                (Path(tmp) / name).write_text(text, encoding="utf-8")
        try:
            return load_config(tmp)
        except ConfigError as e:
            raise AddError(e.problems[0]) from None


def _write(path, text):
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


def machine_text(text, name, ssh_alias, always_on, run_tmp_dir=None):
    """`kandu.yaml` with one more machine at the end of its `machines:` list; with `run_tmp_dir`, its run temporary
    folder is on there."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"machines:\s*(#.*)?$", line)), None)
    if start is None:
        raise AddError("kandu.yaml has no machines: list")
    end, indent = start, "  "
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace() and not line.startswith("-"):
            break
        if line.strip():
            end = i
            item = re.match(r"(\s*)- ", line)
            if item:
                indent = item.group(1)
    entry = (f"{indent}- name: {name}\n{indent}  reach: ssh {ssh_alias}\n"
             f"{indent}  always_on: {'true' if always_on else 'false'}\n")
    if run_tmp_dir:
        entry += f"{indent}  run_tmp: true\n{indent}  run_tmp_dir: {json.dumps(run_tmp_dir)}\n"
    if not lines[end].endswith("\n"):
        lines[end] += "\n"
    return "".join(lines[:end + 1]) + entry + "".join(lines[end + 1:])


def add_machine(name, ssh_alias, always_on=False, local_dir=DEFAULT_LOCAL_DIR, probe=None, run_tmp_dir=None):
    """Adds a machine reached by an SSH alias on the brain. Answers whether it answered.

    With `run_tmp_dir`, each run there gets its own run temporary folder inside it; without, the option stays off.
    """
    name, ssh_alias = name.strip(), ssh_alias.strip()
    if not MACHINE_NAME.fullmatch(name):
        raise AddError("a machine name is letters, digits, '.', '_' or '-'")
    if not SSH_REACH.fullmatch(f"ssh {ssh_alias}"):
        raise AddError("the SSH alias is one word from ~/.ssh/config on the brain, for example 'laptop'")
    path = Path(local_dir) / "kandu.yaml"
    config = load_config(local_dir)
    if any(m.name == name for m in config.settings.machines):
        raise AddError(f"there is already a machine named {name!r}")
    run_tmp_dir = (run_tmp_dir or "").strip() or None
    text = machine_text(path.read_text(encoding="utf-8"), name, ssh_alias, always_on, run_tmp_dir)
    _validate_copy(local_dir, kandu_text=text)
    _write(path, text)
    machine = Machine(name=name, reach=f"ssh {ssh_alias}", always_on=always_on, run_tmp=bool(run_tmp_dir),
                      run_tmp_dir=run_tmp_dir)
    return {"machine": name, "reachable": (probe or _answers)(machine)}


def _answers(machine):
    try:
        return Host(machine).run(["true"], timeout=20).returncode == 0
    except RunnerError:
        return False


def _guess(path, facts, files, lines):
    """What the copy says about itself, as fields of a project entry, and warnings where it cannot say."""
    found = {}
    warnings = found["warnings"] = []
    pythons = [line[3:] for line in lines if line.startswith("py ")]
    if CHECKS_SCRIPT in files:
        found["test"] = f"bash {CHECKS_SCRIPT}"
    elif "npm-test" in facts:
        found["test"] = "npm test"
    elif files & {"pyproject.toml", "pytest.ini", "setup.py", "requirements.txt", "tests"}:
        # A worktree is a fresh checkout with no .venv, so only a Python of the machine itself answers there.
        python = pythons[0] if pythons else None
        if python is None:
            warnings.append("no Python answers on that machine: give the test command yourself")
        else:
            found["test"] = f"if [ -f {CHECKS_SCRIPT} ]; then bash {CHECKS_SCRIPT}; else {python} {COMPILE}; fi"
            found["test_task"] = True
            if "pytests" not in facts:
                why = "there are no tests yet"
            elif f"pytest {python}" not in lines:
                why = f"pytest is not installed for {python}"
            else:
                why = "the tests need the project's own setup, which a fresh checkout does not have"
            warnings.append(f"{why}: until {CHECKS_SCRIPT} exists, the test only checks that the code compiles. "
                            "A first task can write that script.")
    elif "make-test" in facts:
        found["test"] = "make test"
    if "package-lock.json" in files:  # a fresh checkout has no node_modules: Kandu installs them before its tests
        found["setup"], found["setup_inputs"] = "npm ci", "package-lock.json"
    elif "package.json" in files:
        found["setup"], found["setup_inputs"] = "npm install", "package.json"
    if "npm-dev" in facts:
        found["run_local"] = "npm run dev"
    for script in ("deploy/deploy.sh", "scripts/deploy.sh", "deploy.sh"):
        if script in files:
            found["deploy"] = f"bash {path.rstrip('/')}/{script}"
            break
    if ".env" in files:
        found["secrets_from"] = f"{path.rstrip('/')}/.env"  # its location only; no value is read
    names = next((line[9:].split() for line in lines if line.startswith("envnames ")), [])
    if ".env" not in files and names:
        warnings.append(f"no .env yet. .env.example names {', '.join(names)}: create .env from it on that machine, "
                        "then set the secrets file here")
    elif names:
        warnings.append(f"tokens a task may get, by name: {', '.join(names)}. Allow them with /kandu-config")
    description = next((line[5:].strip() for line in lines if line.startswith("desc ")), "")
    if description:
        found["description"] = description[:200]
    found["forbidden"] = ", ".join(f"{line[4:]}/" for line in lines if line.startswith("dir "))
    found["repo_rules"] = " ".join(f"Follow {f}." for f in ("AGENTS.md", "CLAUDE.md") if f in files)
    return found


def plain_repo(text):
    """`owner/name` from what a person pastes: that, a GitHub URL, or a git remote."""
    text = (text or "").strip()
    found = REMOTE.search(text)
    if found:
        return f"{found.group(1)}/{found.group(2)}"
    return text.removesuffix(".git").strip("/")


def home_path(host, path):
    """A path as the config writes it: under the machine's home it starts with `~/`."""
    path = path.rstrip("/") or "/"
    if path.startswith("~"):
        return path
    home = host.absolute("~/.").removesuffix(".")
    if path.startswith(home) and len(path) > len(home):
        return "~/" + path[len(home):]
    return path


def detect(config, machine, path, repo="", runner=None):
    """Reads the copy at `path` on `machine` and proposes a project entry. Changes nothing."""
    machine, path, repo = machine.strip(), path.strip(), plain_repo(repo)
    if not any(m.name == machine for m in config.settings.machines):
        raise AddError(f"unknown machine {machine!r}")
    if not path:
        raise AddError("give the path of the project on that machine")
    if repo and not REPO.fullmatch(repo):
        raise AddError(f"the repo is owner/name or its GitHub link, for example acme-org/website; got {repo!r}")
    host = (runner or Runner(config)).hosts[machine]
    try:
        path = home_path(host, path)
        output = host.check(["sh", "-c", LOOK, "sh", host.absolute(path)])
    except RunnerError as e:
        raise AddError(f"machine {machine} did not answer: {e}") from None
    for p in config.projects:
        if p.machine == machine and p.path.rstrip("/") == path.rstrip("/"):
            raise AddError(f"this path is already the project {p.name!r}")
    lines = output.splitlines()
    facts = {line for line in lines if " " not in line}
    files = {line[5:] for line in lines if line.startswith("file ")}
    remote = next((line[7:].strip() for line in lines if line.startswith("remote ")), "")
    match = REMOTE.search(remote)
    found_repo = f"{match.group(1)}/{match.group(2)}" if match else ""
    if repo and found_repo and repo.lower() != found_repo.lower():
        raise AddError(f"the copy at that path belongs to {found_repo}, not {repo}")
    present = "missing" not in facts
    if present and "git" not in facts:
        raise AddError("that folder is not a git copy")
    repo = repo or found_repo
    if not repo:
        raise AddError("no GitHub repo found at that path: give it as owner/name")
    name = re.sub(r"[^a-z0-9-]+", "-", repo.split("/")[1].lower()).strip("-")
    family_board = next((p.board for p in config.projects if p.family == name and p.board), None)
    proposal = {"machine": machine, "path": path, "repo": repo, "name": name, "family": name, "run": "",
                "description": "", "test": "", "setup": "", "setup_inputs": "", "run_local": "", "deploy": "", "live_url": "", "secrets_from": "",
                "repo_rules": "", "forbidden": "", "test_task": False, **DEFAULTS}
    guessed = _guess(path, facts, files, lines) if present else {"warnings": []}
    warnings = guessed.pop("warnings")
    proposal.update(guessed)
    proposal["deploy_task"] = not proposal["deploy"]  # no deploy found: a first task can write one
    return {"proposal": proposal, "warnings": warnings, "copy": "present" if present else "will be cloned",
            "board": f"{family_board.owner} #{family_board.number}" if family_board else "a new board"}


def _items(value):
    """A list given as a list, or as text with commas between its items."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value or [])


def _entry(fields, board):
    """The project entry, in the order of config.example/projects.example.yaml."""
    entry = {"name": fields["name"], "description": fields.get("description", ""), "family": fields["family"],
             "repos": [fields["repo"]], "board": {"owner": board.owner, "number": board.number,
                                                  **({"columns": dict(board.columns)} if board.columns else {})},
             "run": fields["run"], "machine": fields["machine"], "path": fields["path"], "test": fields["test"]}
    if fields.get("base_branch"):  # none: the repo's default branch, as GitHub names it
        entry["base_branch"] = fields["base_branch"]
    if fields.get("setup"):
        entry["setup"] = {"command": fields["setup"], "inputs": _items(fields.get("setup_inputs"))}
    if fields.get("run_local"):
        entry["run_local"] = fields["run_local"]
    if fields.get("deploy") or fields.get("deploy_task"):
        entry["deploy"] = {"mode": "direct", "command": fields.get("deploy") or DEPLOY_PENDING}
        if fields.get("live_url"):
            entry["deploy"]["live_url"] = fields["live_url"]
    if fields.get("secrets_from"):
        entry["secrets_from"] = fields["secrets_from"]
    forbidden = _items(fields.get("forbidden"))
    try:
        numbers = {key: int(fields.get(key, DEFAULTS[key]))
                   for key in ("priority", "max_parallel_workers", "daily_token_limit")}
    except (TypeError, ValueError):
        raise AddError("priority, max_parallel_workers and daily_token_limit are whole numbers") from None
    entry.update({"forbidden": forbidden, "repo_rules": fields.get("repo_rules", ""),
                  "auto_merge": bool(fields.get("auto_merge", DEFAULTS["auto_merge"])), "frozen": False,
                  **numbers, "paused": False})
    if fields.get("max_open_prs"):  # none: no limit on the project's open PRs
        try:
            entry["max_open_prs"] = int(fields["max_open_prs"])
        except (TypeError, ValueError):
            raise AddError("max_open_prs is a whole number") from None
    return entry


def projects_text(text, entry):
    """`projects.yaml` with one more entry at the end."""
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=1000)
    block = "".join(f"  {line}" if line.strip() else line for line in block.splitlines(keepends=True))
    text = re.sub(r"^projects:\s*\[\]\s*$", "projects:", text, flags=re.MULTILINE)
    if not re.search(r"^projects:", text, flags=re.MULTILINE):
        text += "projects:\n"
    return text.rstrip("\n") + "\n\n" + block


def add_project(fields, github, local_dir=DEFAULT_LOCAL_DIR, check=None, runner=None):
    """Writes the project, cloning its copy and creating its family's board when there is none.

    The entry is validated before anything is created, so a mistake leaves nothing behind.
    """
    fields = {k: v.strip() if isinstance(v, str) else v for k, v in fields.items()}
    fields["repo"] = plain_repo(fields.get("repo", ""))
    for key in ("machine", "path", "repo", "name", "test", "run"):
        if not fields.get(key):
            raise AddError(f"{key} is required" + (": clone or in-place, always your choice" if key == "run" else ""))
    if fields["run"] not in RUN_MODES:
        raise AddError(f"run is one of {', '.join(RUN_MODES)}")
    if not PROJECT_NAME.fullmatch(fields["name"]):
        raise AddError("a project name is lower-case letters, digits and '-'")
    fields["family"] = fields.get("family") or fields["name"]
    for flag in FLAGS:
        if isinstance(fields.get(flag), str):
            fields[flag] = fields[flag] == "true"
    if fields.get("live_url") and not fields.get("deploy"):
        raise AddError("a live URL needs a deploy command")
    local_dir = Path(local_dir)
    config = load_config(local_dir)
    if any(p.name == fields["name"] for p in config.projects):
        raise AddError(f"there is already a project named {fields['name']!r}")
    runner = runner or Runner(config)
    found = detect(config, fields["machine"], fields["path"], fields["repo"], runner)
    fields["path"] = found["proposal"]["path"]
    owner = fields["repo"].split("/")[0]
    board = next((p.board for p in config.projects if p.family == fields["family"] and p.board), None)
    path = local_dir / "projects.yaml"
    text = path.read_text(encoding="utf-8")
    _validate_copy(local_dir, projects_text=projects_text(text, _entry(fields, board or Board(owner, 1))))
    host = runner.hosts[fields["machine"]]
    cloned = False
    if found["copy"] != "present":
        try:
            host.check(["git", "clone", f"https://github.com/{fields['repo']}.git", host.absolute(fields["path"])])
        except RunnerError as e:
            raise AddError(f"the repo could not be cloned on {fields['machine']}: {e}") from None
        cloned = True
    created = board is None
    if created:
        board = Board(owner=owner, number=github.create_board(owner, fields["name"], BOARD_COLUMNS))
    _write(path, projects_text(text, _entry(fields, board)))
    restored = memory.restore(local_dir, fields["name"])  # a project added back uses what Kandu learned before
    config = load_config(local_dir)
    project = next(p for p in config.projects if p.name == fields["name"])

    def first_task(title, body):
        try:
            return tasks.create_task(config, github, project.name, fields["repo"], title, body, local_dir,
                                     "Medium", "Chore", "Small")
        except (tasks.TaskError, GitHubError) as e:
            return f"not created: {e}"
    test_task = first_task(TEST_TASK_TITLE, TEST_TASK_BODY) if fields.get("test_task") else None
    wants_deploy = fields.get("deploy_task") and not fields.get("deploy")
    deploy_task = first_task(DEPLOY_TASK_TITLE, DEPLOY_TASK_BODY) if wants_deploy else None
    checks = check(config, project) if check else []
    return {"project": project.name, "board": {"owner": board.owner, "number": board.number, "created": created},
            "cloned": cloned, "memory_restored": bool(restored), "deploy_task": deploy_task, "test_task": test_task,
            "checks": [{"level": level, "text": text} for level, text in checks]}


def doctor_checks(config, project, github):
    from .doctor import check_project
    return check_project(project, Runner(config), github)


def main():
    parser = argparse.ArgumentParser(description="Add a machine or a project to Kandu.")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    sub = parser.add_subparsers(dest="what", required=True)
    m = sub.add_parser("machine", help="a machine reached by an SSH alias on the brain")
    m.add_argument("--name", required=True)
    m.add_argument("--ssh", required=True, help="the alias in ~/.ssh/config")
    m.add_argument("--always-on", action="store_true")
    m.add_argument("--run-tmp-dir", help="a folder on that machine's own disk: each run gets its own temporary "
                                         "folder inside it, removed when it ends (default: off)")
    p = sub.add_parser("project", help="a project from its machine, path and repo")
    p.add_argument("--machine", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="change a proposed field; run=clone or run=in-place is always needed to add")
    p.add_argument("--dry-run", action="store_true", help="only show what would be written")
    args = parser.parse_args()
    try:
        if args.what == "machine":
            print(json.dumps(add_machine(args.name, args.ssh, args.always_on, args.local,
                                         run_tmp_dir=args.run_tmp_dir)))
            return 0
        config = load_config(args.local)
        found = detect(config, args.machine, args.path, args.repo)
        fields = found["proposal"]
        for pair in args.set:
            key, _, value = pair.partition("=")
            fields[key] = value.lower() == "true" if key in FLAGS else value
        if args.dry_run:
            print(json.dumps(found, indent=2, ensure_ascii=False))
            return 0
        from .mirror import open_github
        github = open_github(config)
        print(json.dumps(add_project(fields, github, args.local, lambda c, pr: doctor_checks(c, pr, github)),
                         indent=2, ensure_ascii=False))
    except (AddError, ConfigError) as e:
        print(f"add: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
