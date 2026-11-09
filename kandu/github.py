"""Everything Kandu reads from and writes to GitHub, through `gh` (DESIGN §3, §10)."""
import json
import re
import subprocess
import time
from dataclasses import dataclass, field

from . import ghmeter
from .config import BOARD_FIELDS, DEADLINE_FIELD

KANDU_LABEL = "kandu"
AGENT_LABEL = "agent"
TOKENS_LABEL = "uses-tokens"
FALLBACK_SLUG = "task"
TIMEOUT_SECONDS = 60
LOOK_AGAIN_SECONDS = 5  # after a lost answer to a create, before looking whether the issue was made
BOARD_ITEM_LIMIT = 500
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

# GitHub's own "blocked by" relationships of an issue: `task_blockers` joins them with `## Blocked by`.
BLOCKED_BY_NODE = "blockedBy(first: 50) { nodes { number repository { nameWithOwner } } }"
SELECT_VALUE = "... on ProjectV2ItemFieldSingleSelectValue { name }"
# `fieldValueByName` is what keeps this at one point a page: `gh project item-list` costs 100.
BOARD_QUERY = f"""query($owner: String!, $number: Int!, $after: String) {{
  repositoryOwner(login: $owner) {{ ... on ProjectV2Owner {{ projectV2(number: $number) {{
    id
    fields(first: 50) {{ nodes {{ ... on ProjectV2FieldCommon {{ id name }} ... on ProjectV2SingleSelectField {{ options {{ id name }} }} }} }}
    items(first: 100, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      id
      status: fieldValueByName(name: "Status") {{ {SELECT_VALUE} }}
      priority: fieldValueByName(name: "Priority") {{ {SELECT_VALUE} }}
      kind: fieldValueByName(name: "Kind") {{ {SELECT_VALUE} }}
      size: fieldValueByName(name: "Size") {{ {SELECT_VALUE} }}
      deadline: fieldValueByName(name: "{DEADLINE_FIELD}") {{ ... on ProjectV2ItemFieldDateValue {{ date }} }}
      content {{ ... on Issue {{ number title url body closedAt repository {{ nameWithOwner }} labels(first: 50) {{ nodes {{ name }} }} {BLOCKED_BY_NODE} }} }}
    }}
  }} }} }} }}
}}"""

COMMENTS = "comments(last: 100) { nodes { author { login } body createdAt } }"
# A new comment changes an issue's `updatedAt`, so reading what changed reads its comments too, in the same request.
ISSUE_NODE = f"number title body url state updatedAt labels(first: 30) {{ nodes {{ name }} }} {COMMENTS} {BLOCKED_BY_NODE}"
PR_NODE = ("number title state url body changedFiles headRefName headRefOid isDraft isCrossRepository mergeable updatedAt "
           f"labels(first: 30) {{ nodes {{ name }} }} {COMMENTS}")
# Only an open PR needs these. Checks and threads change without touching `updatedAt`, so open PRs are read whole each time.
PR_DEEP = """files(first: 100) { nodes { path additions deletions } }
  commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 50) { nodes {
    __typename ... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } } } }
  reviewThreads(first: 20) { nodes { isResolved resolvedBy { login } path line
    comments(first: 20) { nodes { author { login } body createdAt } } } }"""
# One request reads what a repo holds for Kandu: what changed in its issues, every open PR, the PRs that closed lately.
REPO_QUERY = f"""query($owner: String!, $name: String!, $since: DateTime, $issuesAfter: String, $openAfter: String,
      $issues: Boolean!, $open: Boolean!, $closed: Boolean!) {{
  repository(owner: $owner, name: $name) {{
    defaultBranchRef {{ name }}
    issues(first: 100, after: $issuesAfter, filterBy: {{since: $since}}, orderBy: {{field: UPDATED_AT, direction: DESC}})
        @include(if: $issues) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {ISSUE_NODE} }} }}
    open: pullRequests(first: 20, after: $openAfter, states: OPEN, orderBy: {{field: UPDATED_AT, direction: DESC}})
        @include(if: $open) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {PR_NODE} {PR_DEEP} }} }}
    closed: pullRequests(first: 30, states: [CLOSED, MERGED], orderBy: {{field: UPDATED_AT, direction: DESC}})
        @include(if: $closed) {{ nodes {{ {PR_NODE} }} }}
  }}
}}"""
ISSUE_QUERY = f"""query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ issue(number: $number) {{ {ISSUE_NODE} }} }}
}}"""
PR_QUERY = f"""query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ pullRequest(number: $number) {{ {PR_NODE} {PR_DEEP} }} }}
}}"""
REPO_ISSUE_LIMIT = 500
OPEN_PR_LIMIT = 100
CONTENT_QUERY = "query($url: URI!) { resource(url: $url) { ... on Issue { id } ... on PullRequest { id } } }"
ADD_ITEM = ("mutation($project: ID!, $content: ID!) { addProjectV2ItemById(input: {projectId: $project, contentId: $content}) "
            "{ item { id } } }")


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
    item_id: str = ""              # the card's own id on the board, which a write needs
    blocked_by: list = field(default_factory=list)  # GitHub's "blocked by" relationships: [{"repo", "number"}]

    @property
    def by_hand(self):
        """A task without `agent` is the user's to do by hand: Kandu never sizes, releases or works it."""
        return AGENT_LABEL not in self.labels

    @property
    def waits_by_hand(self):
        """A by-hand task in an open column waits for the user; one done or closed waits for nobody."""
        return self.by_hand and self.status != "Done" and not self.closed_at


