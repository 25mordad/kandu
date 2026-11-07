"""The reviewer's report, and the review record kept on the PR (DESIGN §5).

The record lives in the PR's comments, so a cycle that is cut off loses nothing.
Each comment carries a hidden JSON marker. Only comments by Kandu's own account
count; anyone else on a public repo can write the same text.
"""
import json
import re

REPORT_KEYS = {"head_sha", "summary", "criteria", "earlier", "findings", "checked_ok", "door",
               "blast_radius", "brief", "user_brief", "wait_for_user"}
FINDING_KEYS = {"n", "path", "line", "blocker", "body", "criterion"}
DOORS = ("two-way", "one-way")
CRITERION_RESULTS = ("met", "not_met", "not_shown")
EARLIER_RESULTS = ("fixed", "not_fixed", "partly_fixed")
REVIEW_MARKER = re.compile(r"<!-- kandu-review (\{.*?\}) -->", re.DOTALL)
FIX_MARKER = re.compile(r"<!-- kandu-fix ([0-9a-f]+) -->")
UPDATE_MARKER = re.compile(r"<!-- kandu-update ([0-9a-f]+) -->")
# The last thing in a reply. A model may write a line before it, however clearly it is told not to.
_TEXT = {"type": "string"}
_TEXTS = {"type": "array", "items": _TEXT}
_NUMBER_OR_NULL = {"type": ["integer", "null"]}
# Given to `claude`, so a report cannot leave a key out. validate_report still checks what a schema cannot.
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "head_sha": _TEXT, "summary": _TEXT,
        "criteria": {"type": "array", "items": {"type": "object", "required": ["result"], "properties": {
            "criterion": _TEXT, "result": {"enum": list(CRITERION_RESULTS)}}}},
        "earlier": {"type": "array", "items": {"type": "object", "required": ["n", "result", "body"], "properties": {
            "n": {"type": "integer"}, "result": {"enum": list(EARLIER_RESULTS)}, "body": _TEXT}}},
        "findings": {"type": "array", "items": {
            "type": "object", "required": sorted(FINDING_KEYS), "additionalProperties": False, "properties": {
                "n": {"type": "integer"}, "path": _TEXT, "line": _NUMBER_OR_NULL, "blocker": {"type": "boolean"},
                "criterion": _NUMBER_OR_NULL, "body": _TEXT}}},
        "checked_ok": _TEXTS, "door": {"enum": list(DOORS)}, "wait_for_user": _TEXTS,
        "blast_radius": _TEXT, "brief": _TEXT, "user_brief": _TEXT,
    },
    "required": sorted(REPORT_KEYS),
    "additionalProperties": False,
}
CODE_FENCE = re.compile(r"(?:\A|\n)```(?:json)?\n((?:(?!```).)*)\n```\Z", re.DOTALL)


class ReportError(Exception):
    pass


def parse_report(text):
    """Takes the reviewer's last message; returns the JSON object in it."""
    text = text.strip()
    fence = CODE_FENCE.search(text)
    try:
        report = json.loads(fence.group(1) if fence else text)
    except json.JSONDecodeError as e:
        raise ReportError("report is not JSON") from e
    if not isinstance(report, dict):
        raise ReportError("report is not a JSON object")
    return report


def _check_finding(finding):
    if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
        raise ReportError("a finding has the wrong keys")
    line = finding["line"]
    if type(finding["n"]) is not int or (line is not None and type(line) is not int):
        raise ReportError("a finding's n and line must be numbers")
    if not isinstance(finding["path"], str) or type(finding["blocker"]) is not bool:
        raise ReportError("a finding has a wrong path or blocker")
    if finding["criterion"] is not None and type(finding["criterion"]) is not int:
        raise ReportError("a finding's criterion must be a number or null")
    if not isinstance(finding["body"], str) or not finding["body"].strip():
        raise ReportError("a finding has no body")


def validate_report(report, still_open, second=False):
    """Checks a report against the items left by the last review. A second review judges no criteria and adds nothing."""
    if set(report) != REPORT_KEYS:
        raise ReportError("report has the wrong keys")
    if not isinstance(report["head_sha"], str) or not report["head_sha"]:
        raise ReportError("report has no head_sha")
    if report["door"] not in DOORS:
        raise ReportError("door must be two-way or one-way")
    if not isinstance(report["user_brief"], str) or not report["user_brief"].strip():
        raise ReportError("report has no user_brief")
    if not isinstance(report["wait_for_user"], list) or not all(isinstance(q, str) for q in report["wait_for_user"]):
        raise ReportError("wait_for_user must be a list of questions")
    for name in ("criteria", "earlier", "findings"):
        if not isinstance(report[name], list):
            raise ReportError(f"{name} must be a list")
    if not second:
        if not report["criteria"]:
            raise ReportError("no acceptance criterion has a result")
        for criterion in report["criteria"]:
            if not isinstance(criterion, dict) or criterion.get("result") not in CRITERION_RESULTS:
                raise ReportError("a criterion has no valid result")
        for finding in report["findings"]:
            _check_finding(finding)
    for item in report["earlier"]:
        if not isinstance(item, dict) or item.get("result") not in EARLIER_RESULTS:
            raise ReportError("an earlier finding has no valid result")
    used = {f["n"] for f in still_open}
    answered = [e.get("n") for e in report["earlier"]]
    if len(answered) != len(used) or set(answered) != used:
        raise ReportError("every open finding needs exactly one result")
    if not second:
        new = [f["n"] for f in report["findings"]]
        if len(set(new)) != len(new) or used & set(new):
            raise ReportError("finding numbers must not repeat")


