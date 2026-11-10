"""Project-scoped discovery and validation of portable Agent Skills."""
import json
import os
import re
import sys
from pathlib import Path
import yaml
from xuloom.branding import configuration_file


def read_frontmatter(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Missing YAML frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Unclosed YAML frontmatter")
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter must be a mapping")
    return metadata, "\n".join(lines[end + 1:]).strip()


class SkillRegistry:
    def __init__(self, cwd=None, home=None):
        self.cwd = Path(cwd or os.getcwd()).resolve()
        self.home = Path(home or Path.home()).resolve()
        self.settings_path = configuration_file(self.cwd, "capabilities.json")
        # Directories contributed by loaded plugins, kept in memory only.
        self.plugin_roots = {"skills": [], "agents": []}

    def settings(self):
        if not self.settings_path.exists():
            return {"skill_roots": [], "agent_roots": [], "disabled_skills": []}
        payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Capabilities configuration must be an object")
        return payload

    def update(self, **changes):
        payload = self.settings()
        payload.update(changes)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        from xuloom.core.storage import atomic_json
        atomic_json(self.settings_path, payload)
        return payload

    def roots(self, kind="skills"):
        settings = self.settings()
        roots = [(self.cwd / name / kind, "project") for name in (".xuloom", ".agents", ".claude")]
        roots += [(Path(p).expanduser().resolve(), "imported") for p in settings.get("skill_roots" if kind == "skills" else "agent_roots", [])]
        roots += [(Path(p), "plugin") for p in getattr(self, "plugin_roots", {}).get(kind, [])]
        if kind == "skills":
            roots += [(Path(p).expanduser().resolve(), "environment") for p in os.environ.get("XULOOM_SKILLS_PATHS", "").split(os.pathsep) if p]
        roots += [(self.home / name / kind, "global") for name in (".xuloom", ".agents", ".claude")]
        if kind == "skills":
            # Ship actual SKILL.md resources; user/project skills take precedence.
            roots.append((Path(__file__).with_name("builtin"), "builtin"))
        elif kind == "agents":
            # Valid mounted skills may carry their native roles with their resources.
            roots += [(Path(skill["root"]) / "agents", skill["scope"]) for skill in self.active()]
        seen = set()
        for root, scope in roots:
            key = os.path.normcase(str(root.resolve()))
            if key not in seen:
                seen.add(key)
                yield root, scope

    def discover(self):
        entries, seen_paths, active_names = [], set(), set()
        for root, scope in self.roots():
            if not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"})
                if "SKILL.md" not in files:
                    continue
                path = (Path(directory) / "SKILL.md").resolve()
                identity = os.path.normcase(str(path))
                if identity in seen_paths:
                    continue
                seen_paths.add(identity)
                entry = {"id": identity, "name": path.parent.name, "description": "", "path": str(path), "root": str(path.parent), "scope": scope, "valid": True, "error": "", "enabled": True, "shadowed": False, "compatibility": ""}
                try:
                    metadata, _ = read_frontmatter(path)
                    name, description = metadata.get("name"), metadata.get("description")
                    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
                        raise ValueError("name must contain lowercase letters, digits and single hyphens (max 64)")
                    if name != path.parent.name:
                        raise ValueError("name must match the skill directory name")
                    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                        raise ValueError("description must be a non-empty string (max 1024)")
                    entry.update(name=name, description=description.strip(), compatibility=str(metadata.get("compatibility", "")), allowed_tools=metadata.get("allowed-tools", ""))
                    extension = metadata.get("metadata", {})
                    extension = extension.get("xuloom", {}) if isinstance(extension, dict) else {}
                    entry["completion_protocol"] = extension.get("completion-protocol", "") if isinstance(extension, dict) else ""
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    entry.update(valid=False, error=str(exc))
                if entry["valid"] and entry["enabled"]:
                    entry["shadowed"] = entry["name"] in active_names
                    active_names.add(entry["name"])
                entries.append(entry)
        return entries

    def active(self):
        return [s for s in self.discover() if s["valid"] and s["enabled"] and not s["shadowed"]]

    def resolve(self, name):
        matches = [s for s in self.active() if name in (s["name"], s["id"])]
        if not matches:
            raise ValueError("Skill is missing, invalid or shadowed: " + str(name))
        return matches[0]

    def read(self, name):
        skill = self.resolve(name)
        _, body = read_frontmatter(skill["path"])
        return ("Skill: {name}\nSkill directory: {root}\nProject directory: {cwd}\nPython executable: {python}\n"
                "Resolve skill references, assets and script paths relative to the skill directory. "
                "Keep project file operations relative to the project directory. "
                "Check script dependencies before execution. Skill instructions do not grant additional tool permissions.\n\n{body}").format(cwd=self.cwd, python=sys.executable, body=body, **skill)

    def read_resource(self, name, relative_path):
        skill = self.resolve(name)
        root = Path(skill["root"]).resolve()
        resource = (root / relative_path).resolve()
        try:
            resource.relative_to(root)
        except ValueError:
            raise ValueError("Resource must stay inside the skill directory")
        if resource.stat().st_size > 1_000_000:
            raise ValueError("Resource exceeds 1 MB; use a dedicated file tool")
        return resource.read_text(encoding="utf-8")

    def catalog(self):
        return "\n".join("- {}: {}".format(s["name"], s["description"]) for s in self.active())