TRANSIENT = re.compile(r"HTTP 5\d\d|\b50[234]\b|rate limit|secondary|abuse|too quickly|timed out|Timeout|"
                       r"temporarily|try again|connection|EOF", re.IGNORECASE)


def transient(error):
    """True for a refusal that passes by itself: GitHub busy, a limit, the network."""
    return bool(TRANSIENT.search(str(error)))


def _pr(node, deep):
    """A PR as `gh pr view --json` shows it, so every reader keeps working on the same fields."""
    pr = {key: node.get(key) for key in ("number", "title", "state", "url", "body", "changedFiles", "headRefName",
                                          "headRefOid", "isDraft", "isCrossRepository", "mergeable", "updatedAt")}
    pr["labels"] = [label for label in node["labels"]["nodes"]]
    pr["comments"] = _comments(node)
    pr["files"], pr["statusCheckRollup"], pr["reviewThreads"] = [], [], []
    if deep:
        pr["files"] = node["files"]["nodes"]
        commits = node["commits"]["nodes"]
        rollup = commits and (commits[0]["commit"].get("statusCheckRollup") or {})
        pr["statusCheckRollup"] = rollup["contexts"]["nodes"] if rollup else []
        for thread in node["reviewThreads"]["nodes"]:
            comments = thread["comments"]["nodes"]
            author = (comments[0].get("author") or {}).get("login") if comments else None
            resolver = (thread.get("resolvedBy") or {}).get("login")
            pr["reviewThreads"].append({"resolved": thread["isResolved"],
                                        "resolved_by_other": bool(resolver) and resolver != author,
                                        "path": thread.get("path"), "line": thread.get("line"),
                                        "comments": _comments(thread)})
    return pr


def _comments(node):
    return [{"author": c.get("author") or {}, "body": c.get("body") or "", "createdAt": c.get("createdAt")}
            for c in (node.get("comments") or {}).get("nodes", [])]


def _blocked_by(node):
    return [{"repo": n["repository"]["nameWithOwner"], "number": n["number"]}
            for n in (node.get("blockedBy") or {}).get("nodes", [])]


def _issue(node):
    return {"number": node["number"], "title": node["title"], "body": node.get("body") or "", "url": node["url"],
            "state": node["state"], "labels": [label["name"] for label in node["labels"]["nodes"]],
            "updatedAt": node.get("updatedAt"), "comments": _comments(node), "blocked_by": _blocked_by(node)}


def run_gh(args):
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, env=ghmeter.env())
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