def _in_scope(finding, criteria, changed):
    number = finding["criterion"]
    if number is not None and 1 <= number <= len(criteria) and criteria[number - 1]["result"] != "met":
        return True
    return finding["path"] in changed


def hold_to_scope(report, criteria, changed):
    """Holds a first review to the issue and the PR, and returns how many findings fell outside them.

    The criteria come from the issue, so a reviewer cannot add one or reword one. An issue with no such section
    has none to hold it to. A finding must name a criterion that is not met, or sit in a file of this PR.
    """
    if criteria:
        if len(report["criteria"]) != len(criteria):
            raise ReportError("every acceptance criterion of the issue needs exactly one result")
        report["criteria"] = [c | {"criterion": text} for c, text in zip(report["criteria"], criteria)]
    kept = [f for f in report["findings"] if _in_scope(f, report["criteria"], changed)]
    dropped = len(report["findings"]) - len(kept)
    report["findings"] = kept
    if any(c["result"] != "met" for c in report["criteria"]) and not blockers(kept):
        raise ReportError("a criterion is not met, but no blocker says so")
    return dropped


def carry_over(report, previous, still_open):
    """Ends a second review, and returns how many findings it wrongly added.

    It may not raise anything new, so the criteria stand as the first review left them: its blockers were what
    stood in their way. A door stays one-way once it was.
    """
    dropped = len(report["findings"])
    report["findings"] = []
    if previous["door"] == "one-way":
        report["door"] = "one-way"
    done = not blockers(open_after(report, still_open))
    report["criteria"] = [c | {"result": "met"} if done else c for c in previous["criteria"]]
    return dropped


def open_after(report, still_open):
    """Returns the findings open after this report: the unfixed earlier ones, then the new ones."""
    results = {e["n"]: e for e in report["earlier"]}
    carried = [f for f in still_open if results[f["n"]]["result"] != "fixed"]
    return carried + report["findings"]


def blockers(findings):
    return [f for f in findings if f["blocker"]]


def _finding_line(finding):
    kind = "Blocker" if finding["blocker"] else "Note"
    place = finding["path"] + (f":{finding['line']}" if finding["line"] else "")
    return f"- **{kind} {finding['n']}** `{place}`: {finding['body']}"


def comment_for(report, round_number, findings, second=False):
    """Renders one review round: readable text, then the marker the supervisor reads back."""
    record = {"round": round_number, "second": second, "head_sha": report["head_sha"], "door": report["door"], "open": findings,
              "summary": report["summary"], "brief": report["brief"], "blast_radius": report["blast_radius"],
              "criteria": report["criteria"], "wait_for_user": report["wait_for_user"]}
    # `>` only occurs inside JSON strings, so this keeps `-->` in a finding from ending the marker.
    marker = json.dumps(record, ensure_ascii=False).replace(">", "\\u003e")
    lines = [f"**Kandu review, round {round_number}** on `{report['head_sha']}`", "", report["brief"], ""]
    lines += [f"- {c.get('criterion', '')}: {c['result']}" for c in report["criteria"]]
    lines += [""] + ([_finding_line(f) for f in findings] or ["No findings."])
    lines += ["", *[f"- **Waits for you:** {q}" for q in report["wait_for_user"]]]
    lines += ["", f"Door: {report['door']}. Blast radius: {report['blast_radius']}", "",
              f"<!-- kandu-review {marker} -->"]
    return "\n".join(lines)


def _own_comments(pr, account):
    return [c["body"] for c in pr["comments"] if (c.get("author") or {}).get("login") == account]


def reviews_on(pr, account):
    """Returns the review records on a PR, oldest first."""
    reviews = []
    for body in _own_comments(pr, account):
        for marker in REVIEW_MARKER.findall(body):
            try:
                reviews.append(json.loads(marker))
            except json.JSONDecodeError:
                continue
    return reviews


def fix_comment(head_sha):
    return f"Kandu: the fix round changed no code on `{head_sha}`.\n\n<!-- kandu-fix {head_sha} -->"


def fix_tried(pr, account, head_sha):
    """True if a fix round already ended on `head_sha` with no new commit."""
    return any(head_sha in FIX_MARKER.findall(body) for body in _own_comments(pr, account))


def fix_marker_times(pr, account, head_sha):
    """When each `kandu-fix` marker on `head_sha` was posted, oldest first; None where the copy has no time.
    Update rounds wrote this marker too before they had their own, so a caller tells the two apart by time."""
    return [c.get("createdAt") for c in pr["comments"] if (c.get("author") or {}).get("login") == account
            and head_sha in FIX_MARKER.findall(c["body"])]


def update_comment(head_sha):
    return f"Kandu: bringing the default branch in changed nothing on `{head_sha}`.\n\n<!-- kandu-update {head_sha} -->"


def update_tried(pr, account, head_sha):
    """True if an update round already ran on `head_sha` and left it as it was. A fix round's marker does not count."""
    return any(head_sha in UPDATE_MARKER.findall(body) for body in _own_comments(pr, account))
