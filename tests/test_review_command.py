import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.features.review import parse, review_request


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


class ReviewCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(self.temp.name) / "data"),
                                      "XULOOM_CONFIG_DIR": str(Path(self.temp.name) / "config")})
        env.start()
        self.addCleanup(env.stop)

    def commit(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
        git(self.root, "add", name)
        git(self.root, "commit", "-q", "-m", name)
        return git(self.root, "rev-parse", "HEAD")

    def test_parse_separates_target_and_notes(self):
        self.assertEqual(parse(""), ("", ""))
        self.assertEqual(parse(" HEAD~2..HEAD "), ("HEAD~2..HEAD", ""))
        self.assertEqual(parse("-- 重点看并发"), ("", "重点看并发"))
        self.assertEqual(parse("main...HEAD -- check errors"), ("main...HEAD", "check errors"))
        with self.assertRaises(ValueError):
            parse("main HEAD")

    async def test_current_changes_require_a_dirty_worktree(self):
        self.commit("a.txt", "one\n")
        with self.assertRaisesRegex(ValueError, "No uncommitted changes"):
            await review_request(self.root, "")
        (self.root / "new.txt").write_text("draft\n", encoding="utf-8")
        prompt, options = await review_request(self.root, " -- focus on data loss")
        self.assertEqual(options, {"selected_skills": ["code-review"]})
        self.assertIn("uncommitted Git changes", prompt)
        self.assertIn("git diff HEAD", prompt)
        self.assertTrue(prompt.endswith("Reviewer notes: focus on data loss"))

    async def test_commit_and_range_are_pinned_to_hashes(self):
        first = self.commit("a.txt", "one\n")
        second = self.commit("a.txt", "two\n")
        prompt, _ = await review_request(self.root, "HEAD")
        self.assertIn(f"git show {second}", prompt)
        prompt, _ = await review_request(self.root, "HEAD~1..")
        self.assertIn("1 commit(s)", prompt)
        self.assertIn(f"git diff {first}..{second}", prompt)
        prompt, _ = await review_request(self.root, f"{first}...HEAD")
        self.assertIn(f"git diff {first}...{second}", prompt)

    async def test_invalid_targets_fail_before_any_model_request(self):
        self.commit("a.txt", "one\n")
        for target in ("missing-branch", "--output=x", "HEAD..HEAD"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await review_request(self.root, target)

    async def test_review_turn_loads_the_skill_and_keeps_the_typed_command(self):
        from xuloom import QueryEngine
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response
        commit = self.commit("a.txt", "one\n")
        prompt, options = await review_request(self.root, "HEAD")
        model = ScriptedModel(default=model_response(text="No findings."))
        with model:
            async with QueryEngine(model.config(cwd=str(self.root))) as engine:
                events = [event async for event in engine.submit_message(
                    prompt, sys_print_callback=lambda _: None, display_text="/review HEAD", **options)]
        sent = str(model.requests[0].messages[-1]["content"])
        self.assertIn(f"git show {commit}", sent)
        self.assertIn("<selected-skills>", sent)
        self.assertIn("Skill: code-review", sent)
        self.assertIn({"type": "skill_loaded", "name": "code-review"},
                      [{key: event.get(key) for key in ("type", "name")} for event in events if event.get("type") == "skill_loaded"])

    async def test_outside_a_repository_reports_the_reason(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaisesRegex(ValueError, "Git"):
                await review_request(plain, "")


if __name__ == "__main__":
    unittest.main()