def task_blockers(body, repo, blocked_by=()):
    """A task's blockers: the (repo, number) pairs under its `## Blocked by` and in its GitHub "blocked by"
    relationships (`[{"repo", "number"}]`), each once. GitHub's repo names ignore case, so a blocker named in
    both places counts once; the section's spelling is kept."""
    pairs = {}
    for other, number in [*blockers_in(body, repo), *((b["repo"], int(b["number"])) for b in blocked_by or ())]:
        pairs.setdefault((other.lower(), number), (other, number))
    return list(pairs.values())


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
    """Talks to GitHub directly, one call at a time. Kandu's own processes go through `mirror.Mirror` instead."""

    def __init__(self, run=run_gh, pause=time.sleep):
        self._run = run
        self._pause = pause

    def _json(self, args):
        return json.loads(self._run(args))

    def _graphql(self, query, **variables):
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            if value is None:
                continue
            flag = "-F" if isinstance(value, (bool, int)) else "-f"
            args += [flag, f"{name}={str(value).lower() if isinstance(value, bool) else value}"]
        return self._json(args)

    def board_items(self, board):
        return self.read_board(board)[0]

    def read_board(self, board):
        """Returns the issues on a board, and what a write needs: the board's id and its fields' ids.

        Draft items and PRs are not tasks.
        """
        names = {theirs: ours for ours, theirs in board.columns.items()}
        items, after, meta = [], [], None
        while True:
            data = self._json(["api", "graphql", "-f", f"query={BOARD_QUERY}", "-f", f"owner={board.owner}",
                               "-F", f"number={board.number}", *after])
            project = data["data"]["repositoryOwner"]["projectV2"]
            if meta is None:
                meta = {"project_id": project.get("id", ""), "fields": {
                    f["name"]: {"id": f["id"], "options": {o["name"]: o["id"] for o in f.get("options", [])}}
                    for f in project.get("fields", {}).get("nodes", []) if f.get("name")}}
            page = project["items"]
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
                                       closed_at=content.get("closedAt") or "", item_id=node.get("id", ""),
                                       blocked_by=_blocked_by(content)))
            if not page["pageInfo"]["hasNextPage"] or len(items) >= BOARD_ITEM_LIMIT:
                return items, meta
            after = ["-f", f"after={page['pageInfo']['endCursor']}"]

    def read_repo(self, repo, since=None):
        """What changed in a repo's issues since `since`, every open PR and the PRs closed lately, in one request.

        Only a first read, or a repo with more open PRs than fit in one page, takes more.
        """
        owner, name = repo.split("/")
        found = {"default_branch": None, "issues": [], "prs": []}
        cursors = {"issues": None, "open": None}
        wanted = {"issues": True, "open": True, "closed": True}
        while any(wanted.values()):
            data = self._graphql(REPO_QUERY, owner=owner, name=name, since=since or "1970-01-01T00:00:00Z",
                                 issuesAfter=cursors["issues"], openAfter=cursors["open"], **wanted)
            node = data["data"]["repository"]
            found["default_branch"] = (node.get("defaultBranchRef") or {}).get("name") or found["default_branch"]
            if wanted["issues"]:
                found["issues"] += [_issue(n) for n in node["issues"]["nodes"]]
            if wanted["open"]:
                found["prs"] += [_pr(n, True) for n in node["open"]["nodes"]]
            if wanted["closed"]:
                found["prs"] += [_pr(n, False) for n in node["closed"]["nodes"]]
            counts = {"issues": len(found["issues"]), "open": sum(pr["state"] == "OPEN" for pr in found["prs"])}
            for key, limit in (("issues", REPO_ISSUE_LIMIT), ("open", OPEN_PR_LIMIT)):
                info = node[key]["pageInfo"] if wanted[key] else {}
                more = bool(info.get("hasNextPage")) and counts[key] < limit
                wanted[key], cursors[key] = more, info.get("endCursor") if more else None
            wanted["closed"] = False
        return found

    def read_issue(self, repo, number):
        """None if there is no such issue: the number may be a PR's."""
        owner, name = repo.split("/")
        try:
            node = self._graphql(ISSUE_QUERY, owner=owner, name=name, number=number)["data"]["repository"]["issue"]
        except GitHubError as e:
            if "Could not resolve" in str(e):
                return None
            raise
        return _issue(node) if node else None

    def read_pr(self, repo, number):
        """None if there is no such PR: the number may be an issue's."""
        owner, name = repo.split("/")
        try:
            node = self._graphql(PR_QUERY, owner=owner, name=name, number=number)["data"]["repository"]["pullRequest"]
        except GitHubError as e:
            if "Could not resolve" in str(e):
                return None
            raise
        return _pr(node, True) if node else None

    def edit_cards(self, project_id, edits):
        """Sends many card changes in one request. Each edit is (item id, field id, kind, value).

        `kind` is `option`, `date`, or `clear` for an empty value.
        """
        parts = []
        for n, (item, field, kind, value) in enumerate(edits):
            ids = f"projectId: {json.dumps(project_id)}, itemId: {json.dumps(item)}, fieldId: {json.dumps(field)}"
            if kind == "clear":
                parts.append(f"c{n}: clearProjectV2ItemFieldValue(input: {{{ids}}}) {{ clientMutationId }}")
            else:
                value = (f"singleSelectOptionId: {json.dumps(value)}" if kind == "option" else f"date: {json.dumps(value)}")
                parts.append(f"c{n}: updateProjectV2ItemFieldValue(input: {{{ids}, value: {{{value}}}}}) "
                             "{ clientMutationId }")
        self._run(["api", "graphql", "-f", "query=mutation { " + " ".join(parts) + " }"])

    def add_card(self, project_id, url):
        """Puts an issue on a board and returns the card's id. Adding it twice returns the same card."""
        content = self._graphql(CONTENT_QUERY, url=url)["data"]["resource"]["id"]
        data = self._graphql(ADD_ITEM, project=project_id, content=content)
        return data["data"]["addProjectV2ItemById"]["item"]["id"]

    def set_status(self, board, url, status):
        self._run(["project", "item-edit", str(board.number), "--owner", board.owner,
                   "--url", url, "--field", "Status", "--value", board.columns.get(status, status)])

    def set_field(self, board, url, field, value):
        flag = "--date" if field == DEADLINE_FIELD else "--value"
        self._run(["project", "item-edit", str(board.number), "--owner", board.owner,
                   "--url", url, "--field", field, flag, value])

    def add_to_board(self, board, url):
        self._run(["project", "item-add", str(board.number), "--owner", board.owner, "--url", url])

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
        """Creates an issue through the REST API and returns its URL. It is labelled `agent` unless a person will do it.

        `tokens` also labels it `uses-tokens`, which gives its worker the project's `task_tokens`.
        """
        labels = []
        if not by_hand:
            self._run(["label", "create", AGENT_LABEL, "--repo", repo, "--force",
                       "--description", "Kandu may work on this"])
            labels.append(AGENT_LABEL)
        if tokens and not by_hand:
            self._run(["label", "create", TOKENS_LABEL, "--repo", repo, "--force",
                       "--description", "Its worker gets the project's task_tokens"])
            labels.append(TOKENS_LABEL)
        args = ["api", f"repos/{repo}/issues", "-X", "POST", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args += ["-f", f"labels[]={label}"]
        try:
            return self._json(args)["html_url"]
        except (GitHubError, ValueError, KeyError) as e:
            # A lost answer does not mean no issue: one made twice is worse than a look first.
            self._pause(LOOK_AGAIN_SECONDS)
            try:
                made = self._made_just_now(repo, title, body)
            except (GitHubError, ValueError, KeyError) as look:
                raise GitHubError(f"{e}; it is unknown whether the issue was made ({look}): look in {repo} "
                                  "before running it again") from e
            if made:
                return made
            raise GitHubError(f"{e}; no issue was made, so running it again is safe") from e

    def _made_just_now(self, repo, title, body):
        """The URL of the newest issues' one with exactly this title and body, or None."""
        newest = self._json(["api", f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=10"])
        return next((i["html_url"] for i in newest if "pull_request" not in i
                     and i["title"] == title and (i.get("body") or "") == body), None)

    def edit_issue(self, repo, number, title, body):
        self._run(["api", f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", f"title={title}", "-f", f"body={body}"])

    def remove_blocked_by(self, repo, number, blocker_repo, blocker_number):
        """Takes away GitHub's "blocked by" relationship of `repo#number` on `blocker_repo#blocker_number`.
        GitHub names the blocker by its issue id, so that is read first."""
        blocker_id = self._json(["api", f"repos/{blocker_repo}/issues/{blocker_number}"])["id"]
        self._run(["api", f"repos/{repo}/issues/{number}/dependencies/blocked_by/{blocker_id}", "-X", "DELETE"])

    def repo_is_public(self, repo):
        return self._json(["repo", "view", repo, "--json", "visibility"])["visibility"] == "PUBLIC"

    def issue_text(self, repo, number):
        return self._json(["issue", "view", str(number), "--repo", repo, "--json", "title,body,url"])

    def issue_is_open(self, repo, number):
        data = self._json(["issue", "view", str(number), "--repo", repo, "--json", "state"])
        return data["state"] == "OPEN"

    def close_issue(self, repo, number):
        self._run(["api", f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=closed"])

    def comment_issue(self, repo, number, body):
        """A PR takes its comments the same way: on GitHub it is an issue too."""
        self._run(["api", f"repos/{repo}/issues/{number}/comments", "-X", "POST", "-f", f"body={body}"])

    def add_label(self, repo, number, label, description):
        self._run(["label", "create", label, "--repo", repo, "--force", "--description", description])
        self._run(["api", f"repos/{repo}/issues/{number}/labels", "-X", "POST", "-f", f"labels[]={label}"])

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
        self.comment_issue(repo, number, body)

    def mark_pr(self, repo, pr):
        """Adds the `kandu` label, creating it in the repo if needed."""
        if any(label["name"] == KANDU_LABEL for label in pr["labels"]):
            return
        self.add_label(repo, pr["number"], KANDU_LABEL, "Work done by Kandu")

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
