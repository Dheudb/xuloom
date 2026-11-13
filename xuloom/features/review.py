from pydantic import BaseModel, Field

from xuloom.features.workspace_files import git, git_status
from xuloom.tools.base import BaseAgenticTool

SKILL = "code-review"
USAGE = "Use /review, /review <commit|range> or /review [<commit|range>] -- <notes>"
READ_TOOLS = frozenset({"file_read_tool", "grep_tool", "glob_tool", "review_git"})
DIFF_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--submodule=short")


def parse(argument):
    parts = argument.split()
    if not parts:
        return "", ""
    if parts[0] == "--":
        return "", argument.strip()[2:].strip()
    rest = argument.strip()[len(parts[0]):].strip()
    if parts[0].startswith("-") or rest and rest.split(maxsplit=1)[0] != "--":
        raise ValueError(USAGE)
    return parts[0], rest[2:].strip() if rest else ""


async def read_git(cwd, *args):
    """Run fixed read operations without Git's configurable external helpers."""
    env = {"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_ALLOW_PROTOCOL": ""}
    options = ["--no-pager"]
    if args[0] in {"status", "diff"}:
        code, output, _ = await git(cwd, "config", "--null", "--name-only", "--get-regexp",
                                   r"^filter\..*\.(clean|process|required)$", env_overrides=env)
        if code not in {0, 1}:
            raise ValueError("Could not read Git filter configuration.")
        for key in dict.fromkeys(output.rstrip("\0").split("\0")) if output else ():
            options.extend(("-c", key + ("=false" if key.endswith(".required") else "=")))
    if args[0] == "status":
        args = (args[0], "--ignore-submodules=dirty", *args[1:])
    return await git(cwd, *options, *args, env_overrides=env)


async def _commit(cwd, revision):
    code, output, _ = await read_git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                     revision + "^{commit}")
    if code:
        raise ValueError("Not a commit: " + revision)
    return output.strip()


async def _scope(cwd, target):
    code, root, _ = await read_git(cwd, "rev-parse", "--show-toplevel")
    if code:
        raise ValueError("The workspace is not a Git repository or Git could not read it.")
    root = root.rstrip("\r\n")
    if not target:
        status = await git_status(root, read_git=read_git)
        if not status["available"]:
            raise ValueError(status["reason"])
        if not status["dirty"]:
            raise ValueError("No uncommitted changes. " + USAGE)
        return {"root": root, "kind": "worktree", "base": status["commit"]}, (
            f"the current uncommitted Git changes in {root}, including staged, unstaged and untracked files. "
            "Use review_git with status, staged and unstaged; read untracked files directly.")
    separator = "..." if "..." in target else ".." if ".." in target else ""
    if not separator:
        commit = await _commit(root, target)
        return {"root": root, "kind": "commit", "head": commit}, f"commit {target} ({commit}). Use review_git with diff."
    left, right = target.split(separator, 1)
    start, end = await _commit(root, left or "HEAD"), await _commit(root, right or "HEAD")
    base = start
    if separator == "...":
        code, output, _ = await read_git(root, "merge-base", start, end)
        if code:
            raise ValueError(f"Cannot review {target}: no common ancestor is available.")
        base = output.strip()
    if base == end:
        raise ValueError(f"No changes in {target}.")
    return {"root": root, "kind": "range", "base": base, "head": end}, (
        f"the Git range {target} ({start}{separator}{end}). Use review_git with diff, comparing {base} to {end}.")


async def review_request(cwd, argument):
    """Return the model input and trusted scope for one read-only review turn."""
    target, notes = parse(argument)
    try:
        scope, summary = await _scope(cwd, target)
    except FileNotFoundError:
        raise ValueError("Git is not installed or the working directory is missing.") from None
    prompt = (f"Review {summary} Report findings only."
              + (f"\n\nReviewer notes: {notes}" if notes else ""))
    return prompt, {"selected_skills": [SKILL], "review_scope": scope}


class ReviewGitInput(BaseModel):
    action: str = Field(description="Use diff for a commit/range; status, staged or unstaged for a working tree.")
    offset: int = Field(0, ge=0, description="Next character offset from the previous result.")


class ReviewGitTool(BaseAgenticTool):
    name = "review_git"
    description = "Read Git changes for the fixed /review scope. Follow next_offset to read the rest of the output."
    input_schema = ReviewGitInput
    is_read_only = True
    risk_level = "low"

    async def execute(self, action, offset=0):
        scope = self.context["review_scope"]
        kind = scope["kind"]
        if kind == "worktree":
            base = (scope["base"],) if scope["base"] else ()
            commands = {
                "status": ("status", "--porcelain=v1", "--untracked-files=all"),
                "staged": ("diff", *DIFF_FLAGS, "--ignore-submodules=dirty", "--cached", *base, "--"),
                "unstaged": ("diff", *DIFF_FLAGS, "--ignore-submodules=dirty", "--"),
            }
        elif kind == "commit":
            commands = {"diff": ("show", "--format=fuller", "--diff-merges=first-parent", *DIFF_FLAGS, scope["head"], "--")}
        else:
            commands = {"diff": ("diff", *DIFF_FLAGS, scope["base"], scope["head"], "--")}
        if action not in commands:
            return "Error: Available review actions: " + ", ".join(commands)
        code, output, error = await read_git(scope["root"], *commands[action])
        if code:
            return "Error: " + (error.strip() or "Git could not read the review scope.")
        end = offset + 3400
        footer = f"\n[next_offset={end}]" if len(output) > end else "\n[End of Git output]"
        return output[offset:end] + footer


def can_use_review_path(engine, scope):
    from pathlib import Path

    return not engine.execution_sandbox.enabled or Path(scope["root"]).resolve().is_relative_to(
        Path(engine.session_cwd).resolve())
