"""Loads and validates local/kandu.yaml and local/projects.yaml (DESIGN §4, §12).

Every problem is reported at once, so a config can be fixed in one pass.
Paths are kept as written: a `~` may belong to another machine.
"""
import argparse
import posixpath
import re
import sys
import types
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

import yaml

DEFAULT_LOCAL_DIR = Path(__file__).resolve().parent.parent / "local"
SSH_REACH = re.compile(r"ssh \w[\w.@-]*")
BOARD_COLUMNS = ("Ready", "In progress", "In review", "Needs info", "Blocked", "Done")


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


@dataclass
class ClaudeLimits:
    session_5h_percent: int
    weekly_percent: int


@dataclass
class Claude:
    auth: str
    limits: ClaudeLimits
    login_warn_days: int = 7


@dataclass
class GitHub:
    account: str
    owners: list[str]


@dataclass
class Telegram:
    bot_token_env: str
    chat_id_env: str


@dataclass
class Notify:
    adapter: str
    language: str
    telegram: Telegram | None = None


@dataclass
class Supervisor:
    wake_every_minutes: int = 60
    max_cycle_minutes: int = 50
    max_parallel_workers: int = 2
    max_review_rounds: int = 2
    auto_merge: bool = False
    auto_revert: bool = False


@dataclass
class Dashboard:
    bind: str
    port: int


@dataclass
class Board:
    owner: str
    number: int
    columns: dict[str, str] = field(default_factory=dict)  # Kandu's column name -> this board's, if different


@dataclass
class Deploy:
    mode: str
    command: str
    live_url: str = ""             # empty: nothing can check the live software, for example behind a login


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
    description: str = ""
    board: Board | None = None
    run_local: str | None = None
    deploy: Deploy | None = None
    secrets_from: str | None = None
    forbidden: list[str] = field(default_factory=list)
    repo_rules: str = ""
    auto_merge: bool | None = None
    frozen: bool = False
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class Settings:
    brain: Brain
    machines: list[Machine]
    claude: Claude
    github: GitHub
    notify: Notify
    dashboard: Dashboard
    supervisor: Supervisor = field(default_factory=Supervisor)
    didban: Didban = field(default_factory=Didban)


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

    _check_one_of(s.notify.adapter, ("telegram", "none"), "notify.adapter", problems)
    if s.notify.adapter == "telegram" and s.notify.telegram is None:
        problems.append("notify.telegram: required when notify.adapter is telegram")

    for name in ("wake_every_minutes", "max_cycle_minutes", "max_parallel_workers", "max_review_rounds"):
        _check_positive(getattr(s.supervisor, name), f"supervisor.{name}", problems)
    if s.supervisor.auto_revert:
        problems.append("supervisor.auto_revert: must be false; Kandu only reports breakage (DESIGN §5)")
    if not 1 <= s.dashboard.port <= 65535:
        problems.append(f"dashboard.port: must be between 1 and 65535, got {s.dashboard.port}")


def _check_projects(projects, s, problems):
    machines = [m.name for m in s.machines]
    seen = set()
    family_boards = {}
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
        _check_one_of(p.run, ("clone", "in-place"), f"{where}.run", problems)
        if p.machine not in machines:
            problems.append(f"{where}.machine: unknown machine {p.machine!r}")
        if p.run == "clone" and p.machine != s.brain.machine:
            problems.append(f"{where}.machine: run: clone must be on the brain's machine {s.brain.machine!r}")
        if p.run == "clone" and not _is_inside(p.path, s.brain.work_dir):
            problems.append(f"{where}.path: run: clone must be inside brain.work_dir {s.brain.work_dir!r}, "
                            f"written the same way, got {p.path!r}")
        if p.deploy is not None:
            _check_one_of(p.deploy.mode, ("direct", "preview"), f"{where}.deploy.mode", problems)
        _check_positive(p.priority, f"{where}.priority", problems)
        _check_positive(p.max_parallel_workers, f"{where}.max_parallel_workers", problems)


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
    if problems:
        raise ConfigError(problems)
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
