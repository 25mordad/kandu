"""A project's base branch (DESIGN §5): the branch of its repos that Kandu works on and merges into.

A project names it with `base_branch`; without it, it is each repo's default branch on GitHub, as before. Every
reader of that branch asks `base_branch`, so no part of Kandu works on another one. A named base branch that GitHub
does not have holds the project: the supervisor records it here, none of its tasks starts, and the dashboard and the
doctor say why.
"""

MISSING, FOUND = "base_missing", "base_found"


def base_branch(github, project, repo):
    """The branch of `repo` that Kandu works on and merges into for `project`: its own `base_branch`, or else the
    repo's default branch on GitHub."""
    return project.base_branch or github.default_branch(repo)


def missing_repos(github, project):
    """The repos of `project` that lack the base branch it names on GitHub. One that names none lacks nothing: a
    repo's default branch is always there."""
    if not project.base_branch:
        return []
    return [repo for repo in project.repos if github.branch_tip(repo, project.base_branch) is None]


def missing(store, project):
    """The `base_missing` event while the project's base branch is missing, or None: a later `base_found` ends it."""
    last = None
    for day in store.days():
        for event in store.events(day):
            if event["type"] in (MISSING, FOUND) and event.get("project") == project.name:
                last = event
    return last if last is not None and last["type"] == MISSING else None


def record(store, project, repos):
    """Records what GitHub showed of the project's base branch, only when that changed: `repos` lacks it, or none."""
    held = missing(store, project)
    if repos and (held is None or held["repos"] != repos or held["branch"] != project.base_branch):
        store.record(MISSING, project=project.name, branch=project.base_branch, repos=repos)
    elif not repos and held is not None:
        store.record(FOUND, project=project.name, branch=project.base_branch)


def why(event):
    """What the dashboard and the doctor say of a missing base branch."""
    return f"the base branch {event['branch']} is not on GitHub in {', '.join(event['repos'])}"
