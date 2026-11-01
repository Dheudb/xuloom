import os
import shutil
import git

class SandboxManager:
    """
    Workspace change isolation using Git worktrees or directory copies.
    This does not restrict host processes, network access or arbitrary filesystem access.
    """
    def __init__(self, sandbox_root=".xuloom_sandbox", *, cwd=None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.sandbox_root = sandbox_root
        self.active_sandbox = None

    def _ignored_names(self):
        return {
            ".git",
            self.sandbox_root,
            ".xuloom",
            "__pycache__",
            "node_modules",
            "venv",
            "env",
            "dist",
            "build",
        }

    def _get_workspace_root(self):
        try:
            repo = git.Repo(self.cwd, search_parent_directories=True)
            return repo.working_dir, True
        except git.InvalidGitRepositoryError:
            return self.cwd, False

    def _make_filesystem_sandbox_root(self, root_dir: str):
        parent = os.path.dirname(root_dir)
        base = os.path.basename(root_dir.rstrip("\\/")) or "workspace"
        return os.path.join(parent, f"{base}_{self.sandbox_root.strip('.')}")

    def _copy_workspace_tree(self, source_root: str, target_root: str):
        ignored = self._ignored_names()
        for current_root, dirs, files in os.walk(source_root):
            rel_root = os.path.relpath(current_root, source_root)
            dirs[:] = [d for d in dirs if d not in ignored]
            if rel_root == ".":
                rel_root = ""
            dest_root = os.path.join(target_root, rel_root) if rel_root else target_root
            os.makedirs(dest_root, exist_ok=True)
            for file_name in files:
                if file_name in ignored:
                    continue
                src_file = os.path.join(current_root, file_name)
                dst_file = os.path.join(dest_root, file_name)
                shutil.copy2(src_file, dst_file)

    def _create_git_sandbox(self, root_dir: str, branch_name: str, sb_path: str) -> str:
        repo = git.Repo(root_dir)
        repo.git.worktree('add', '-b', branch_name, sb_path)
        self.active_sandbox = {
            "branch": branch_name,
            "path": sb_path,
            "root": root_dir,
            "mode": "git_worktree",
        }
        return (
            f"Sandbox created as git worktree on branch '{branch_name}'. "
            f"Supported workspace paths resolve to '{sb_path}'. This is workspace isolation, not an OS security sandbox."
        )

    def _create_filesystem_sandbox(self, root_dir: str, branch_name: str, sb_path: str) -> str:
        os.makedirs(os.path.dirname(sb_path), exist_ok=True)
        self._copy_workspace_tree(root_dir, sb_path)
        self.active_sandbox = {
            "branch": branch_name,
            "path": sb_path,
            "root": root_dir,
            "mode": "filesystem_clone",
        }
        return (
            f"Sandbox created as filesystem clone '{branch_name}'. "
            f"Workspace paths resolve into '{sb_path}'. This does not restrict host or network access. Merge copies files back into the original workspace."
        )
        
    def create_sandbox(self, branch_name: str) -> str:
        root_dir, is_git_repo = self._get_workspace_root()
        if self.active_sandbox:
            return f"Error: Sandbox '{self.active_sandbox['branch']}' is already active."

        sandbox_base = (
            os.path.join(root_dir, self.sandbox_root)
            if is_git_repo
            else self._make_filesystem_sandbox_root(root_dir)
        )
        sb_path = os.path.join(sandbox_base, branch_name)
        if os.path.exists(sb_path):
            return f"Error: Sandbox branch {branch_name} already actively mounted."
            
        try:
            if is_git_repo:
                os.makedirs(os.path.join(root_dir, self.sandbox_root), exist_ok=True)
                return self._create_git_sandbox(root_dir, branch_name, sb_path)
            return self._create_filesystem_sandbox(root_dir, branch_name, sb_path)
        except Exception as e:
            return f"Error creating Git worktree boundary: {e}"
            
    def abort_sandbox(self) -> str:
        """Detonates the current sandbox and restores reality."""
        if not self.active_sandbox: return "Error: No active sandbox currently mounted."
        try:
            root_dir = self.active_sandbox["root"]
            sb_path = self.active_sandbox["path"]
            branch = self.active_sandbox["branch"]
            mode = self.active_sandbox.get("mode", "git_worktree")

            if mode == "git_worktree":
                repo = git.Repo(root_dir)
                repo.git.worktree('remove', '--force', sb_path)
                try:
                    repo.git.branch('-D', branch)
                except git.GitCommandError:
                    pass
            else:
                shutil.rmtree(sb_path, ignore_errors=True)
                
            self.active_sandbox = None
            return f"Sandbox '{branch}' was successfully discarded. Back to reality."
        except Exception as e:
            return f"Crashed while trying to exit sandbox: {e}"

    def _merge_filesystem_sandbox(self, root_dir: str, sb_path: str):
        ignored = self._ignored_names()
        for current_root, dirs, files in os.walk(sb_path):
            rel_root = os.path.relpath(current_root, sb_path)
            dirs[:] = [d for d in dirs if d not in ignored]
            if rel_root == ".":
                rel_root = ""
            dest_root = os.path.join(root_dir, rel_root) if rel_root else root_dir
            os.makedirs(dest_root, exist_ok=True)
            for file_name in files:
                if file_name in ignored:
                    continue
                src_file = os.path.join(current_root, file_name)
                dst_file = os.path.join(dest_root, file_name)
                shutil.copy2(src_file, dst_file)
            
    def merge_sandbox(self) -> str:
        """Promotes experimental chaos back into main branch upon user/AI approval."""
        if not self.active_sandbox: return "Error: No active sandbox currently mounted."
        try:
            root_dir = self.active_sandbox["root"]
            sb_path = self.active_sandbox["path"]
            branch = self.active_sandbox["branch"]
            mode = self.active_sandbox.get("mode", "git_worktree")
            
            if mode == "git_worktree":
                # Commit pending artifacts inside shadow realm
                sb_repo = git.Repo(sb_path)
                sb_repo.git.add(all=True)
                if sb_repo.is_dirty() or sb_repo.untracked_files:
                    sb_repo.git.commit(m=f"Auto-Merge: Xuloom Shadow Actions verified and lifted from '{branch}'")

                # Perform surgical transplant back into native realm
                main_repo = git.Repo(root_dir)
                current_main_branch = main_repo.active_branch.name
                main_repo.git.merge(branch)

                main_repo.git.worktree('remove', '--force', sb_path)
                main_repo.git.branch('-d', branch)
            else:
                current_main_branch = "filesystem-workspace"
                self._merge_filesystem_sandbox(root_dir, sb_path)
                shutil.rmtree(sb_path, ignore_errors=True)
            
            self.active_sandbox = None
            return (
                f"Changes from sandbox '{branch}' successfully integrated into '{current_main_branch}'. "
                "Sandbox dismantled."
            )
        except Exception as e:
            return f"Critical transplant failure! Sandbox remains active. Fix merge collision manually: {e}"
            
    def resolve_path(self, path_value: str) -> str:
        """Map workspace paths without confusing similarly named sibling directories."""
        if not path_value:
            return path_value
        if not self.active_sandbox:
            return os.path.abspath(os.path.join(self.cwd, path_value))
            
        root = self.active_sandbox["root"]
        sb_path = self.active_sandbox["path"]

        abs_p = os.path.abspath(path_value)
        if not os.path.isabs(path_value):
            return os.path.abspath(os.path.join(sb_path, path_value))

        # If absolute path points inside root workspace...
        def contains(base):
            try:
                return os.path.normcase(os.path.commonpath([abs_p, base])) == os.path.normcase(os.path.abspath(base))
            except ValueError:  # Different Windows drives.
                return False
        if contains(root) and not contains(sb_path):
            rel = os.path.relpath(abs_p, root)
            return os.path.join(sb_path, rel)

        return abs_p

    def resolve_working_directory(self, cwd: str = None) -> str:
        if not self.active_sandbox:
            return os.path.abspath(os.path.join(self.cwd, cwd or "."))
        if not cwd:
            return self.active_sandbox["path"]
        return self.resolve_path(cwd)

    def get_status(self) -> dict:
        if not self.active_sandbox:
            return {
                "active": False,
                "mode": None,
                "branch": None,
                "path": None,
                "root": None,
            }

        status = {
            "active": True,
            "mode": self.active_sandbox.get("mode"),
            "branch": self.active_sandbox.get("branch"),
            "path": self.active_sandbox.get("path"),
            "root": self.active_sandbox.get("root"),
        }

        if self.active_sandbox.get("mode") == "git_worktree":
            try:
                repo = git.Repo(self.active_sandbox["path"])
                status["dirty"] = repo.is_dirty()
                status["untracked_files"] = len(repo.untracked_files)
                status["head_commit"] = repo.head.commit.hexsha[:7]
            except Exception:
                pass

        return status

    def export_state(self) -> dict:
        return self.get_status() if self.active_sandbox else {}

    def load_state(self, payload: dict):
        if not isinstance(payload, dict) or not payload:
            self.active_sandbox = None
            return

        branch = payload.get("branch")
        path = payload.get("path")
        root = payload.get("root")
        mode = payload.get("mode", "git_worktree")
        if branch and path and root:
            self.active_sandbox = {
                "branch": branch,
                "path": path,
                "root": root,
                "mode": mode,
            }
        else:
            self.active_sandbox = None
