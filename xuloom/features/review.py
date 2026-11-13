"""Turn `/review [<commit|range>] [-- <notes>]` into a code-review turn over a fixed Git scope."""
from xuloom.features.workspace_files import git, git_status

SKILL = "code-review"
USAGE = "Use /review, /review <commit|range> or /review [<commit|range>] -- <notes>"


def parse(argument):
    argument = argument.strip()
    if argument.startswith("--"):
        return "", argument[2:].strip()
    target, _, notes = argument.partition(" -- ")
    if len(target.split()) > 1:
        raise ValueError(USAGE)
    return target.strip(), notes.strip()


async def _commit(cwd, revision):
    # A leading dash would be read as a Git option instead of a revision.
    if revision.startswith("-"):
        raise ValueError("Not a commit: " + revision)
    code, output, _ = await git(cwd, "rev-parse", "--verify", "--quiet", revision + "^{commit}")
    if code:
        raise ValueError("Not a commit: " + revision)
    return output.strip()


async def _scope(cwd, target):
    if not target:
        status = await git_status(cwd)
        if not status["available"]:
            raise ValueError(status["reason"])
        if not status["dirty"]:
            raise ValueError("No uncommitted changes. " + USAGE)
        base = ("The repository has no commits yet; review every file listed by `git status`."
                if status["unborn"] else "Read them with `git status` and `git diff HEAD`, and read untracked files directly.")
        return f"the current uncommitted Git changes in {status['root']} (staged, unstaged and untracked). {base}"
    separator = "..." if "..." in target else ".." if ".." in target else ""
    if not separator:
        commit = await _commit(cwd, target)
        return f"commit {target} ({commit}). Read it with `git show {commit}`."
    left, right = target.split(separator, 1)
    start, end = await _commit(cwd, left or "HEAD"), await _commit(cwd, right or "HEAD")
    code, count, _ = await git(cwd, "rev-list", "--count", f"{start}..{end}")
    if code or not int(count.strip() or 0):
        raise ValueError(f"No commits in {target}.")
    return (f"the {count.strip()} commit(s) in {target} ({start}{separator}{end}). "
            f"Read them with `git log {start}..{end}` and `git diff {start}{separator}{end}`.")


async def review_request(cwd, argument):
    """Return the model input and `submit_message` options for one review turn."""
    target, notes = parse(argument)
    try:
        scope = await _scope(cwd, target)
    except FileNotFoundError:
        raise ValueError("Git is not installed") from None
    prompt = (f"Review {scope} Report findings only; do not modify files or create commits."
              + (f"\n\nReviewer notes: {notes}" if notes else ""))
    return prompt, {"selected_skills": [SKILL]}
