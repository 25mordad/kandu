"""A project's open-PR limit (`max_open_prs`): how many open PRs this install's GitHub account may have on its repos.

A project without one is never held. At the limit, no task of the project starts from `Ready` and none is sized;
work on a PR that is already open goes on. The PRs are counted from Kandu's copy of GitHub alone, so the count costs
GitHub nothing: every open PR whose author is `github.account`, compared without case, whoever opened it. The hold is
logged once when it starts and once when it ends.
"""

HELD, FREED = "open_prs_held", "open_prs_freed"


def open_prs(github, project, account):
    """The open PRs on the project's repos whose author is `account`, compared without case, as `OWNER/REPO#N`."""
    login = account.lower()
    return [f"{repo}#{pr['number']}" for repo in project.repos for pr in github.open_prs(repo)
            if ((pr.get("author") or {}).get("login") or "").lower() == login]


def at_limit(github, project, account):
    """The open PRs that hold the project, or None while it is below its limit or has none."""
    if project.max_open_prs is None:
        return None
    prs = open_prs(github, project, account)
    return prs if len(prs) >= project.max_open_prs else None


def held(store):
    """The names of the projects whose hold the log shows as lasting: an `open_prs_held` with no `open_prs_freed`
    after it. One pass over the log."""
    last = {}
    for day in store.days():
        for event in store.events(day):
            if event["type"] in (HELD, FREED):
                last[event["project"]] = event["type"]
    return {name for name, kind in last.items() if kind == HELD}


def record(store, project, prs, was):
    """Logs the hold when it starts and when it ends, and nothing while it lasts. `prs` is what `at_limit` found
    now, and `was` whether the project was held. Returns whether it is held now."""
    if prs is not None and not was:
        store.record(HELD, project=project.name, prs=prs, limit=project.max_open_prs)
    elif prs is None and was:
        store.record(FREED, project=project.name)
    return prs is not None
