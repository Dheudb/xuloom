import os
from pathlib import Path
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool

class GlobToolInput(BaseModel):
    path: str = Field(None, description="Directory to explore. Defaults to current directory.")
    pattern: str = Field(None, description="Optional simplistic glob pattern (e.g. *.py) to filter results.")

class GlobTool(BaseAgenticTool):
    workspace_paths = {"path": True}
    name = "glob_tool"
    description = "Fast file pattern matching tool that works with any codebase size. Supports glob patterns like '**/*.js' or 'src/**/*.ts'. Returns matching file paths sorted by modification time. Use this tool when you need to find files by name patterns. When you are doing an open ended search that may require multiple rounds of globbing and grepping, use agent_tool instead."
    input_schema = GlobToolInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        return (
            "Fast file pattern matching tool that works with any codebase size. "
            "Supports glob patterns like '**/*.js' or 'src/**/*.ts'. "
            "Returns matching file paths sorted by modification time. "
            "Use this tool when you need to find files by name patterns. "
            "When you are doing an open-ended search that may require multiple "
            "rounds of globbing and grepping, use agent_tool instead."
        )

    async def execute(self, path: str = None, pattern: str = None) -> str:
        base_path = Path(path or os.getcwd())
        
        if not base_path.exists() or not base_path.is_dir():
            return f"Error: '{base_path}' is not a valid directory path."
            
        # Ignore noisy heavy vendor/compiled directories
        ignore_dirs = {".git", "node_modules", "venv", "env", "__pycache__", ".idea", ".vscode", "dist", "build"}
        
        matches = []
        try:
            # We enforce an implicit depth of 3 strictly if scanning wildly without a pattern
            # to avoid returning 100,000 files in massive monorepos
            for root, dirs, files in os.walk(base_path):
                # Prune in-place ensures os.walk skips deeply exploring them entirely!
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                
                rel_root = os.path.relpath(root, base_path)
                if rel_root == ".":
                    rel_root = ""
                    
                depth = rel_root.count(os.sep) if rel_root else 0
                if depth > 6 and not pattern:
                    # Skip plunging too deep if we're just blindly mapping territory
                    continue
                    
                if rel_root:
                    matches.append(f"📁 {rel_root}/")
                    
                for f in files:
                    if not pattern or f.endswith(pattern.replace('*','')):
                        matches.append(f"   📄 {os.path.join(rel_root, f) if rel_root else f}")
                        
                # Soft limit aligned with Claude Code's maxResultSizeChars: 100_000
                # (~50 chars/line × 2000 items ≈ 100K)
                if len(matches) > 2000:
                    matches.append("\n... [Tree list truncated at 2000 items. Use a glob pattern or navigate to a specific sub-directory.] ...")
                    break
                    
            if not matches:
                return "Directory is empty or no files matched the layout constraints."
                
            return "\\n".join(matches)
            
        except Exception as e:
            return f"Error exploring local directory map: {str(e)}"
