"""Read-only workspace navigation and Git review, shared by desktop RPCs."""
import asyncio
import os
from pathlib import Path

from xuloom.features.file_changes import SNAPSHOT_LIMIT, describe_change, read_snapshot


def workspace_path(cwd, requested=""):
    root = Path(cwd).resolve()
    if not isinstance(requested, str) or "\0" in requested or "://" in requested:
        raise ValueError("请选择工作区内的本地路径")
    path = (root / requested).resolve()
    if not path.is_relative_to(root):
        raise ValueError("只能浏览当前工作目录")
    return path


def list_directory(cwd, requested=""):
    directory = workspace_path(cwd, requested)
    if not directory.is_dir():
        raise ValueError("目录已被移动或删除")
    entries = []
    with os.scandir(directory) as children:
        for child in children:
            if child.name == ".git":
                continue
            try:
                resolved = Path(child.path).resolve()
                inside = resolved.is_relative_to(Path(cwd).resolve())
                entries.append({"name": child.name, "path": child.path,
                                "directory": child.is_dir(), "available": inside})
            except (OSError, RuntimeError):
                continue
    entries.sort(key=lambda item: (not item["directory"], item["name"].casefold()))
    return {"path": str(directory), "entries": entries[:1000], "truncated": len(entries) > 1000}


def inspect_reference(cwd, requested, known=(), base_path=None):
    """Resolve a selected reference through existing preview/workspace boundaries."""
    from xuloom.features.file_changes import preview_path
    if base_path:
        base = preview_path(cwd, base_path, known)
        if not isinstance(requested, str): raise ValueError("请选择本地路径")
        requested = str(base.parent / requested)
    try:
        path = preview_path(cwd, requested, known)
        return {"path": str(path), "directory": False}
    except ValueError:
        path = workspace_path(cwd, requested)
        if not path.is_dir(): raise ValueError("文件或目录不存在，或不在当前会话的预览范围内")
        return {"path": str(path), "directory": True}


async def git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if key not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"}}
    env["LC_ALL"] = "C"
    process = await asyncio.create_subprocess_exec(
        "git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-c", "diff.relative=false", *args, cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0, env=env)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=10)
    except BaseException as exc:
        if process.returncode is None:
            try: process.kill()
            except ProcessLookupError: pass
        await process.communicate()
        if isinstance(exc, asyncio.TimeoutError): raise ValueError("读取 Git 状态超时") from exc
        raise
    return process.returncode, output.decode("utf-8", errors="replace"), error.decode("utf-8", errors="replace")


async def git_status(cwd):
    """Read-only repository snapshot, without model, session, diff or network requests."""
    if not Path(cwd).is_dir():
        return {"available": False, "code": "workspace_missing", "reason": "工作目录不存在"}
    try:
        code, root, error = await git(cwd, "rev-parse", "--show-toplevel")
        if code:
            return {"available": False, "code": "not_repository" if "not a git repository" in error.lower() else "git_error",
                "reason": "当前目录不是 Git 工作区，或 Git 拒绝读取；请检查目录与仓库所有权"}
        code, output, _ = await git(root.rstrip("\r\n"), "status", "--porcelain=v2", "--branch", "-z", "--no-renames", "--untracked-files=normal")
        if code: return {"available": False, "code": "git_error", "reason": "无法读取 Git 状态"}
    except FileNotFoundError:
        return {"available": False, "code": "git_missing", "reason": "未安装 Git"}
    except ValueError:
        return {"available": False, "code": "git_timeout", "reason": "读取 Git 状态超时"}
    headers, staged, unstaged, untracked, changed, conflicted = {}, 0, 0, 0, 0, 0
    for row in output.split("\0"):
        if row.startswith("# "):
            key, _, value = row[2:].partition(" "); headers[key] = value
        elif row.startswith(("1 ", "2 ", "u ")):
            changed += 1
            if row.startswith("u "):
                conflicted += 1
                continue
            xy = row.split(" ", 2)[1]
            staged += int(xy[0] != "."); unstaged += int(xy[1] != ".")
        elif row.startswith("? "):
            changed += 1; untracked += 1
    branch, commit = headers.get("branch.head"), headers.get("branch.oid")
    detached, unborn = branch == "(detached)", commit == "(initial)"
    ahead = behind = None
    if "branch.ab" in headers:
        parts = headers["branch.ab"].split()
        if len(parts) == 2: ahead, behind = abs(int(parts[0])), abs(int(parts[1]))
    return {"available": True, "root": root.rstrip("\r\n"), "branch": None if detached else branch,
        "commit": None if unborn else commit, "detached": detached, "unborn": unborn, "dirty": changed > 0,
        "changed": changed, "staged": staged, "unstaged": unstaged, "untracked": untracked, "conflicted": conflicted,
        "upstream": headers.get("branch.upstream"), "ahead": ahead, "behind": behind}


