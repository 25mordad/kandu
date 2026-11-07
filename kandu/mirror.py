"""Kandu's own copy of GitHub, and the queue of what it still has to send there (DESIGN §3).

Every Kandu process reads from this copy, never from GitHub directly. A change is written
to the copy at once and queued; `kandu.sync` sends the queue every few minutes, in as few
requests as it can. Some writes go out at once, because the next step needs GitHub's answer:
a merge, creating an issue (its number names the task), and opening a PR or replacing its
description, which Kandu does for the worker.

The copy is read again from GitHub every few minutes, when the supervisor wakes, and when
the user gives an order. If a card changed on GitHub while a change to the same field
waited in the queue, the local change stands: it is sent, and GitHub follows the copy.
"""
import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import DEADLINE_FIELD
from .github import BoardItem, GitHub, GitHubError, is_companion, transient

MIRROR_DIR = Path(__file__).resolve().parent.parent / "state" / "mirror"
FRESH_SECONDS = 60            # a copy younger than this is not read again: a burst of wakes costs GitHub one read
SINCE_MARGIN_SECONDS = 120    # issues are read from a little before the last read, so a clock apart loses nothing
SEND_PAUSE_SECONDS = 1.0      # between two sent changes, below GitHub's limit on content made per minute
CARDS_PER_REQUEST = 20
PUSH_SETTLE_TRIES = 6         # a PR shows a push a few seconds after the branch has it; read it up to this often
PUSH_SETTLE_SECONDS = 5.0
MISSING_CARD_TRIES = 3        # a change to a card not on the board waits this many sends for it, then is dropped
CARD_FIELDS = ("Status", "Priority", "Kind", "Size", DEADLINE_FIELD)


class Later(GitHubError):
    """A change that cannot go yet, but may on a later send."""


def _now_iso(clock):
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _board_key(board):
    return f"{board.owner}-{board.number}"


def _attribute(field):
    return field.lower()


