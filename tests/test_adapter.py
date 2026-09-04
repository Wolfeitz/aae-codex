from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from aae_codex.cli import PROJECTION_MARKER, handle_hook, install, sync_skills


class CodexAdapterTests(unittest.TestCase):
    def test_init_installs_native_files_and_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / ".codex/hooks.json"
            hooks.parent.mkdir(parents=True)
            hooks.write_text('{"existing": true}', encoding="utf-8")
            self.assertEqual(install(root), 0)
            self.assertEqual(hooks.read_text(), '{"existing": true}')
            self.assertTrue((root / ".codex/agents/aae-independent-reviewer.toml").is_file())

    def test_skill_projection_is_native_and_does_not_replace_foreign_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / ".aae/skills/repo-recon"
            source.mkdir(parents=True)
            (source / "skill.json").write_text(
                json.dumps({"name": "repo-recon", "description": "Inspect the repository"}),
                encoding="utf-8",
            )
            (source / "SKILL.md").write_text("# Procedure\n", encoding="utf-8")
            self.assertEqual(sync_skills(root), [])
            projected = root / ".agents/skills/repo-recon/SKILL.md"
            self.assertIn(PROJECTION_MARKER, projected.read_text())
            projected.write_text("hand-authored", encoding="utf-8")
            errors = sync_skills(root)
            self.assertTrue(errors)
            self.assertEqual(projected.read_text(), "hand-authored")

    def test_hook_passes_sanitized_file_facts_to_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aae = root / ".aae"
            aae.mkdir()
            (aae / "hooks.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "rules": [{
                        "id": "check-python",
                        "on": "files-changed",
                        "paths": ["src/**/*.py"],
                        "run_check": [sys.executable, "-c", "raise SystemExit(0)"],
                    }],
                }),
                encoding="utf-8",
            )
            secret = "never-persist-this-response"
            output = handle_hook(root, {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "tool_use_id": "tool-1",
                "tool_name": "apply_patch",
                "tool_input": {"command": "*** Update File: src/aae/core.py"},
                "tool_response": secret,
            })
            self.assertIsNone(output)
            record = next((root / ".aae/runtime/hook-events").glob("*.json")).read_text()
            self.assertNotIn(secret, record)
            self.assertIn('"adapter": "codex"', record)


if __name__ == "__main__":
    unittest.main()
