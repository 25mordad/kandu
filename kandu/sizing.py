"""The size scale of a task (DESIGN §2). The numbers live here, so a prompt cannot drift from what code enforces."""
import re

SMALL, MEDIUM, LARGE = "Small", "Medium", "Large"
ORDER = (SMALL, MEDIUM, LARGE)
LIMITS = {SMALL: {"criteria": 3, "files": 5}, MEDIUM: {"criteria": 5, "files": 15}}
DOORS = ("two-way", "one-way", "mixed")  # `mixed`: easy work and work that is hard to undo in one task.
CRITERIA = re.compile(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
CRITERION = re.compile(r"^\s*[-*] \[[ xX]\]", re.MULTILINE)


def criteria_in(body):
    section = CRITERIA.search(body or "")
    return len(CRITERION.findall(section.group(1))) if section else 0


def fits(size, criteria, files):
    return size == LARGE or (criteria <= LIMITS[size]["criteria"] and files <= LIMITS[size]["files"])


def smallest(criteria, files, door):
    """Returns the smallest size these facts allow."""
    if door == "mixed":
        return LARGE
    return next(size for size in ORDER if fits(size, criteria, files))


def describe():
    """The scale as the splitter reads it."""
    return [f"- `{size}`: at most {limit['criteria']} acceptance criteria and {limit['files']} changed files"
            for size, limit in LIMITS.items()] + [f"- `{LARGE}`: anything more, or a `mixed` door"]
