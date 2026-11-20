"""Loads and validates local/kandu.yaml and local/projects.yaml (DESIGN §4, §12).

Every problem is reported at once, so a config can be fixed in one pass.
Paths are kept as written: a `~` may belong to another machine.
"""
import argparse
import json
import posixpath
import re
import shutil
import sys
import tempfile
import types
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

import yaml

DEFAULT_LOCAL_DIR = Path(__file__).resolve().parent.parent / "local"
SSH_REACH = re.compile(r"ssh \w[\w.@-]*")
RUN_MODES = ("clone", "in-place")  # where a project runs: always set by the user, never inferred
ROLES = ("worker", "reviewer", "splitter", "live_check", "queen")
MODEL_ROLES = ROLES + ("bazras",)  # Bazras's tools are fixed in code, so it has no `permissions` entry.
PROJECT_MODEL_ROLES = tuple(role for role in MODEL_ROLES if role != "queen")  # the queen works for every project at once
CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
MODEL = re.compile(r"[a-z][\w.\[\]-]*")  # It reaches `claude --model`, so it cannot be a flag.
GIT_NAME = re.compile(r"\w[\w./-]*")  # a remote or branch that reaches git as an argument, so never a flag
TECHNICAL_DEBT = "Technical debt"
SPLIT_APPROVALS = ("user", "queen")   # who accepts a split proposal; the dashboard's switch wins over the registry
BOARD_COLUMNS = ("Backlog", TECHNICAL_DEBT, "Ready", "In progress", "In review", "Needs info", "Needs split", "Blocked", "Done")
BOARD_FIELDS = {"Priority": ("High", "Medium", "Low"), "Kind": ("Bug", "Feature", "Chore"),
                "Size": ("Small", "Medium", "Large")}
DEADLINE_FIELD = "Deadline"
NOTIFY_EVENTS = ("supervisor_stopped", "failure", "needs_decision", "pr_ready", "task_done", "usage_limit", "claude_login",
                 "machine_unreachable")
NOTIFY_LANGUAGES = ("en", "fa")
# A run temporary folder's parent: absolute or under ~, with no `..` and no space, and never the root or ~ itself.
RUN_TMP_DIR = re.compile(r"(/|~/)[\w.@+/-]*[\w@+-][\w.@+/-]*")
LOOKS = ("classic", "classic-new", "clean")  # the dashboard's looks: Classic, Classic new and Kandu Clean


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass
class Brain:
    machine: str
    work_dir: str
    install_dir: str


@dataclass
class KnowledgeSource:
    name: str
    machine: str
    path: str


@dataclass
class Didban:
    knowledge: list[KnowledgeSource] = field(default_factory=list)


@dataclass
class Machine:
    name: str
    reach: str
    always_on: bool
    # A run temporary folder (DESIGN §9): each agent run, test and setup command on this machine gets its own
    # folder inside `run_tmp_dir` as its temporary directory, removed when the run ends. Off until the owner turns it on.
    run_tmp: bool = False
    run_tmp_dir: str | None = None   # a folder on the machine's own disk: absolute, or starting with ~/


@dataclass
class ClaudeLimits:
    session_5h_percent: int
    weekly_percent: int


@dataclass
class Claude:
    auth: str
    limits: ClaudeLimits
    login_warn_days: int = 7
    models: dict[str, str] = field(default_factory=dict)  # role -> model; a role not named runs on Claude's default


@dataclass
class GitHub:
    account: str
    owners: list[str]
    sync_every_minutes: int = 10   # how often Kandu's copy is read from GitHub and its queue sent (DESIGN §3)


@dataclass
class Telegram:
    bot_token_env: str
    chat_id_env: str
    buttons: bool = False  # an approve button under a PR that waits for the user's yes (telegram_bot.py)


@dataclass
class Notify:
    adapter: str
    telegram: Telegram | None = None
    events: list[str] = field(default_factory=lambda: list(NOTIFY_EVENTS))
    language: str | None = None    # the language of notifications only; without it, top-level `language`


@dataclass
class Supervisor:
    wake_every_minutes: int = 60
    max_cycle_minutes: int = 50
    max_parallel_workers: int = 2
    max_parallel_sizers: int = 4   # splitter runs are read-only, so they go side by side
    auto_merge: bool = False
    auto_revert: bool = False
    queen: bool = True
    queen_max_retries: int = 3
    queen_daily_token_limit: int = 2000000
    unreachable_alert_hours: int = 12  # a machine silent this long while tasks wait for it: the user hears once
    queen_drop_blockers: bool = False   # she may ask the user to drop a blocker; off until the user turns it on
    # A merged task's folders and branches go: on by default, as the user chose, and off from the dashboard.
    clean_merged: bool = True


@dataclass
class BazrasSettings:
    at: str = "06:00"              # when a project's daily look is due, unless its `inspect.at` says otherwise


@dataclass
class Backup:
    """Kandu's daily copy of what it cannot rebuild from Git: `local/` and `state/` (DESIGN §12).

    Off until the user turns it on, here or in the dashboard: a new install must not start work by itself.
    """
    enabled: bool = False
    at: str = "03:00"              # HH:MM on the brain's clock
    destination: str = "~/kandu-backups"   # a folder on the brain's machine, outside `local/` and `state/`
    keep: int = 7                  # how many copies stay; older ones are removed after a good one is written


@dataclass
class Upstream:
    """How far the install's `main` is from the public Kandu it came from, shown on the dashboard (DESIGN §12).

    Off until the user turns it on: a new install must not reach the network by itself. The check only fetches
    and counts; updates come in as a task on the board of `project`, Kandu's own repo, and never by themselves.
    """
    enabled: bool = False
    remote: str = "upstream"       # the git remote of the public repo, in the install
    branch: str = "main"           # its branch the install's `main` is counted against
    every_hours: int = 4           # how often the dashboard checks by itself; "Check now" checks at once
    project: str = ""              # the project in projects.yaml whose repo is this install's own copy


