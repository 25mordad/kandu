"""A task's `Reference` and the read-only copy of it that reaches a run (DESIGN §9).

A task's `## Reference` section names a repo and paths, and what is to be copied or matched. When the repo is another
project's in the registry, Kandu copies only those paths, from that project's copy on its machine, into a folder of
the run's own, and tells the worker and its reviewers where it is. This module reads the section and decides what may
be copied; `Runner` does the copying and the removal.

The section is read like this: a line `Repo: owner/name` starts a reference; the paths are the `code` words on the
lines that follow which start with `Path:` or `Paths:` or are list items. Anything else is for the reader.
"""
import posixpath
import re
from dataclasses import dataclass, field

from .github import GitHubError

SECTION = re.compile(r"^## Reference\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
REPO_LINE = re.compile(r"^\W*Repos?\W*:\s*`?([\w.-]+/[\w.-]+)`?", re.IGNORECASE)
PATH_LINE = re.compile(r"^\s*(?:[-*+]\s|Paths?\s*:)", re.IGNORECASE)
CODE = re.compile(r"`([^`\s]+)`")

# What a note on the task says. None names a repo or a path: the issue may be on a public repo.
NOT_IN_REGISTRY = "its repo is not a project in this install's registry, so nothing was copied"
PRIVATE_INTO_PUBLIC = "its repo is private and this task's repo is public, so nothing was copied"
VISIBILITY_UNKNOWN = "Kandu could not tell whether its repo is private, so nothing was copied"
NO_PATHS = "it names no path to copy"
UNSAFE_PATH = "a path it names is not a plain path inside the repo, so it was left out"
MISSING_PATH = "a path it names is not in the source project's copy, so it was left out"
UNREACHABLE = "the machine that holds its source did not answer, so nothing was copied"
UNREADABLE = "its source could not be read, so nothing was copied"
TOO_BIG = "its paths are larger than the limit for a copy, so nothing was copied"


@dataclass
class Reference:
    """One reference of a task: a repo and the paths named under it."""
    repo: str
    paths: list = field(default_factory=list)


def references_in(body):
    """The references in an issue's `## Reference` section, in order."""
    match = SECTION.search(body or "")
    found = []
    for line in (match.group(1) if match else "").splitlines():
        repo = REPO_LINE.match(line)
        if repo:
            found.append(Reference(repo.group(1)))
        elif found and PATH_LINE.match(line):
            found[-1].paths += CODE.findall(line)
    return found


def safe_path(path):
    """A path inside the repo: relative, no `..`, and never git's own folder."""
    normal = posixpath.normpath(path)
    parts = normal.split("/")
    return (not path.startswith(("/", "~", "-")) and normal != "." and ".." not in parts
            and ".git" not in parts and "\\" not in path and "\0" not in path)


def project_of(registry, repo):
    """The registered project that holds `repo`, or None."""
    return next((p for p in registry if repo.lower() in (r.lower() for r in p.repos)), None)


def folder_name(repo):
    """Where a repo's paths go inside the run's reference folder."""
    return repo.replace("/", "__")


def plan(task_repo, body, registry, is_public):
    """What to copy for a task: (list of (source project, Reference, its number in the issue), notes).

    A reference to the task's own repo is skipped: the worker already has it. One that is not in the registry, or
    whose private repo would reach a run on a public repo, is refused, and a note says why; so is a path that is not a
    plain path in its repo.
    """
    wanted, notes = [], []
    for number, reference in enumerate(references_in(body), 1):
        if reference.repo.lower() == task_repo.lower():
            continue
        project = project_of(registry, reference.repo)
        if project is None:
            notes.append(f"Reference {number}: {NOT_IN_REGISTRY}.")
            continue
        try:
            if is_public(task_repo) and not is_public(reference.repo):
                notes.append(f"Reference {number}: {PRIVATE_INTO_PUBLIC}.")
                continue
        except GitHubError:
            notes.append(f"Reference {number}: {VISIBILITY_UNKNOWN}.")
            continue
        paths = [p for p in dict.fromkeys(reference.paths) if safe_path(p)]
        if len(paths) < len(set(reference.paths)):
            notes.append(f"Reference {number}: {UNSAFE_PATH}.")
        if not paths:
            if not reference.paths:
                notes.append(f"Reference {number}: {NO_PATHS}.")
            continue
        wanted.append((project, Reference(reference.repo, paths), number))
    return wanted, notes


def prompt_section(folder, copied):
    """What the worker's and the reviewers' prompts say about the copy. `copied` is a list of (repo, paths)."""
    lines = ["## Reference copy",
             f"Kandu put a read-only copy of the files this task's `Reference` names in `{folder}`, one folder per "
             "repo. Read them to build or check against; never change them, and never commit or push anything from "
             "there. The folder is removed when this run ends."]
    for repo, paths in copied:
        lines.append(f"- `{repo}` is in `{posixpath.join(folder, folder_name(repo))}`: "
                     + ", ".join(f"`{p}`" for p in paths))
    return "\n".join(lines)


def read_rule(folder):
    """The allow rule that lets a limited role read the copy and nothing else outside its checkout."""
    return f"Read(/{folder.rstrip('/')}/**)"