async def git_changes(cwd):
    try:
        code, root, _ = await git(cwd, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        return {"available": False, "reason": "未安装 Git", "files": []}
    if code:
        return {"available": False, "reason": "当前工作目录不是 Git 仓库", "files": []}
    code, status, error = await git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", ".")
    if code:
        raise ValueError(error or "无法读取 Git 状态")
    head = (await git(cwd, "rev-parse", "--verify", "HEAD"))[0] == 0
    stats = {}
    if head:
        code, numbers, error = await git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", "HEAD", "--", ".")
        if code:
            raise ValueError(error or "无法读取 Git 行数")
        for row in numbers.split("\0"):
            parts = row.split("\t", 2)
            if len(parts) == 3:
                stats[parts[2]] = (int(parts[0]), int(parts[1])) if parts[0].isdigit() and parts[1].isdigit() else (None, None)
    files = []
    for row in status.split("\0"):
        if len(row) < 4:
            continue
        state, relative = row[:2], row[3:]
        path = Path(root.rstrip("\r\n")) / relative
        if not path.resolve().is_relative_to(Path(cwd).resolve()):
            continue
        added, removed = stats.get(relative, (0, 0))
        if not head or state == "??":
            snapshot = read_snapshot(path)
            if snapshot is not None and "\0" not in snapshot:
                added, removed = len(snapshot.splitlines()), 0
            else:
                added = removed = None
        record = describe_change(path, None, None, "delete" if "D" in state else "create" if "A" in state or state == "??" else "edit")
        record.update(added=added, removed=removed, statsAvailable=added is not None, source="git", status=state.strip())
        files.append(record)
    return {"available": True, "root": root.rstrip("\r\n"), "files": files[:500], "truncated": len(files) > 500}


async def git_file_diff(cwd, requested):
    workspace_path(cwd, requested)
    # Keep Git's lexical path: a tracked symlink and its target have different diffs.
    path = Path(os.path.abspath(Path(cwd) / requested))
    snapshot = await git_changes(cwd)
    entry = next((item for item in snapshot["files"] if os.path.normcase(os.path.abspath(item["path"])) == os.path.normcase(str(path))), None)
    if not entry:
        raise ValueError("文件当前没有 Git 工作区差异，请刷新变更列表")
    head = (await git(cwd, "rev-parse", "--verify", "HEAD"))[0] == 0
    if not head or entry["status"] == "??":
        content = read_snapshot(path)
        if content is not None and "\0" in content:
            content = None
        return {**entry, **describe_change(path, "", content, entry["operation"]), "source": "git"}
    relative = path.relative_to(Path(snapshot["root"]).resolve()).as_posix()
    code, diff, error = await git(snapshot["root"], "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "HEAD", "--", relative)
    if code:
        raise ValueError(error or "无法读取文件差异")
    return {**entry, "diff": diff[:SNAPSHOT_LIMIT], "truncated": len(diff) > SNAPSHOT_LIMIT}
