"""Everything Kandu reads from and writes to GitHub, through `gh` (DESIGN §3, §10)."""
import fcntl
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import BOARD_FIELDS, DEADLINE_FIELD

KANDU_LABEL = "kandu"
AGENT_LABEL = "agent"
TOKENS_LABEL = "uses-tokens"
FALLBACK_SLUG = "task"
TIMEOUT_SECONDS = 60
BOARD_ITEM_LIMIT = 500
BOARD_CACHE_DIR = Path(__file__).resolve().parent.parent / "state" / "boards"
BOARD_CACHE_SECONDS = 60
PR_SEARCH_LIMIT = 200
PR_FIELDS = "number,title,state,url,body,changedFiles,files,headRefName,headRefOid,isDraft,isCrossRepository,labels,comments,mergeable,statusCheckRollup"
BLOCKED_BY = re.compile(r"^## Blocked by\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
ISSUE_REFERENCE = re.compile(r"(?:(?<![\w.-])([\w.-]+/[\w.-]+))?#(\d+)")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { reviewThreads(first: 100) { nodes {
    isResolved resolvedBy { login } comments(first: 1) { nodes { author { login } } }
  } } } }
}"""

SELECT_VALUE = "... on ProjectV2ItemFieldSingleSelectValue { name }"
# `fieldValueByName` is what keeps this at one point a page: `gh project item-list` costs 100.
BOARD_QUERY = f"""query($owner: String!, $number: Int!, $after: String) {{
  repositoryOwner(login: $owner) {{ ... on ProjectV2Owner {{ projectV2(number: $number) {{ items(first: 100, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      status: fieldValueByName(name: "Status") {{ {SELECT_VALUE} }}
      priority: fieldValueByName(name: "Priority") {{ {SELECT_VALUE} }}
      kind: fieldValueByName(name: "Kind") {{ {SELECT_VALUE} }}
      size: fieldValueByName(name: "Size") {{ {SELECT_VALUE} }}
      deadline: fieldValueByName(name: "{DEADLINE_FIELD}") {{ ... on ProjectV2ItemFieldDateValue {{ date }} }}
      content {{ ... on Issue {{ number title url body closedAt repository {{ nameWithOwner }} labels(first: 50) {{ nodes {{ name }} }} }} }}
    }}
  }} }} }} }}
}}"""


class GitHubError(Exception):
    pass


@dataclass
class BoardItem:
    repo: str
    number: int
    title: str
    url: str
    status: str
    labels: list
    body: str
    priority: str = ""
    kind: str = ""
    size: str = ""
    deadline: str = ""
    closed_at: str = ""


class BoardCache:
    """One copy of each board on disk, shared by every Kandu process (DESIGN §3).

    GitHub stays the source of truth: a write goes there first and lands here after.
    """

    def __init__(self, root=BOARD_CACHE_DIR, clock=time.time):
        self.root = Path(root)
        self.clock = clock

    def _path(self, board):
        return self.root / f"{board.owner}-{board.number}.json"

    @contextmanager
    def locked(self, board):
        """Held during a read of GitHub, so a process that arrives meanwhile gets its result."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._path(board).with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _load(self, board):
        try:
            data = json.loads(self._path(board).read_text(encoding="utf-8"))
            return data["read_at"], [BoardItem(**item) for item in data["items"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _store(self, board, read_at, items):
        temporary = self._path(board).with_suffix(".tmp")
        temporary.write_text(json.dumps({"read_at": read_at, "items": [asdict(i) for i in items]}), encoding="utf-8")
        os.replace(temporary, self._path(board))

    def version(self, board):
        """Changes with every write of the copy, whichever process made it."""
        try:
            return self._path(board).stat().st_mtime_ns
        except OSError:
            return None

    def fresh(self, board):
        """The board's items, or None if the copy is missing or too old."""
        found = self._load(board)
        if found is None or not 0 <= self.clock() - found[0] < BOARD_CACHE_SECONDS:
            return None
        return found[1]

    def save(self, board, items):
        self._store(board, self.clock(), items)

    def update(self, board, url, changes):
        """Puts a write GitHub accepted into the copy. The copy keeps its age."""
        with self.locked(board):
            found = self._load(board)
            if found is None:
                return
            read_at, items = found
            for item in items:
                if item.url == url:
                    for name, value in changes.items():
                        setattr(item, name, value)
            self._store(board, read_at, items)

    def forget(self, board=None):
        """For a change the copy cannot follow. Without a board it drops them all: an issue has no board of its own."""
        for path in [self._path(board)] if board else self.root.glob("*.json"):
            path.unlink(missing_ok=True)


def run_gh(args):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GitHubError(f"gh {args[0]}: {type(e).__name__}") from e
    if result.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].rstrip("-") or FALLBACK_SLUG


def branch_for(number, title):
    return f"kandu/{number}-{slugify(title)}"


def blockers_in(body, repo):
    """Returns the (repo, number) pairs in an issue's `## Blocked by` section."""
    section = BLOCKED_BY.search(body or "")
    if section is None:
        return []
    text = HTML_COMMENT.sub("", section.group(1))
    return [(other or repo, int(number)) for other, number in ISSUE_REFERENCE.findall(text)]


def _branch_slug(branch):
    match = re.fullmatch(r"kandu/\d+-(.+)", branch)
    return match.group(1) if match else None


def is_companion(ours, ours_repo, theirs, theirs_repo):
    """DESIGN §5: a PR body names the other PR, or the branches have the same slug."""
    slug = _branch_slug(ours["headRefName"])
    if slug not in (None, FALLBACK_SLUG) and slug == _branch_slug(theirs["headRefName"]):
        return True

    def names(pr, repo, number):
        return bool(re.search(rf"{re.escape(repo)}(?:#|/pull/){number}\b", pr["body"] or ""))

    return names(ours, theirs_repo, theirs["number"]) or names(theirs, ours_repo, ours["number"])


class GitHub:
    def __init__(self, run=run_gh, cache=None):
        self._run = run
        self._cache = cache

    def _json(self, args):
        return json.loads(self._run(args))

    def board_version(self, board):
        """Changes when any process changes the shared copy of the board. Asks GitHub nothing."""
        return None if self._cache is None else self._cache.version(board)

    def board_items(self, board):
        """Returns the issues on a board, from the shared copy while it is fresh."""
        if self._cache is None:
            return self._read_board(board)
        with self._cache.locked(board):
            items = self._cache.fresh(board)
            if items is None:
                items = self._read_board(board)
                self._cache.save(board, items)
            return items

    def _read_board(self, board):
        """Draft items and PRs are not tasks."""
        names = {theirs: ours for ours, theirs in board.columns.items()}
        items, after = [], []
        while True:
            data = self._json(["api", "graphql", "-f", f"query={BOARD_QUERY}", "-f", f"owner={board.owner}",
                               "-F", f"number={board.number}", *after])
            page = data["data"]["repositoryOwner"]["projectV2"]["items"]
            for node in page["nodes"]:
                content = node.get("content") or {}
                if "number" not in content:
                    continue
                value = lambda name, key="name": (node.get(name) or {}).get(key, "")
                items.append(BoardItem(repo=content["repository"]["nameWithOwner"], number=content["number"],
                                       title=content["title"], url=content["url"],
                                       status=names.get(value("status"), value("status")),
                                       labels=[label["name"] for label in content["labels"]["nodes"]],
                                       body=content.get("body") or "", priority=value("priority"), kind=value("kind"),
                                       size=value("size"), deadline=value("deadline", "date"),
                                       closed_at=content.get("closedAt") or ""))
            if not page["pageInfo"]["hasNextPage"] or len(items) >= BOARD_ITEM_LIMIT:
                return items
            after = ["-f", f"after={page['pageInfo']['endCursor']}"]

    def set_status(self, board, url, status):
        self._run(["project", "item-edit", str(board.number), "--owner", board.owner,
                   "--url", url, "--field", "Status", "--value", board.columns.get(status, status)])
        if self._cache is not None:
            self._cache.update(board, url, {"status": status})

    def set_field(self, board, url, field, value):
        flag = "--date" if field == DEADLINE_FIELD else "--value"
        self._run(["project", "item-edit", str(board.number), "--owner", board.owner,
                   "--url", url, "--field", field, flag, value])
        if self._cache is not None:
            self._cache.update(board, url, {field.lower(): value})

    def add_to_board(self, board, url):
        self._run(["project", "item-add", str(board.number), "--owner", board.owner, "--url", url])
        if self._cache is not None:
            self._cache.forget(board)

    def board_columns(self, board):
        """Returns the option names of the board's `Status` field."""
        data = self._json(["project", "field-list", str(board.number), "--owner", board.owner, "--format", "json"])
        status = next((f for f in data["fields"] if f["name"] == "Status"), {})
        return [option["name"] for option in status.get("options", [])]

    def board_fields(self, board):
        """Returns the names of the board's fields."""
        data = self._json(["project", "field-list", str(board.number), "--owner", board.owner, "--format", "json"])
        return [field["name"] for field in data["fields"]]

    def create_board(self, owner, title, columns):
        """Creates a board and gives its `Status` field exactly these columns."""
        board = self._json(["project", "create", "--owner", owner, "--title", title, "--format", "json"])
        fields = self._json(["project", "field-list", str(board["number"]), "--owner", owner, "--format", "json"])
        status = next(f for f in fields["fields"] if f["name"] == "Status")
        options = ", ".join(f'{{name: {json.dumps(c)}, color: GRAY, description: ""}}' for c in columns)
        query = ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
                 "{ clientMutationId } }" % (json.dumps(status["id"]), options))
        self._run(["api", "graphql", "-f", f"query={query}"])
        for name, values in BOARD_FIELDS.items():
            self._run(["project", "field-create", str(board["number"]), "--owner", owner, "--name", name,
                       "--data-type", "SINGLE_SELECT", "--single-select-options", ",".join(values)])
        self._run(["project", "field-create", str(board["number"]), "--owner", owner, "--name", DEADLINE_FIELD,
                   "--data-type", "DATE"])
        return board["number"]

    def add_columns(self, board, names):
        """Adds columns to a board. Each existing option keeps its id, or its items would lose their column."""
        data = self._json(["project", "field-list", str(board.number), "--owner", board.owner, "--format", "json"])
        status = next(f for f in data["fields"] if f["name"] == "Status")
        query = ("query { node(id: %s) { ... on ProjectV2SingleSelectField { options { id name color description } } } }"
                 % json.dumps(status["id"]))
        options = self._json(["api", "graphql", "-f", f"query={query}"])["data"]["node"]["options"]
        kept = [f'{{id: {json.dumps(o["id"])}, name: {json.dumps(o["name"])}, color: {o["color"]}, '
                f'description: {json.dumps(o["description"])}}}' for o in options]
        added = [f'{{name: {json.dumps(name)}, color: GRAY, description: ""}}' for name in names]
        query = ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
                 "{ clientMutationId } }" % (json.dumps(status["id"]), ", ".join(kept + added)))
        self._run(["api", "graphql", "-f", f"query={query}"])

    def create_issue(self, repo, title, body, by_hand=False, tokens=False):
        """Creates an issue and returns its URL. It is labelled `agent` unless a person will do it.

        `tokens` also labels it `uses-tokens`, which gives its worker the project's `task_tokens`.
        """
        if by_hand:
            return self._run(["issue", "create", "--repo", repo, "--title", title, "--body", body]).strip()
        self._run(["label", "create", AGENT_LABEL, "--repo", repo, "--force",
                   "--description", "Kandu may work on this"])
        labels = ["--label", AGENT_LABEL]
        if tokens:
            self._run(["label", "create", TOKENS_LABEL, "--repo", repo, "--force",
                       "--description", "Its worker gets the project's task_tokens"])
            labels += ["--label", TOKENS_LABEL]
        return self._run(["issue", "create", "--repo", repo, "--title", title, "--body", body, *labels]).strip()

    def edit_issue(self, repo, number, title, body):
        self._run(["issue", "edit", str(number), "--repo", repo, "--title", title, "--body", body])
        if self._cache is not None:
            self._cache.forget()

    def repo_is_public(self, repo):
        return self._json(["repo", "view", repo, "--json", "visibility"])["visibility"] == "PUBLIC"

    def issue_text(self, repo, number):
        return self._json(["issue", "view", str(number), "--repo", repo, "--json", "title,body,url"])

    def issue_is_open(self, repo, number):
        data = self._json(["issue", "view", str(number), "--repo", repo, "--json", "state"])
        return data["state"] == "OPEN"

    def close_issue(self, repo, number):
        self._run(["issue", "close", str(number), "--repo", repo])
        if self._cache is not None:
            self._cache.forget()

    def comment_issue(self, repo, number, body):
        self._run(["issue", "comment", str(number), "--repo", repo, "--body", body])

    def default_branch(self, repo):
        data = self._json(["repo", "view", repo, "--json", "defaultBranchRef"])
        return data["defaultBranchRef"]["name"]

    def find_pr(self, repo, issue):
        """Returns the newest PR of an issue's `kandu/` branch in this repo, or None. A fork's PR never counts."""
        found = self._json(["pr", "list", "--repo", repo, "--state", "all", "--limit", str(PR_SEARCH_LIMIT),
                            "--json", "number,headRefName,isCrossRepository"])
        numbers = [pr["number"] for pr in found
                   if pr["headRefName"].startswith(f"kandu/{issue}-") and not pr["isCrossRepository"]]
        return self.pr(repo, max(numbers)) if numbers else None

    def pr(self, repo, number):
        return self._json(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])

    def review_threads(self, repo, number):
        owner, name = repo.split("/")
        data = self._json(["api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                           "-F", f"number={number}"])
        threads = []
        for node in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            comments = node["comments"]["nodes"]
            author = (comments[0].get("author") or {}).get("login") if comments else None
            resolver = (node.get("resolvedBy") or {}).get("login")
            threads.append({"resolved": node["isResolved"], "resolved_by_other": bool(resolver) and resolver != author})
        return threads

    def merge_pr(self, repo, number, head_sha):
        """GitHub refuses this if the head is no longer `head_sha`."""
        self._run(["pr", "merge", str(number), "--repo", repo, "--squash", "--match-head-commit", head_sha])

    def comment_pr(self, repo, number, body):
        self._run(["pr", "comment", str(number), "--repo", repo, "--body", body])

    def mark_pr(self, repo, pr):
        """Adds the `kandu` label, creating it in the repo if needed."""
        if any(label["name"] == KANDU_LABEL for label in pr["labels"]):
            return
        self._run(["label", "create", KANDU_LABEL, "--repo", repo, "--force",
                   "--description", "Work done by Kandu"])
        self._run(["pr", "edit", str(pr["number"]), "--repo", repo, "--add-label", KANDU_LABEL])

    def companions(self, repo, pr, repos):
        """Returns the open companion PRs of `pr` in `repos`, as (repo, number) pairs."""
        found = []
        for other in repos:
            others = self._json(["pr", "list", "--repo", other, "--state", "open",
                                 "--json", "number,headRefName,body,isCrossRepository"])
            for candidate in others:
                same_pr = other == repo and candidate["number"] == pr["number"]
                if not same_pr and not candidate["isCrossRepository"] and is_companion(pr, repo, candidate, other):
                    found.append((other, candidate["number"]))
        return found