class Mirror:
    """The same calls as `github.GitHub`, served from the copy. Writes are queued."""

    def __init__(self, remote=None, root=MIRROR_DIR, account="", clock=time.time, pause=time.sleep):
        self.remote = remote or GitHub()
        self.root = Path(root)
        self.account = account
        self.clock = clock
        self.pause = pause

    # Files. Each copy has its own lock; the queue's lock is always taken after a copy's, never before.

    def _path(self, kind, name):
        return self.root / kind / f"{name.replace('/', '__')}.json"

    @contextmanager
    def _locked(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _load(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _store(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    @property
    def _outbox_path(self):
        return self.root / "outbox.json"

    def outbox(self):
        return self._load(self._outbox_path) or []

    def _log_dropped(self, entry, why):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "dropped.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps({"at": _now_iso(self.clock), "why": why, "change": entry}, ensure_ascii=False) + "\n")

    # Boards.

    def _board(self, board):
        """The board's copy, read from GitHub first if there is none."""
        path = self._path("boards", _board_key(board))
        data = self._load(path)
        if data is None:
            self.refresh_board(board)
            data = self._load(path)
        return data

    def refresh_board(self, board, max_age=0):
        path = self._path("boards", _board_key(board))
        with self._locked(path):
            data = self._load(path)
            if data is not None and 0 <= self.clock() - data["read_at"] < max_age:
                return
            items, meta = self.remote.read_board(board)
            data = {"read_at": self.clock(), "board": asdict(board), "meta": meta, "items": [asdict(i) for i in items]}
            with self._locked(self._outbox_path):
                self._overlay_board(board, data, resolve=True)
            self._store(path, data)

    def board_version(self, board):
        """Changes with every write of the copy, whichever process made it. Asks GitHub nothing."""
        try:
            return self._path("boards", _board_key(board)).stat().st_mtime_ns
        except OSError:
            return None

    def board_items(self, board):
        return [BoardItem(**item) for item in self._board(board)["items"]]

    def set_status(self, board, url, status):
        self._queue_card(board, url, "Status", status)

    def set_field(self, board, url, field, value):
        self._queue_card(board, url, field, value)

    def add_to_board(self, board, url):
        """At once: the card must exist before its fields can be queued."""
        data = self._board(board)
        item_id = self.remote.add_card(data["meta"]["project_id"], url)
        repo, number = url.split("github.com/")[1].split("/issues/")
        issue = self._issue(repo, int(number)) or {}
        path = self._path("boards", _board_key(board))
        with self._locked(path):
            data = self._load(path) or data
            if not any(item["url"] == url for item in data["items"]):
                data["items"].append(asdict(BoardItem(repo, int(number), issue.get("title", ""), url, "",
                                                      issue.get("labels", []), issue.get("body", ""), item_id=item_id)))
                self._store(path, data)

    def board_columns(self, board):
        return self.remote.board_columns(board)

    def board_fields(self, board):
        return self.remote.board_fields(board)

    def create_board(self, owner, title, columns):
        return self.remote.create_board(owner, title, columns)

    def add_columns(self, board, names):
        return self.remote.add_columns(board, names)

    # Repos: issues, PRs, the default branch.

    def _repo(self, repo):
        path = self._path("repos", repo)
        data = self._load(path)
        if data is None:
            self.refresh_repo(repo)
            data = self._load(path)
        return data

    def refresh_repo(self, repo, max_age=0):
        path = self._path("repos", repo)
        with self._locked(path):
            data = self._load(path)
            if data is not None and 0 <= self.clock() - data["read_at"] < max_age:
                return
            since = None
            if data is not None:
                since = datetime.fromtimestamp(data["read_at"] - SINCE_MARGIN_SECONDS, timezone.utc)
                since = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            read_at = self.clock()
            found = self.remote.read_repo(repo, since)
            data = data or {"issues": {}, "prs": {}}
            data["read_at"] = read_at
            data["default_branch"] = found["default_branch"] or data.get("default_branch")
            for issue in found["issues"]:
                data["issues"][str(issue["number"])] = issue
            for pr in [pr for pr in data["prs"].values() if pr["state"] == "OPEN"]:
                pr["stale"] = True   # an open PR missing from this read was closed; it shows among the closed ones
            for pr in found["prs"]:
                data["prs"][str(pr["number"])] = pr
            with self._locked(self._outbox_path):
                self._overlay_repo(repo, data)
            self._store(path, data)

    def refresh_pr(self, repo, number):
        """One PR, read now: after an agent pushed to it or a merge."""
        self._put(repo, "prs", number, self.remote.read_pr(repo, number))

    def refresh_pushed_pr(self, repo, number):
        """One PR, read after an agent may have pushed to it. GitHub moves the branch at once but the PR's head
        a few seconds later, so the PR is read again until it shows the branch's last commit, or the tries run out.
        Returns that commit: the PR may still be behind it."""
        pushed = self.remote.branch_head(repo, self.pr(repo, number)["headRefName"])
        for n in range(PUSH_SETTLE_TRIES):
            if n:
                self.pause(PUSH_SETTLE_SECONDS)
            self.refresh_pr(repo, number)
            if self.pr(repo, number)["headRefOid"] == pushed:
                break
        return pushed

    def _put(self, repo, kind, number, value):
        if value is None:
            return
        self._repo(repo)
        path = self._path("repos", repo)
        with self._locked(path):
            data = self._load(path)
            data[kind][str(number)] = value
            with self._locked(self._outbox_path):
                self._overlay_repo(repo, data)
            self._store(path, data)

    def _issue(self, repo, number):
        issue = self._repo(repo)["issues"].get(str(number))
        if issue is None:  # older than the copy, or in a repo Kandu does not watch
            self._put(repo, "issues", number, self.remote.read_issue(repo, number))
            issue = self._repo(repo)["issues"].get(str(number))
        return issue

    def issue_text(self, repo, number):
        issue = self._issue(repo, number)
        if issue is None:
            raise GitHubError(f"{repo}#{number}: no such issue")
        return {"title": issue["title"], "body": issue["body"], "url": issue["url"]}

    def issue_is_open(self, repo, number):
        issue = self._issue(repo, number)
        if issue is None:
            raise GitHubError(f"{repo}#{number}: no such issue")
        return issue["state"] == "OPEN"

    def default_branch(self, repo):
        return self._repo(repo)["default_branch"]

    def repo_is_public(self, repo):
        return self.remote.repo_is_public(repo)

    def find_pr(self, repo, issue):
        """Returns the newest PR of an issue's `kandu/` branch in this repo, or None. A fork's PR never counts."""
        prs = self._repo(repo)["prs"].values()
        numbers = [pr["number"] for pr in prs
                   if pr["headRefName"].startswith(f"kandu/{issue}-") and not pr["isCrossRepository"]]
        return self.pr(repo, max(numbers)) if numbers else None

    def pr(self, repo, number):
        pr = self._repo(repo)["prs"].get(str(number))
        if pr is None or pr.get("stale"):
            self.refresh_pr(repo, number)
            pr = self._repo(repo)["prs"].get(str(number))
        if pr is None:
            raise GitHubError(f"{repo}#{number}: no such PR")
        return pr

    def review_threads(self, repo, number):
        return self.pr(repo, number)["reviewThreads"]

    def companions(self, repo, pr, repos):
        """Returns the open companion PRs of `pr` in `repos`, as (repo, number) pairs."""
        found = []
        for other in repos:
            for candidate in self._repo(other)["prs"].values():
                same_pr = other == repo and candidate["number"] == pr["number"]
                if (candidate["state"] == "OPEN" and not candidate.get("stale") and not same_pr
                        and not candidate["isCrossRepository"] and is_companion(pr, repo, candidate, other)):
                    found.append((other, candidate["number"]))
        return found

    # Writes that go at once.

    def create_issue(self, repo, title, body, by_hand=False, tokens=False):
        url = self.remote.create_issue(repo, title, body, by_hand=by_hand, tokens=tokens)
        number = int(url.rstrip("/").split("/")[-1])
        labels = [] if by_hand else ["agent", *(["uses-tokens"] if tokens else [])]
        self._put(repo, "issues", number, {"number": number, "title": title, "body": body, "url": url,
                                           "state": "OPEN", "labels": labels, "updatedAt": _now_iso(self.clock)})
        return url

    def merge_pr(self, repo, number, head_sha):
        """GitHub refuses this if the head is no longer `head_sha`. The copy reads the repo after."""
        self.remote.merge_pr(repo, number, head_sha)
        try:
            self.refresh_pr(repo, number)
            self.refresh_repo(repo)
        except GitHubError:
            pass  # the merge stands; the next read of the copy shows it

    def create_pr(self, repo, head, base, title, body):
        """Opens the PR at once: the next step reads it. The copy reads the repo after."""
        number = self.remote.create_pr(repo, head, base, title, body)
        try:
            self.refresh_repo(repo)
        except GitHubError:
            pass  # the PR stands; the next read of the copy shows it
        return number

    def edit_pr_body(self, repo, number, body):
        self.remote.edit_pr_body(repo, number, body)
        try:
            self.refresh_pr(repo, number)
        except GitHubError:
            pass

    # Writes that wait in the queue.

    def _queue(self, entry):
        entry = {"id": secrets.token_hex(6), "queued_at": _now_iso(self.clock), "tries": 0, **entry}
        with self._locked(self._outbox_path):
            entries = self.outbox()
            if entry["kind"] == "card":  # a second change to the same field replaces the first; its starting point stays
                for old in entries:
                    if old["kind"] == "card" and (old["board"], old["url"], old["field"]) == \
                            (entry["board"], entry["url"], entry["field"]):
                        entry["before"], entry["id"] = old["before"], old["id"]
                entries = [old for old in entries if old["id"] != entry["id"]]
            entries.append(entry)
            self._store(self._outbox_path, entries)
        return entry

    def _queue_card(self, board, url, field, value):
        data = self._board(board)
        item = next((i for i in data["items"] if i["url"] == url), None)
        before = item[_attribute(field)] if item else ""
        self._queue({"kind": "card", "board": asdict(board), "url": url, "field": field, "value": value,
                     "before": before})
        self._apply_board(board)

    def _queue_issue(self, kind, repo, number, **fields):
        self._queue({"kind": kind, "repo": repo, "number": number, **fields})
        if self._load(self._path("repos", repo)) is not None:
            self._apply_repo(repo)
        if kind in ("close", "edit"):
            for path in (self.root / "boards").glob("*.json"):
                data = self._load(path)
                if data and any(i["repo"] == repo and i["number"] == number for i in data["items"]):
                    self._apply_board(_BoardRef(**data["board"]))

    def _apply_board(self, board):
        path = self._path("boards", _board_key(board))
        with self._locked(path):
            data = self._load(path)
            if data is not None:
                with self._locked(self._outbox_path):
                    self._overlay_board(board, data, resolve=False)
                self._store(path, data)

    def _apply_repo(self, repo):
        path = self._path("repos", repo)
        with self._locked(path):
            data = self._load(path)
            if data is not None:
                with self._locked(self._outbox_path):
                    self._overlay_repo(repo, data)
                self._store(path, data)

    def comment_issue(self, repo, number, body):
        self._queue_issue("comment", repo, number, body=body)

    def comment_pr(self, repo, number, body):
        self._queue_issue("comment", repo, number, body=body, pr=True)

    def close_issue(self, repo, number):
        self._queue_issue("close", repo, number)

    def edit_issue(self, repo, number, title, body):
        self._queue_issue("edit", repo, number, title=title, body=body)

    def mark_pr(self, repo, pr):
        if any(label["name"] == "kandu" for label in pr["labels"]):
            return
        self._queue_issue("label", repo, pr["number"], label="kandu", description="Work done by Kandu")

    # The queue on top of what GitHub said. Called with the copy's lock and the queue's lock held.

    def _overlay_board(self, board, data, resolve):
        """Puts queued changes on a board's copy.

        With `resolve`, `data` is what GitHub says now. A queued card change GitHub already holds is
        done; any other stays and is put on the copy, even if the card changed on GitHub meanwhile:
        the local change is the base, and the next send makes GitHub follow it.
        """
        entries, kept = self.outbox(), []
        items = {item["url"]: item for item in data["items"]}
        key = _board_key(board)
        for entry in entries:
            if entry["kind"] == "card" and _board_key(_BoardRef(**entry["board"])) == key:
                item = items.get(entry["url"])
                if item is not None:
                    now = item[_attribute(entry["field"])]
                    if resolve and now == entry["value"]:
                        continue  # GitHub already holds it
                    item[_attribute(entry["field"])] = entry["value"]
            elif entry["kind"] in ("close", "edit"):
                for item in data["items"]:
                    if (item["repo"], item["number"]) == (entry["repo"], entry["number"]):
                        if entry["kind"] == "close":
                            item["closed_at"] = item["closed_at"] or entry["queued_at"]
                        else:
                            item["title"], item["body"] = entry["title"], entry["body"]
            kept.append(entry)
        if len(kept) != len(entries):
            self._store(self._outbox_path, kept)

    def _overlay_repo(self, repo, data):
        for entry in self.outbox():
            if entry.get("repo") != repo:
                continue
            issue = data["issues"].get(str(entry["number"]))
            pr = data["prs"].get(str(entry["number"]))
            if entry["kind"] == "close" and issue is not None:
                issue["state"] = "CLOSED"
            elif entry["kind"] == "edit" and issue is not None:
                issue["title"], issue["body"] = entry["title"], entry["body"]
            elif entry["kind"] == "label" and pr is not None:
                if not any(label["name"] == entry["label"] for label in pr["labels"]):
                    pr["labels"].append({"name": entry["label"]})
            elif entry["kind"] == "comment" and entry.get("pr") and pr is not None:
                comment = {"author": {"login": self.account}, "body": entry["body"], "createdAt": entry["queued_at"]}
                if not any(c["body"] == entry["body"] and (c.get("author") or {}).get("login") == self.account
                           for c in pr["comments"]):
                    pr["comments"].append(comment)

    # Reading everything, and sending the queue.

    def refresh(self, config, max_age=FRESH_SECONDS):
        """Reads every board and repo of the config again, unless its copy is younger than `max_age`.

        A refusal leaves that copy as it was and goes on with the rest. Returns what failed.
        """
        failed = []
        boards = {_board_key(p.board): p.board for p in config.projects if p.board is not None}
        for board in boards.values():
            try:
                self.refresh_board(board, max_age)
            except (GitHubError, KeyError, TypeError) as e:
                failed.append(f"board {_board_key(board)}: {e}")
        for repo in dict.fromkeys(repo for p in config.projects for repo in p.repos):
            try:
                self.refresh_repo(repo, max_age)
            except (GitHubError, KeyError, TypeError) as e:
                failed.append(f"{repo}: {e}")
        return failed

    def send(self):
        """Sends the queue. Call it right after `refresh`, so what GitHub holds meanwhile is read first.

        A refusal that passes by itself keeps the change for the next send; any other drops and logs it.
        Returns (sent, kept, dropped).
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "send.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0, len(self.outbox()), 0
            entries = self.outbox()
            done, failed = set(), {}
            cards = [e for e in entries if e["kind"] == "card"]
            for board_key in dict.fromkeys(_board_key(_BoardRef(**e["board"])) for e in cards):
                group = [e for e in cards if _board_key(_BoardRef(**e["board"])) == board_key]
                self._send_cards(group, done, failed)
            for entry in entries:
                if entry["kind"] == "card":
                    continue
                if done or failed:
                    self.pause(SEND_PAUSE_SECONDS)
                try:
                    self._send_one(entry)
                    done.add(entry["id"])
                except GitHubError as e:
                    failed[entry["id"]] = e
            return self._settle(done, failed)

    def _send_cards(self, group, done, failed):
        board = _BoardRef(**group[0]["board"])
        data = self._board(board)
        meta, items = data["meta"], {item["url"]: item for item in data["items"]}
        edits, sent = [], []
        for entry in group:
            item = items.get(entry["url"])
            field = meta["fields"].get(entry["field"])
            if item is None or not item.get("item_id"):
                late = entry["tries"] + 1 < MISSING_CARD_TRIES
                failed[entry["id"]] = (Later if late else GitHubError)("the card is not on the board")
                continue
            if field is None:
                failed[entry["id"]] = GitHubError(f"the board has no field {entry['field']}")
                continue
            value = entry["value"]
            if entry["field"] == "Status":
                value = board.columns.get(value, value)
            if value == "":
                edits.append((item["item_id"], field["id"], "clear", None))
            elif entry["field"] == DEADLINE_FIELD:
                edits.append((item["item_id"], field["id"], "date", value))
            elif value in field["options"]:
                edits.append((item["item_id"], field["id"], "option", field["options"][value]))
            else:
                failed[entry["id"]] = GitHubError(f"{entry['field']} has no option {value!r}")
                continue
            sent.append(entry)
        for start in range(0, len(edits), CARDS_PER_REQUEST):
            chunk, entries = edits[start:start + CARDS_PER_REQUEST], sent[start:start + CARDS_PER_REQUEST]
            if start:
                self.pause(SEND_PAUSE_SECONDS)
            try:
                self.remote.edit_cards(meta["project_id"], chunk)
                done.update(e["id"] for e in entries)
            except GitHubError as e:
                if transient(e) or len(chunk) == 1:
                    failed.update({entry["id"]: e for entry in entries})
                    continue
                for edit, entry in zip(chunk, entries):  # one bad change must not hold back the rest
                    self.pause(SEND_PAUSE_SECONDS)
                    try:
                        self.remote.edit_cards(meta["project_id"], [edit])
                        done.add(entry["id"])
                    except GitHubError as one:
                        failed[entry["id"]] = one

    def _send_one(self, entry):
        repo, number = entry["repo"], entry["number"]
        if entry["kind"] == "comment":
            self.remote.comment_issue(repo, number, entry["body"])
        elif entry["kind"] == "close":
            self.remote.close_issue(repo, number)
        elif entry["kind"] == "edit":
            self.remote.edit_issue(repo, number, entry["title"], entry["body"])
        elif entry["kind"] == "label":
            self.remote.add_label(repo, number, entry["label"], entry["description"])

    def _settle(self, done, failed):
        dropped = 0
        with self._locked(self._outbox_path):
            kept = []
            for entry in self.outbox():
                if entry["id"] in done:
                    continue
                error = failed.get(entry["id"])
                if error is not None:
                    entry["tries"] += 1
                    if not (isinstance(error, Later) or transient(error)):
                        self._log_dropped(entry, str(error))
                        dropped += 1
                        continue
                    entry["last_error"] = str(error)
                kept.append(entry)
            self._store(self._outbox_path, kept)
        return len(done), len(kept), dropped


class _BoardRef:
    """A board as the queue stores it: enough to name its copy and its columns."""

    def __init__(self, owner, number, columns=None):
        self.owner, self.number, self.columns = owner, number, columns or {}


def open_github(config, root=MIRROR_DIR, refresh=False):
    """The copy, as every Kandu process uses it. With `refresh`, for an order from the user: read GitHub first."""
    mirror = Mirror(GitHub(), root, account=config.settings.github.account)
    if refresh:
        mirror.refresh(config)
    return mirror