@dataclass
class Rules:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class Permissions:
    """What each role may do. A role not named here may do everything (DESIGN §9)."""
    worker: Rules | None = None
    reviewer: Rules | None = None
    splitter: Rules | None = None
    live_check: Rules | None = None
    queen: Rules | None = None


@dataclass
class Dashboard:
    bind: str
    port: int
    show_requests: bool = False    # list open requests with their decision maps; off until the user turns it on
    look: str = "classic"          # the look a browser opens in until it chooses its own (LOOKS)


@dataclass
class Board:
    owner: str
    number: int
    columns: dict[str, str] = field(default_factory=dict)  # Kandu's column name -> this board's, if different
    assignee: str = ""             # set by Kandu from `multi_user`, never by hand: only cards assigned to it are seen


@dataclass
class Deploy:
    mode: str
    command: str
    live_url: str = ""             # empty: nothing can check the live software, for example behind a login


@dataclass
class Setup:
    """What a fresh checkout needs before the project's `test` runs there, such as `npm ci` (DESIGN §4)."""
    command: str
    inputs: list[str] = field(default_factory=list)  # files whose change runs it again; none: each new head


@dataclass
class Inspect:
    """What Bazras looks at each day (DESIGN §5). It only reads; a check that exits non-zero is a finding.

    Off until the user turns it on, here or in the dashboard: a new install must not start work by itself.
    """
    enabled: bool = False
    at: str | None = None          # HH:MM on the brain's clock; without it, `bazras.at`
    tests: bool = True             # run the project's test command on its default branch
    units: list[str] = field(default_factory=list)   # systemd units: `name.service`, or `user:name.timer` for a user unit
    cron: list[str] = field(default_factory=list)    # text each must find in the machine's crontab
    logs: list[str] = field(default_factory=list)    # log files on its machine; relative to `path`; new error lines count
    checks: list[str] = field(default_factory=list)  # the project's own read-only commands, run in a checkout
    ignore: list[str] = field(default_factory=list)  # file patterns other writers change every day, e.g. `PR_LOG.md`


@dataclass
class Project:
    name: str
    family: str
    repos: list[str]
    run: str
    machine: str
    path: str
    test: str
    priority: int
    max_parallel_workers: int
    paused: bool
    daily_token_limit: int
    description: str = ""
    board: Board | None = None
    run_local: str | None = None
    setup: Setup | None = None       # run before Kandu runs `test` in a checkout of its own, when that needs it
    deploy: Deploy | None = None
    inspect: Inspect | None = None   # Bazras's daily look; without it, or with it off, Bazras leaves the project alone
    secrets_from: str | None = None
    task_tokens: dict[str, str] = field(default_factory=dict)  # token name -> what it is for
    forbidden: list[str] = field(default_factory=list)
    repo_rules: str = ""
    queen_rules: str = ""
    auto_merge: bool | None = None
    frozen: bool = False
    split_approval: str = "user"     # who accepts this project's split proposals (SPLIT_APPROVALS)
    queen_answers_one_way: bool = False   # the queen's yes to a head answers its one-way door and its questions
    multi_user: bool = False         # several people share the board, each with a Kandu of their own: this install
                                     # sees only the cards assigned to its `github.account`; off, assignees do not matter
    allowed_tools: list[str] = field(default_factory=list)   # added to the worker's and live check's rules, if they have any
    reports: list[str] = field(default_factory=list)   # files on its machine that `kandu.brief show` reads, read-only
    models: dict[str, str] = field(default_factory=dict)   # role -> model for this project; a role not named here
                                                           # runs on `claude.models`, and without it on Claude's default


@dataclass
class Settings:
    brain: Brain
    machines: list[Machine]
    claude: Claude
    github: GitHub
    notify: Notify
    dashboard: Dashboard
    language: str = "en"           # what Kandu says to the user; everything on GitHub stays in English
    supervisor: Supervisor = field(default_factory=Supervisor)
    permissions: Permissions = field(default_factory=Permissions)
    didban: Didban = field(default_factory=Didban)
    bazras: BazrasSettings = field(default_factory=BazrasSettings)
    backup: Backup = field(default_factory=Backup)
    upstream: Upstream = field(default_factory=Upstream)


@dataclass
class Registry:
    projects: list[Project]


@dataclass
class DidbanBrain:
    reach: str
    install_dir: str


@dataclass
class DidbanConfig:
    brain: DidbanBrain


@dataclass
class Config:
    settings: Settings
    projects: list[Project]

    def machine(self, name):
        return next(m for m in self.settings.machines if m.name == name)

    def model_for(self, project, role):
        """The model `role` runs on for `project`: its own entry, then `claude.models`, then None, Claude's default.
        The queen works for every project at once, so only `claude.models` names hers."""
        if project is not None and role != "queen" and role in project.models:
            return project.models[role]
        return self.settings.claude.models.get(role)

    def auto_merge_for(self, project):
        if project.auto_merge is not None:
            return project.auto_merge
        return self.settings.supervisor.auto_merge


def _type_name(tp):
    return getattr(tp, "__name__", str(tp))


def _build(tp, raw, where, problems):
    """Turns parsed YAML into the dataclass `tp`, collecting problems."""
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        inner = next(a for a in get_args(tp) if a is not type(None))
        return None if raw is None else _build(inner, raw, where, problems)
    if origin is list:
        if not isinstance(raw, list):
            problems.append(f"{where}: must be a list")
            return []
        item = get_args(tp)[0]
        return [_build(item, v, f"{where}[{i}]", problems) for i, v in enumerate(raw)]
    if origin is dict:
        if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
            problems.append(f"{where}: must map text to text")
            return {}
        return raw
    if is_dataclass(tp):
        return _build_dataclass(tp, raw, where, problems)
    if type(raw) is not tp:
        problems.append(f"{where}: must be {_type_name(tp)}, got {raw!r}")
    return raw


def _build_dataclass(tp, raw, where, problems):
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping")
        return None
    hints = get_type_hints(tp)
    known = {f.name: f for f in fields(tp)}
    for key in raw:
        if key not in known:
            problems.append(f"{where}.{key}: unknown key")
    values = {}
    for name, f in known.items():
        path = f"{where}.{name}" if where else name
        if name in raw:
            values[name] = _build(hints[name], raw[name], path, problems)
        elif f.default is MISSING and f.default_factory is MISSING:
            problems.append(f"{path}: missing")
    if any(k not in values and known[k].default is MISSING
           and known[k].default_factory is MISSING for k in known):
        return None
    return tp(**values)


