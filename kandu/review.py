"""The reviewer's report, and the review record kept on the PR (DESIGN §5).

The record lives in the PR's comments, so a cycle that is cut off loses nothing.
Each comment carries a hidden JSON marker. Only comments by Kandu's own account
count; anyone else on a public repo can write the same text.
"""
import json
import re

REPORT_KEYS = {"head_sha", "summary", "criteria", "earlier", "findings", "checked_ok", "door",
               "blast_radius", "brief", "user_brief", "wait_for_user"}
FINDING_KEYS = {"n", "path", "line", "blocker", "body"}
DOORS = ("two-way", "one-way")
CRITERION_RESULTS = ("met", "not_met", "not_shown")
EARLIER_RESULTS = ("fixed", "not_fixed", "partly_fixed")
REVIEW_MARKER = re.compile(r"<!-- kandu-review (\{.*?\}) -->", re.DOTALL)
FIX_MARKER = re.compile(r"<!-- kandu-fix ([0-9a-f]+) -->")
CODE_FENCE = re.compile(r"\A```(?:json)?\n(.*)\n```\Z", re.DOTALL)


class ReportError(Exception):
    pass


def parse_report(text):
    """Takes the reviewer's last message; returns the JSON object in it."""
    text = text.strip()
    fence = CODE_FENCE.match(text)
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
    if not isinstance(finding["body"], str) or not finding["body"].strip():
        raise ReportError("a finding has no body")


def validate_report(report, still_open):
    """Checks a report against the findings left open by the last review."""
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
    new = [f["n"] for f in report["findings"]]
    if len(set(new)) != len(new) or used & set(new):
        raise ReportError("finding numbers must not repeat")
    if any(c["result"] != "met" for c in report["criteria"]) and not open_after(report, still_open):
        raise ReportError("a criterion is not met, but no finding says so")


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


def comment_for(report, round_number, findings):
    """Renders one review round: readable text, then the marker the supervisor reads back."""
    record = {"round": round_number, "head_sha": report["head_sha"], "door": report["door"], "open": findings,
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
