"""Everything Kandu reads from and writes to GitHub, through `gh` (DESIGN §3, §10)."""
import json
import re
import subprocess
from dataclasses import dataclass

KANDU_LABEL = "kandu"
AGENT_LABEL = "agent"
FALLBACK_SLUG = "task"
TIMEOUT_SECONDS = 60
BOARD_ITEM_LIMIT = 500
PR_SEARCH_LIMIT = 200
PR_FIELDS = "number,title,state,url,body,headRefName,headRefOid,isDraft,isCrossRepository,labels,comments"
BLOCKED_BY = re.compile(r"^## Blocked by\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
ISSUE_REFERENCE = re.compile(r"(?:(?<![\w.-])([\w.-]+/[\w.-]+))?#(\d+)")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


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
    def __init__(self, run=run_gh):
        self._run = run

    def _json(self, args):
        return json.loads(self._run(args))

    def board_items(self, board):
        """Returns the issues on a board; draft items and PRs are not tasks."""
        data = self._json(["project", "item-list", str(board.number), "--owner", board.owner,
                           "--format", "json", "-L", str(BOARD_ITEM_LIMIT)])
        names = {theirs: ours for ours, theirs in board.columns.items()}
        items = []
        for item in data["items"]:
            content = item.get("content", {})
            if content.get("type") != "Issue":
                continue
            items.append(BoardItem(
                repo=content["repository"], number=content["number"], title=content["title"],
                url=content["url"], status=names.get(item.get("status", ""), item.get("status", "")), labels=item.get("labels") or [],
                body=content.get("body", ""),
            ))
        return items

    def set_status(self, board, url, status):
        self._run(["project", "item-edit", str(board.number), "--owner", board.owner,
                   "--url", url, "--field", "Status", "--value", board.columns.get(status, status)])

    def add_to_board(self, board, url):
        self._run(["project", "item-add", str(board.number), "--owner", board.owner, "--url", url])

    def board_columns(self, board):
        """Returns the option names of the board's `Status` field."""
        data = self._json(["project", "field-list", str(board.number), "--owner", board.owner, "--format", "json"])
        status = next((f for f in data["fields"] if f["name"] == "Status"), {})
        return [option["name"] for option in status.get("options", [])]

    def create_board(self, owner, title, columns):
        """Creates a board and gives its `Status` field exactly these columns."""
        board = self._json(["project", "create", "--owner", owner, "--title", title, "--format", "json"])
        fields = self._json(["project", "field-list", str(board["number"]), "--owner", owner, "--format", "json"])
        status = next(f for f in fields["fields"] if f["name"] == "Status")
        options = ", ".join(f'{{name: {json.dumps(c)}, color: GRAY, description: ""}}' for c in columns)
        query = ("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
                 "{ clientMutationId } }" % (json.dumps(status["id"]), options))
        self._run(["api", "graphql", "-f", f"query={query}"])
        return board["number"]

    def create_issue(self, repo, title, body):
        """Creates an issue labelled `agent` and returns its URL."""
        self._run(["label", "create", AGENT_LABEL, "--repo", repo, "--force",
                   "--description", "Kandu may work on this"])
        return self._run(["issue", "create", "--repo", repo, "--title", title, "--body", body,
                          "--label", AGENT_LABEL]).strip()

    def repo_is_public(self, repo):
        return self._json(["repo", "view", repo, "--json", "visibility"])["visibility"] == "PUBLIC"

    def issue_is_open(self, repo, number):
        data = self._json(["issue", "view", str(number), "--repo", repo, "--json", "state"])
        return data["state"] == "OPEN"

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