def _load_yaml(path, problems):
    if not path.exists():
        problems.append(f"{path}: file not found")
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        problems.append(f"{path}: invalid YAML: {e}")
        return None
    return {} if data is None else data


def _check_one_of(value, allowed, where, problems):
    if value not in allowed:
        problems.append(f"{where}: must be one of {', '.join(allowed)}, got {value!r}")


def _check_positive(value, where, problems):
    if value < 1:
        problems.append(f"{where}: must be at least 1, got {value}")


def _check_percent(value, where, problems):
    if not 1 <= value <= 100:
        problems.append(f"{where}: must be between 1 and 100, got {value}")


def _is_inside(path, folder):
    return posixpath.normpath(path).startswith(posixpath.normpath(folder).rstrip("/") + "/")


def _check_settings(s, problems):
    names = [m.name for m in s.machines]
    if len(set(names)) != len(names):
        problems.append("machines: names must be unique")
    for i, m in enumerate(s.machines):
        if m.reach != "local" and not SSH_REACH.fullmatch(m.reach):
            problems.append(f"machines[{i}].reach: must be 'local' or 'ssh <alias>', got {m.reach!r}")
        _check_run_tmp(m.run_tmp, m.run_tmp_dir, f"machines[{i}]", problems)

    brain = next((m for m in s.machines if m.name == s.brain.machine), None)
    if brain is None:
        problems.append(f"brain.machine: unknown machine {s.brain.machine!r}")
    elif brain.reach != "local":
        problems.append(f"brain.machine: {s.brain.machine!r} must have reach: local")

    for i, k in enumerate(s.didban.knowledge):
        if k.machine not in names:
            problems.append(f"didban.knowledge[{i}].machine: unknown machine {k.machine!r}")

    _check_one_of(s.claude.auth, ("subscription", "api_key"), "claude.auth", problems)
    _check_percent(s.claude.limits.session_5h_percent, "claude.limits.session_5h_percent", problems)
    _check_percent(s.claude.limits.weekly_percent, "claude.limits.weekly_percent", problems)
    _check_positive(s.claude.login_warn_days, "claude.login_warn_days", problems)
    for role, model in s.claude.models.items():
        _check_one_of(role, MODEL_ROLES, "claude.models", problems)
        if not MODEL.fullmatch(model):
            problems.append(f"claude.models.{role}: not a model name: {model!r}")

    _check_one_of(s.notify.adapter, ("telegram", "none"), "notify.adapter", problems)
    if s.notify.adapter == "telegram" and s.notify.telegram is None:
        problems.append("notify.telegram: required when notify.adapter is telegram")
    for event in s.notify.events:
        _check_one_of(event, NOTIFY_EVENTS, "notify.events", problems)
    if s.notify.language is not None:
        _check_one_of(s.notify.language, NOTIFY_LANGUAGES, "notify.language", problems)

    for name in ("wake_every_minutes", "max_cycle_minutes", "max_parallel_workers", "max_parallel_sizers",
                 "queen_max_retries", "queen_daily_token_limit", "unreachable_alert_hours"):
        _check_positive(getattr(s.supervisor, name), f"supervisor.{name}", problems)
    _check_positive(s.github.sync_every_minutes, "github.sync_every_minutes", problems)
    if s.supervisor.auto_revert:
        problems.append("supervisor.auto_revert: must be false; Kandu only reports breakage (DESIGN §5)")
    if not CLOCK.fullmatch(s.bazras.at):
        problems.append(f"bazras.at: must be HH:MM, got {s.bazras.at!r}")
    _check_backup(s.backup.enabled, s.backup.at, s.backup.destination, s.backup.keep, problems)
    if not 1 <= s.dashboard.port <= 65535:
        problems.append(f"dashboard.port: must be between 1 and 65535, got {s.dashboard.port}")
    _check_one_of(s.dashboard.look, LOOKS, "dashboard.look", problems)


def _check_run_tmp(enabled, folder, where, problems):
    if folder is not None and (not RUN_TMP_DIR.fullmatch(folder) or ".." in folder.split("/")):
        problems.append(f"{where}.run_tmp_dir: must be a folder that is absolute or starts with ~/, got {folder!r}")
    if enabled and folder is None:
        problems.append(f"{where}.run_tmp_dir: required when run_tmp is true")


def _check_backup(enabled, at, destination, keep, problems):
    if not CLOCK.fullmatch(at):
        problems.append(f"backup.at: must be HH:MM, got {at!r}")
    _check_positive(keep, "backup.keep", problems)
    if not destination.strip():
        if enabled:
            problems.append("backup.destination: required when backup.enabled is true")
    elif not destination.startswith(("/", "~/")) or "\n" in destination:
        problems.append(f"backup.destination: must be a full path, starting with / or ~/, got {destination!r}")


