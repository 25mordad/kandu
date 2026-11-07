"""The terms that must never reach a public repo: the user's own words plus what code finds in `local/` (DESIGN §16).

`local/private-words.txt` holds the words the user wrote. A project added later would slip past
it until the user thought of adding its name, so this also takes the terms of the setup itself:
each project's name, family, repos, board owner and path, each machine and how it is reached,
the brain's folders, Didban's knowledge sources and the values in `local/.env`.
The GitHub account is left out: it owns the public repo and signs its commits. So is a term
the public examples already use (`config.example/`, `README.md`), such as a machine named
`laptop`: it says nothing about this setup.

The git hooks in `.githooks` run it as a file with the install's own Python, so it imports
nothing of Kandu:

    <kandu>/.venv/bin/python -I <kandu>/kandu/privateterms.py <kandu>/local    # one term per line
"""
import re
import sys
from pathlib import Path

import yaml

MIN_LENGTH = 4                 # shorter terms match inside ordinary words
OWN_NAMES = {"kandu"}          # the tool's own name, everywhere in the public repo
HOME = re.compile(r"^(~|/home/[^/]+|/Users/[^/]+)/")


def _yaml(path):
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _words(path):
    if not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _env_values(path):
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip().strip("'\"")
        if len(value) >= 8:
            values.append(value)
    return values


def _path_terms(path):
    """A path in both of its spellings: `~/x/y` and `/home/<user>/x/y` mean the same folder."""
    path = str(path).strip().rstrip("/")
    rest = HOME.sub("", path)
    return [path] if rest == path else [path, f"~/{rest}", f"/{rest}" if "/" in rest else ""]


def _reach_terms(reach):
    """`ssh user@host` gives the user and the host; `local` gives nothing."""
    parts = str(reach).split()
    if not parts or parts[0] != "ssh":
        return []
    terms = []
    for part in parts[1:]:
        if part.startswith("-"):
            continue
        terms.extend(part.split("@"))
    return terms


def _public_text(install_dir):
    """What the public repo shows as examples, lower-cased."""
    install = Path(install_dir)
    files = [*sorted((install / "config.example").glob("*")), install / "README.md"]
    return "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files if f.is_file()).lower()


def config_terms(local_dir):
    """The terms of the setup in `local/`, without the user's own word list."""
    local = Path(local_dir)
    settings = _yaml(local / "kandu.yaml")
    registry = _yaml(local / "projects.yaml")
    account = str((settings.get("github") or {}).get("account") or "")
    terms = []
    brain = settings.get("brain") or {}
    terms += [brain.get("machine", "")]
    for key in ("work_dir", "install_dir"):
        if brain.get(key):
            terms += _path_terms(brain[key])
    for machine in settings.get("machines") or []:
        terms += [machine.get("name", ""), *_reach_terms(machine.get("reach", ""))]
    terms += [owner for owner in (settings.get("github") or {}).get("owners") or []]
    for source in (settings.get("didban") or {}).get("knowledge") or []:
        terms += [source.get("name", ""), *_path_terms(source.get("path", ""))]
    for project in registry.get("projects") or []:
        terms += [project.get("name", ""), project.get("family", ""), project.get("machine", "")]
        for repo in project.get("repos") or []:
            terms += [repo, *str(repo).split("/")]
        terms += [(project.get("board") or {}).get("owner", "")]
        if project.get("path"):
            terms += _path_terms(project["path"])
    terms += _env_values(local / ".env")
    own = {account.lower(), *OWN_NAMES}
    public = _public_text(local.resolve().parent)
    return [str(term) for term in terms
            if term and len(str(term)) >= MIN_LENGTH and str(term).lower() not in own and str(term).lower() not in public]


def all_terms(local_dir):
    """The user's words first, then the setup's, each once, ignoring case."""
    seen, terms = set(), []
    for term in [*_words(Path(local_dir) / "private-words.txt"), *config_terms(local_dir)]:
        term = str(term)
        if term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def main(argv):
    local_dir = argv[1] if len(argv) > 1 else "local"
    for term in all_terms(local_dir):
        print(term)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