def _check_projects(projects, s, problems):
    machines = [m.name for m in s.machines]
    seen = set()
    family_boards, family_multi = {}, {}
    for i, p in enumerate(projects):
        where = f"projects[{i}] ({p.name})"
        if p.name in seen:
            problems.append(f"{where}: duplicate name")
        seen.add(p.name)
        if not p.repos:
            problems.append(f"{where}.repos: must not be empty")
        for repo in p.repos:
            owner, _, name = repo.partition("/")
            if not owner or not name or "/" in name:
                problems.append(f"{where}.repos: must be owner/name, got {repo!r}")
            elif owner not in s.github.owners:
                problems.append(f"{where}.repos: owner {owner!r} is not in github.owners")
        if p.board is not None:
            if p.board.owner not in s.github.owners:
                problems.append(f"{where}.board: owner {p.board.owner!r} is not in github.owners")
            for column in p.board.columns:
                if column not in BOARD_COLUMNS:
                    problems.append(f"{where}.board.columns: unknown column {column!r}")
            if family_boards.setdefault(p.family, p.board) != p.board:
                problems.append(f"{where}.board: family {p.family!r} already has another board; one board per family")
            if p.board.assignee:
                problems.append(f"{where}.board.assignee: set by Kandu; set multi_user: true instead")
            if family_multi.setdefault(p.family, p.multi_user) != p.multi_user:
                problems.append(f"{where}.multi_user: the projects of family {p.family!r} share a board, "
                                "so they must all be multi_user or none")
        elif p.multi_user:
            problems.append(f"{where}.multi_user: needs a board")
        _check_one_of(p.run, RUN_MODES, f"{where}.run", problems)
        if p.machine not in machines:
            problems.append(f"{where}.machine: unknown machine {p.machine!r}")
        if p.run == "clone" and p.machine != s.brain.machine:
            problems.append(f"{where}.machine: run: clone must be on the brain's machine {s.brain.machine!r}")
        if p.run == "clone" and not _is_inside(p.path, s.brain.work_dir):
            problems.append(f"{where}.path: run: clone must be inside brain.work_dir {s.brain.work_dir!r}, "
                            f"written the same way, got {p.path!r}")
        if p.task_tokens and not p.secrets_from:
            problems.append(f"{where}.task_tokens: needs secrets_from, the file they are read from")
        for name, purpose in p.task_tokens.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                problems.append(f"{where}.task_tokens: not a variable name: {name!r}")
            if not purpose.strip():
                problems.append(f"{where}.task_tokens.{name}: say what it is for")
        if p.inspect is not None:
            if p.inspect.at is not None and not CLOCK.fullmatch(p.inspect.at):
                problems.append(f"{where}.inspect.at: must be HH:MM, got {p.inspect.at!r}")
            for i, unit in enumerate(p.inspect.units):
                if not re.fullmatch(r"(user:)?[\w@.:-]+\.(service|timer)", unit):
                    problems.append(f"{where}.inspect.units[{i}]: must be name.service or name.timer, "
                                    f"with user: for a user unit, got {unit!r}")
            for name in ("cron", "checks", "ignore", "logs"):
                if any(not entry.strip() for entry in getattr(p.inspect, name)):
                    problems.append(f"{where}.inspect.{name}: an entry must not be empty")
        for report in p.reports:
            if not report.strip():
                problems.append(f"{where}.reports: a path must not be empty")
        if p.setup is not None:
            if not p.setup.command.strip():
                problems.append(f"{where}.setup.command: must not be empty")
            if any(not entry.strip() for entry in p.setup.inputs):
                problems.append(f"{where}.setup.inputs: an entry must not be empty")
        if p.deploy is not None:
            _check_one_of(p.deploy.mode, ("direct", "preview"), f"{where}.deploy.mode", problems)
        _check_one_of(p.split_approval, SPLIT_APPROVALS, f"{where}.split_approval", problems)
        for role, model in p.models.items():
            _check_one_of(role, PROJECT_MODEL_ROLES, f"{where}.models", problems)
            if not MODEL.fullmatch(model):
                problems.append(f"{where}.models.{role}: not a model name: {model!r}")
        _check_positive(p.priority, f"{where}.priority", problems)
        _check_positive(p.max_parallel_workers, f"{where}.max_parallel_workers", problems)
        _check_positive(p.daily_token_limit, f"{where}.daily_token_limit", problems)


def _check_upstream(s, projects, problems):
    u = s.upstream
    _check_positive(u.every_hours, "upstream.every_hours", problems)
    for name in ("remote", "branch"):
        if not GIT_NAME.fullmatch(getattr(u, name)):
            problems.append(f"upstream.{name}: must be a git name, not starting with '-', got {getattr(u, name)!r}")
    if u.enabled:
        project = next((p for p in projects if p.name == u.project), None)
        if project is None or project.board is None:
            problems.append(f"upstream.project: must name a project with a board, Kandu's own repo, got {u.project!r}")


def set_limits(session, week, local_dir=DEFAULT_LOCAL_DIR):
    """Changes the two usage limits in `kandu.yaml`, line by line, so its comments stay."""
    problems = []
    _check_percent(session, "claude.limits.session_5h_percent", problems)
    _check_percent(week, "claude.limits.weekly_percent", problems)
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "kandu.yaml"
    text = path.read_text(encoding="utf-8")
    for name, value in (("session_5h_percent", session), ("weekly_percent", week)):
        text, found = re.subn(rf"^(\s+{name}:\s*)\d+", rf"\g<1>{value}", text, flags=re.MULTILINE)
        if found != 1:
            raise ConfigError([f"claude.limits.{name}: expected one such line in kandu.yaml, found {found}"])
    _replace_file(path, text)


def _replace_file(path, text):
    temporary = path.with_suffix(".yaml.new")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


def _set_total_workers(text, value):
    """The one `max_parallel_workers` in `kandu.yaml` is the supervisor's; a missing one is added there."""
    line = re.compile(r"^([ \t]+)max_parallel_workers:.*$", re.MULTILINE)
    found = line.findall(text)
    if len(found) > 1:
        raise ConfigError([f"supervisor.max_parallel_workers: expected one such line in kandu.yaml, found {len(found)}"])
    if found:
        return line.sub(lambda m: f"{m.group(1)}max_parallel_workers: {value}", text)
    if re.search(r"^supervisor:[ \t]*(#.*)?$", text, flags=re.MULTILINE):
        return re.sub(r"^(supervisor:[ \t]*(#.*)?)$", rf"\g<1>\n  max_parallel_workers: {value}", text,
                      count=1, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n\nsupervisor:\n  max_parallel_workers: {value}\n"


def _set_project_workers(text, name, value):
    """Changes the line in this project's entry only. Its old comment goes: it spoke of the old number."""
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*- name:", l)]
    start = next((i for i in starts if re.fullmatch(rf"\s*- name:\s*[\"']?{re.escape(name)}[\"']?\s*(#.*)?", lines[i])), None)
    if start is None:
        raise ConfigError([f"projects: no entry named {name!r} in projects.yaml"])
    end = next((i for i in starts if i > start), len(lines))
    hits = [i for i in range(start, end) if re.match(r"^\s+max_parallel_workers:", lines[i])]
    if len(hits) != 1:
        raise ConfigError([f"{name}.max_parallel_workers: expected one such line in its entry, found {len(hits)}"])
    indent = re.match(r"^(\s+)", lines[hits[0]]).group(1)
    lines[hits[0]] = f"{indent}max_parallel_workers: {value}"
    return "\n".join(lines)


def set_inspect(name, enabled, at=None, local_dir=DEFAULT_LOCAL_DIR):
    """Turns Bazras on or off for one project, and sets its hour, in `projects.yaml`, line by line.

    A project with no `inspect` block gets one with only these two lines: the rest keeps its defaults.
    """
    if at is not None and not CLOCK.fullmatch(at):
        raise ConfigError([f"{name}.inspect.at: must be HH:MM, got {at!r}"])
    path = Path(local_dir) / "projects.yaml"
    text = _set_project_inspect(path.read_text(encoding="utf-8"), name, enabled, at)
    entry = next((p for p in (yaml.safe_load(text) or {}).get("projects", []) if p.get("name") == name), {})
    written = entry.get("inspect") or {}
    if written.get("enabled") is not enabled or (at is not None and written.get("at") != at):
        raise ConfigError([f"{name}.inspect: could not be written; change it in projects.yaml by hand"])
    _replace_file(path, text)


def _set_project_inspect(text, name, enabled, at):
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*- name:", l)]
    start = next((i for i in starts if re.fullmatch(rf"\s*- name:\s*[\"']?{re.escape(name)}[\"']?\s*(#.*)?", lines[i])), None)
    if start is None:
        raise ConfigError([f"projects: no entry named {name!r} in projects.yaml"])
    end = next((i for i in starts if i > start), len(lines))
    key_indent = lines[start].index("name:")
    values = {"enabled": "true" if enabled else "false", **({"at": f'"{at}"'} if at is not None else {})}
    found = [i for i in range(start + 1, end) if re.match(rf"^ {{{key_indent}}}inspect:", lines[i])]
    if not found:
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
                                   and len(lines[end - 1]) - len(lines[end - 1].lstrip()) <= key_indent):
            end -= 1  # The block goes after the entry's last key, before blank lines and the next entry's comment.
        pad = " " * key_indent
        block = [f"{pad}inspect:"] + [f"{pad}  {key}: {value}" for key, value in values.items()]
        return "\n".join(lines[:end] + block + lines[end:])
    head = found[0]
    if re.match(rf"^ {{{key_indent}}}inspect:\s*[^\s#]", lines[head]):
        raise ConfigError([f"{name}.inspect: written on one line; change it in projects.yaml by hand"])
    block_end = head + 1
    while block_end < end and (not lines[block_end].strip()
                               or len(lines[block_end]) - len(lines[block_end].lstrip()) > key_indent):
        block_end += 1
    children = [i for i in range(head + 1, block_end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    child = " " * (len(lines[children[0]]) - len(lines[children[0]].lstrip())) if children else " " * (key_indent + 2)
    for key, value in values.items():
        hit = next((i for i in children if re.match(rf"^{child}{key}:", lines[i])), None)
        if hit is None:
            lines.insert(head + 1, f"{child}{key}: {value}")
            children = [i + 1 for i in children]
        else:
            lines[hit] = f"{child}{key}: {value}"  # Its old comment goes: it spoke of the old value.
    return "\n".join(lines)


def set_backup(enabled, at=None, destination=None, keep=None, local_dir=DEFAULT_LOCAL_DIR):
    """Turns the backup on or off, and sets what else is given, in `kandu.yaml`, line by line.

    A value left as None stays as it is; a missing `backup` block is added. Nothing is written if a value is wrong.
    """
    path = Path(local_dir) / "kandu.yaml"
    text = path.read_text(encoding="utf-8")
    now = (yaml.safe_load(text) or {}).get("backup") or {}
    if not isinstance(now, dict):
        raise ConfigError(["backup: written on one line; change it in kandu.yaml by hand"])
    wanted = {"enabled": enabled, "at": at, "destination": destination, "keep": keep}
    given = {key: value for key, value in wanted.items() if value is not None}
    whole = {**Backup().__dict__, **now, **given}
    problems = []
    _check_backup(whole["enabled"], whole["at"], whole["destination"], whole["keep"], problems)
    if problems:
        raise ConfigError(problems)
    written = {key: json.dumps(value) for key, value in given.items()}  # JSON's text and numbers are YAML too.
    text = _set_top_block(text, "backup", written)
    after = (yaml.safe_load(text) or {}).get("backup") or {}
    if any(after.get(key) != value for key, value in given.items()):
        raise ConfigError(["backup: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def _set_top_block(text, block, values):
    """Sets each `key: value` under the top-level `block:`, adding the block or a key that is missing."""
    lines = text.split("\n")
    head = next((i for i, l in enumerate(lines) if re.fullmatch(rf"{block}:[ \t]*(#.*)?", l)), None)
    if head is None:
        if any(re.match(rf"{block}:", l) for l in lines):
            raise ConfigError([f"{block}: written on one line; change it in kandu.yaml by hand"])
        return text.rstrip("\n") + f"\n\n{block}:\n" + "".join(f"  {k}: {v}\n" for k, v in values.items())
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    children = [i for i in range(head + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    pad = " " * (len(lines[children[0]]) - len(lines[children[0]].lstrip())) if children else "  "
    for key, value in values.items():
        hit = next((i for i in children if re.match(rf"^{pad}{key}:", lines[i])), None)
        if hit is None:
            lines.insert(head + 1, f"{pad}{key}: {value}")
            children = [i + 1 for i in children]
        else:
            lines[hit] = f"{pad}{key}: {value}"  # Its old comment goes: it spoke of the old value.
    return "\n".join(lines)


def _set_list_in_block(text, block, key, items):
    """Sets `key:` under the top-level `block:` to a list, one `- item` line each, in place of the list it had."""
    lines = text.split("\n")
    head = next((i for i, l in enumerate(lines) if re.fullmatch(rf"{block}:[ \t]*(#.*)?", l)), None)
    if head is None:
        raise ConfigError([f"{block}: not in kandu.yaml as a block; change it there by hand"])
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    children = [i for i in range(head + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    pad = " " * (len(lines[children[0]]) - len(lines[children[0]].lstrip())) if children else "  "
    new = [f"{pad}{key}:" + ("" if items else " []")] + [f"{pad}  - {item}" for item in items]
    hit = next((i for i in children if re.match(rf"^{pad}{key}:", lines[i])), None)
    if hit is None:
        lines[head + 1:head + 1] = new
        return "\n".join(lines)
    stop = hit + 1  # its old list: the lines under it, deeper than the key or `- ` at its depth
    while stop < end and (not lines[stop].strip() or lines[stop].startswith(pad + " ") or lines[stop].startswith(pad + "- ")
                          or lines[stop].lstrip().startswith("#") and len(lines[stop]) - len(lines[stop].lstrip()) > len(pad)):
        stop += 1
    while stop > hit + 1 and not lines[stop - 1].strip():  # a blank line before the next block stays
        stop -= 1
    lines[hit:stop] = new  # its old comment goes: it spoke of the old list
    return "\n".join(lines)


def set_notify_events(events, local_dir=DEFAULT_LOCAL_DIR):
    """Sets what Kandu tells the user, `notify.events` in `kandu.yaml`, in NOTIFY_EVENTS's order. Nothing is written
    if one is not in NOTIFY_EVENTS. The next cycle uses it."""
    problems = []
    for event in events:
        _check_one_of(event, NOTIFY_EVENTS, "notify.events", problems)
    if problems:
        raise ConfigError(problems)
    events = [event for event in NOTIFY_EVENTS if event in events]
    path = Path(local_dir) / "kandu.yaml"
    text = _set_list_in_block(path.read_text(encoding="utf-8"), "notify", "events", events)
    if ((yaml.safe_load(text) or {}).get("notify") or {}).get("events") != events:
        raise ConfigError(["notify.events: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)
    return events


def set_telegram_buttons(enabled, local_dir=DEFAULT_LOCAL_DIR):
    """Turns the approve button in Telegram on or off: `notify.telegram.buttons` in `kandu.yaml`, line by line."""
    path = Path(local_dir) / "kandu.yaml"
    lines = path.read_text(encoding="utf-8").split("\n")
    head = next((i for i, l in enumerate(lines) if re.fullmatch(r"notify:[ \t]*(#.*)?", l)), None)
    sub = next((i for i in range((head or 0) + 1, len(lines)) if head is not None and re.fullmatch(r"( +)telegram:[ \t]*(#.*)?", lines[i])), None)
    if sub is None:
        raise ConfigError(["notify.telegram: not in kandu.yaml as a block; change it there by hand"])
    pad = re.match(r" +", lines[sub]).group(0)
    inner = next((re.match(r" +", lines[i]).group(0) for i in range(sub + 1, len(lines))
                  if lines[i].strip() and not lines[i].lstrip().startswith("#")), pad + "  ")
    end = sub + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(inner)):
        end += 1
    value = "true" if enabled else "false"
    hit = next((i for i in range(sub + 1, end) if re.match(rf"^{inner}buttons:", lines[i])), None)
    if hit is None:
        lines.insert(sub + 1, f"{inner}buttons: {value}")
    else:
        lines[hit] = f"{inner}buttons: {value}"
    text = "\n".join(lines)
    if (((yaml.safe_load(text) or {}).get("notify") or {}).get("telegram") or {}).get("buttons") is not enabled:
        raise ConfigError(["notify.telegram.buttons: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def set_drop_blockers(enabled, local_dir=DEFAULT_LOCAL_DIR):
    """Lets the queen ask to drop a blocker, or stops her, in `kandu.yaml`'s `supervisor` block, line by line."""
    path = Path(local_dir) / "kandu.yaml"
    text = _set_top_block(path.read_text(encoding="utf-8"), "supervisor",
                          {"queen_drop_blockers": "true" if enabled else "false"})
    if ((yaml.safe_load(text) or {}).get("supervisor") or {}).get("queen_drop_blockers") is not enabled:
        raise ConfigError(["supervisor.queen_drop_blockers: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def set_clean_merged(enabled, local_dir=DEFAULT_LOCAL_DIR):
    """Turns the cleanup after a merge on or off in `kandu.yaml`'s `supervisor` block, line by line."""
    path = Path(local_dir) / "kandu.yaml"
    text = _set_top_block(path.read_text(encoding="utf-8"), "supervisor",
                          {"clean_merged": "true" if enabled else "false"})
    if ((yaml.safe_load(text) or {}).get("supervisor") or {}).get("clean_merged") is not enabled:
        raise ConfigError(["supervisor.clean_merged: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def set_run_tmp(machine, enabled, folder=None, local_dir=DEFAULT_LOCAL_DIR):
    """Turns a machine's run temporary folder on or off, and sets its folder when one is given, in that machine's
    entry under `machines:` in `kandu.yaml`, line by line. The whole config is checked on a copy first, so nothing is
    written if a value is wrong. The next run on the machine uses it."""
    problems = []
    if folder is not None:
        _check_run_tmp(enabled, folder, f"{machine}", problems)
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "kandu.yaml"
    values = {"run_tmp": enabled, **({"run_tmp_dir": json.dumps(folder)} if folder is not None else {})}
    text = _set_machine_values(path.read_text(encoding="utf-8"), machine, values)
    config = _check_on_copy(local_dir, "kandu.yaml", text)
    written = next(m for m in config.settings.machines if m.name == machine)
    if written.run_tmp is not enabled or (folder is not None and written.run_tmp_dir != folder):
        raise ConfigError([f"{machine}.run_tmp: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def _set_machine_values(text, name, values):
    """Sets each `key: value` in the entry of machine `name` under the top-level `machines:` only."""
    lines = text.split("\n")
    head = next((i for i, l in enumerate(lines) if re.fullmatch(r"machines:[ \t]*(#.*)?", l)), None)
    if head is None:
        raise ConfigError(["machines: not in kandu.yaml as a list; change it there by hand"])
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#-"):
        end += 1
    block = "\n".join(lines[head:end])
    if not re.search(rf"^\s*- name:\s*[\"']?{re.escape(name)}[\"']?\s*(#.*)?$", block, flags=re.MULTILINE):
        raise ConfigError([f"machines: no machine named {name!r} in kandu.yaml"])
    block = _set_project_values(block, name, values)
    return "\n".join(lines[:head] + [block] + lines[end:])


def set_show_requests(enabled, local_dir=DEFAULT_LOCAL_DIR):
    """Shows open requests on the dashboard, or hides them, in `kandu.yaml`'s `dashboard` block, line by line."""
    path = Path(local_dir) / "kandu.yaml"
    text = _set_top_block(path.read_text(encoding="utf-8"), "dashboard",
                          {"show_requests": "true" if enabled else "false"})
    if ((yaml.safe_load(text) or {}).get("dashboard") or {}).get("show_requests") is not enabled:
        raise ConfigError(["dashboard.show_requests: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


def set_default_look(look, local_dir=DEFAULT_LOCAL_DIR):
    """Sets the install's default look in `kandu.yaml`'s `dashboard` block, line by line. Nothing is written if it is
    not one of LOOKS."""
    problems = []
    _check_one_of(look, LOOKS, "dashboard.look", problems)
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "kandu.yaml"
    text = _set_top_block(path.read_text(encoding="utf-8"), "dashboard", {"look": look})
    if ((yaml.safe_load(text) or {}).get("dashboard") or {}).get("look") != look:
        raise ConfigError(["dashboard.look: could not be written; change it in kandu.yaml by hand"])
    _replace_file(path, text)


# What the dashboard's settings tab changes in a project's entry, and the kind of each value. The next cycle uses them.
REGISTRY_SETTINGS = {"priority": int, "daily_token_limit": int, "auto_merge": bool, "frozen": bool, "multi_user": bool}
# The supervisor's pace in `kandu.yaml`. A new wake interval reaches the timer on "Restart Kandu".
PACE_SETTINGS = ("wake_every_minutes", "max_cycle_minutes")


def _yaml_scalar(value):
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _check_values(values, kinds, where, problems):
    for key, value in values.items():
        kind = kinds.get(key)
        if kind is None:
            problems.append(f"{where}{key}: not a setting the dashboard changes")
        elif type(value) is not kind:
            problems.append(f"{where}{key}: must be {'true or false' if kind is bool else 'a whole number'}")
        elif kind is int:
            _check_positive(value, f"{where}{key}", problems)


def _check_on_copy(local_dir, name, text):
    """Loads the config as it would be with `text` as `name`, in a folder of its own. Raises ConfigError."""
    local_dir = Path(local_dir)
    with tempfile.TemporaryDirectory() as tmp:
        for file in ("kandu.yaml", "projects.yaml"):
            shutil.copy(local_dir / file, Path(tmp) / file)
        (Path(tmp) / name).write_text(text, encoding="utf-8")
        return load_config(tmp)


def _set_project_values(text, name, values):
    """Sets each `key: value` in this project's entry only, keeping a comment beside it, since it says what the key
    is for. A key the entry lacks is added after its last line."""
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*- name:", l)]
    start = next((i for i in starts if re.fullmatch(rf"\s*- name:\s*[\"']?{re.escape(name)}[\"']?\s*(#.*)?", lines[i])), None)
    if start is None:
        raise ConfigError([f"projects: no entry named {name!r} in projects.yaml"])
    pad = " " * (len(lines[start]) - len(lines[start].lstrip())) + "  "
    for key, value in values.items():
        end = next((i for i in starts if i > start), len(lines))
        line = re.compile(rf"^({re.escape(pad)}{key}:[ \t]*)([^#]*?)([ \t]+#.*)?$")
        hits = [i for i in range(start + 1, end) if line.match(lines[i])]
        if len(hits) > 1:
            raise ConfigError([f"{name}.{key}: expected one such line in its entry, found {len(hits)}"])
        if hits:
            found, written = line.match(lines[hits[0]]), _yaml_scalar(value)
            comment = found.group(3) or ""
            if comment:  # it stays in its column, as long as the value leaves room
                width = len(found.group(2)) + len(comment) - len(comment.lstrip())
                comment = " " * max(1, width - len(written)) + comment.lstrip()
            lines[hits[0]] = f"{pad}{key}: {written}{comment}"
        else:
            last = max(i for i in range(start, end) if lines[i].strip() and not lines[i].lstrip().startswith("#"))
            lines.insert(last + 1, f"{pad}{key}: {_yaml_scalar(value)}")
            starts = [i + 1 if i > last else i for i in starts]
    return "\n".join(lines)


def set_project_values(name, values, local_dir=DEFAULT_LOCAL_DIR):
    """Changes a project's priority, daily tokens, auto-merge, freeze or multi_user in `projects.yaml`, line by line.

    The whole config is checked on a copy first, so nothing is written if any value is wrong.
    """
    problems = []
    _check_values(values, REGISTRY_SETTINGS, f"{name}.", problems)
    if not values:
        problems.append(f"{name}: give a setting to change")
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "projects.yaml"
    text = _set_project_values(path.read_text(encoding="utf-8"), name, values)
    _check_on_copy(local_dir, "projects.yaml", text)
    _replace_file(path, text)


def set_project_model(name, role, model, local_dir=DEFAULT_LOCAL_DIR):
    """Sets the model one role runs on for one project, in its entry's `models` block in `projects.yaml`, line by
    line; None takes the line out, so the role runs on `claude.models` again. The next run uses it.

    The whole config is checked on a copy first, so nothing is written if the role or the model is wrong.
    """
    problems = []
    _check_one_of(role, PROJECT_MODEL_ROLES, f"{name}.models", problems)
    if model is not None and not MODEL.fullmatch(model):
        problems.append(f"{name}.models.{role}: not a model name: {model!r}")
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "projects.yaml"
    text = _set_project_map_value(path.read_text(encoding="utf-8"), name, "models", role,
                                  None if model is None else json.dumps(model))
    config = _check_on_copy(local_dir, "projects.yaml", text)
    project = next(p for p in config.projects if p.name == name)
    if project.models.get(role) != model:
        raise ConfigError([f"{name}.models.{role}: could not be written; change it in projects.yaml by hand"])
    _replace_file(path, text)


def _set_project_map_value(text, name, block, key, value):
    """Sets `key: value` under the `block:` of this project's entry, adding the block when it is missing; a value of
    None takes the key out, and the block with it once nothing is left in it."""
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*- name:", l)]
    start = next((i for i in starts if re.fullmatch(rf"\s*- name:\s*[\"']?{re.escape(name)}[\"']?\s*(#.*)?", lines[i])), None)
    if start is None:
        raise ConfigError([f"projects: no entry named {name!r} in projects.yaml"])
    end = next((i for i in starts if i > start), len(lines))
    key_indent = lines[start].index("name:")
    found = [i for i in range(start + 1, end) if re.match(rf"^ {{{key_indent}}}{block}:", lines[i])]
    if not found:
        if value is None:
            return text
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
                                   and len(lines[end - 1]) - len(lines[end - 1].lstrip()) <= key_indent):
            end -= 1  # The block goes after the entry's last key, before blank lines and the next entry's comment.
        pad = " " * key_indent
        return "\n".join(lines[:end] + [f"{pad}{block}:", f"{pad}  {key}: {value}"] + lines[end:])
    head = found[0]
    if re.match(rf"^ {{{key_indent}}}{block}:\s*[^\s#]", lines[head]):
        raise ConfigError([f"{name}.{block}: written on one line; change it in projects.yaml by hand"])
    block_end = head + 1
    while block_end < end and (not lines[block_end].strip()
                               or len(lines[block_end]) - len(lines[block_end].lstrip()) > key_indent):
        block_end += 1
    children = [i for i in range(head + 1, block_end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    child = " " * (len(lines[children[0]]) - len(lines[children[0]].lstrip())) if children else " " * (key_indent + 2)
    hit = next((i for i in children if re.match(rf"^{child}{re.escape(key)}:", lines[i])), None)
    if value is None:
        if hit is None:
            return text
        if len(children) == 1:
            del lines[head:hit + 1]  # the block goes with its last key; a blank line after it stays
        else:
            del lines[hit]
    elif hit is None:
        lines.insert(head + 1, f"{child}{key}: {value}")
    else:
        lines[hit] = f"{child}{key}: {value}"  # Its old comment goes: it spoke of the old value.
    return "\n".join(lines)


def set_pace(values, local_dir=DEFAULT_LOCAL_DIR):
    """Changes how often the supervisor wakes and how long a cycle may run, in `kandu.yaml`'s `supervisor` block."""
    problems = []
    _check_values(values, dict.fromkeys(PACE_SETTINGS, int), "supervisor.", problems)
    if not values:
        problems.append("supervisor: give wake_every_minutes or max_cycle_minutes")
    if problems:
        raise ConfigError(problems)
    path = Path(local_dir) / "kandu.yaml"
    text = _set_top_block(path.read_text(encoding="utf-8"), "supervisor", values)
    _check_on_copy(local_dir, "kandu.yaml", text)
    _replace_file(path, text)


def set_parallel(total=None, projects=None, local_dir=DEFAULT_LOCAL_DIR):
    """Changes how many workers build at once: in all of Kandu, and in each project named.

    Both files are changed line by line, so their comments stay; nothing is written if any value is wrong.
    """
    projects = projects or {}
    problems = []
    if total is not None:
        _check_positive(total, "supervisor.max_parallel_workers", problems)
    for name, value in projects.items():
        _check_positive(value, f"{name}.max_parallel_workers", problems)
    if problems:
        raise ConfigError(problems)
    local_dir = Path(local_dir)
    settings_path, registry_path = local_dir / "kandu.yaml", local_dir / "projects.yaml"
    settings = _set_total_workers(settings_path.read_text(encoding="utf-8"), total) if total is not None else None
    registry = registry_path.read_text(encoding="utf-8")
    for name, value in projects.items():
        registry = _set_project_workers(registry, name, value)
    if settings is not None:
        _replace_file(settings_path, settings)
    if projects:
        _replace_file(registry_path, registry)


def load_config(local_dir=DEFAULT_LOCAL_DIR):
    """Returns a validated Config, or raises ConfigError listing every problem."""
    local_dir = Path(local_dir)
    problems = []
    raw_settings = _load_yaml(local_dir / "kandu.yaml", problems)
    raw_registry = _load_yaml(local_dir / "projects.yaml", problems)
    if problems:
        raise ConfigError(problems)

    settings = _build(Settings, raw_settings, "", problems)
    registry = _build(Registry, raw_registry, "", problems)
    if problems:
        raise ConfigError(problems)

    _check_settings(settings, problems)
    _check_projects(registry.projects, settings, problems)
    _check_upstream(settings, registry.projects, problems)
    if problems:
        raise ConfigError(problems)
    for project in registry.projects:
        if project.multi_user:
            project.board = replace(project.board, assignee=settings.github.account)
    return Config(settings=settings, projects=registry.projects)


def load_didban(local_dir=DEFAULT_LOCAL_DIR):
    """Returns the validated local/didban.yaml, or raises ConfigError."""
    problems = []
    raw = _load_yaml(Path(local_dir) / "didban.yaml", problems)
    if problems:
        raise ConfigError(problems)
    didban = _build(DidbanConfig, raw, "", problems)
    if not problems and not SSH_REACH.fullmatch(didban.brain.reach):
        problems.append(f"brain.reach: must be 'ssh <alias>', got {didban.brain.reach!r}")
    if problems:
        raise ConfigError(problems)
    return didban


def main():
    parser = argparse.ArgumentParser(description="Validate Kandu's config.")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"{len(e.problems)} problem(s) in config:", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"config ok: {len(config.settings.machines)} machine(s), {len(config.projects)} project(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
