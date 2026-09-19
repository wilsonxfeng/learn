import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

gen = load("generate_system", "scripts/generate_system.py")
mig = load("migrate_legacy", "scripts/migrate_legacy.py")

class GeneratorTests(unittest.TestCase):
    def spec(self, root, *, cold=True):
        (root / "source.md").write_text("authoritative\n", encoding="utf-8")
        today = date.today().isoformat()
        return {
            "topic": "Test course", "project": {"id": "course", "path": "Course"},
            "score_dimensions": ["E", "P", "R", "T"],
            "score_rubric": {d: {str(i): f"{d}{i}" for i in range(4)} for d in ["E", "P", "R", "T"]},
            "authority_inputs": [{"path": "source.md"}],
            "prior_exposure": ["A"] if cold else [],
            "initial_cold_due_date": today if cold else None,
            "modules": [{"id": "A", "title": "A", "prerequisites": [],
                         "packet": {"sources": [{"path": "source.md"}], "sections": ["lines 1-2"]},
                         "exit_target": "Demonstrate A.", "initial_mode": "cold" if cold else "learn"}],
        }

    def test_validate_and_register_boundary_publishes_expected_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); spec = self.spec(root)
            called = []
            def fake(r, e, manifest, initial):
                called.append((r, e, manifest, json.loads(initial.read_text())))
            result = gen.generate_from_spec if False else None
            spec_path = root / "spec.json"; spec_path.write_text(json.dumps(spec), encoding="utf-8")
            result = gen.generate(spec_path, root, register=fake)
            project = root / "Course"
            self.assertEqual(result["cold_reviews"], 1)
            self.assertTrue((project / "Learning.md").is_file())
            manifest = json.loads((project / "learning-system/curriculum.json").read_text())
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["modules"][0]["initial_mode"], "cold")
            state = called[0][3]["projects"]["course"]
            self.assertEqual(state["modules"]["A"]["scores"], [0, 0, 0, 0])
            self.assertIsNone(state["modules"]["A"]["initial_exit_passed_on"])
            self.assertEqual(state["reviews"][0]["due"], date.today().isoformat())

    def test_missing_engine_refuses_before_collision_or_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); spec_path = root / "spec.json"; spec_path.write_text(json.dumps(self.spec(root)), encoding="utf-8")
            with self.assertRaises(RuntimeError): gen.generate(spec_path, root, engine=str(root / "missing-engine.py"))
            self.assertFalse((root / "Course").exists())

    def test_register_failure_removes_new_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); spec_path = root / "spec.json"; spec_path.write_text(json.dumps(self.spec(root)), encoding="utf-8")
            def fail(*args): raise RuntimeError("engine rejected")
            with self.assertRaisesRegex(RuntimeError, "engine rejected"): gen.generate(spec_path, root, register=fail)
            self.assertFalse((root / "Course").exists())

    def test_validation_rejects_cycle_and_unapproved_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "source.md").write_text("x", encoding="utf-8")
            s = self.spec(root); s["modules"][0]["prerequisites"] = ["B"]
            with self.assertRaises(gen.SpecError): gen.validate_spec(s, root)
            s = self.spec(root); s["authority_inputs"] = [{"path": "../outside.md"}]
            with self.assertRaises(gen.SpecError): gen.validate_spec(s, root)

    def test_real_engine_register_integration(self):
        engine = ROOT / "scripts" / "learning_system.py"
        if not engine.exists():
            self.skipTest("parallel engine not installed yet")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); s = self.spec(root, cold=False)
            s["initial_cold_due_date"] = None
            spec_path = root / "spec.json"; spec_path.write_text(json.dumps(s), encoding="utf-8")
            result = gen.generate(spec_path, root, engine=str(engine))
            self.assertEqual(result["project_id"], "course")
            state = json.loads((root / ".learning/state.json").read_text())
            self.assertIn("course", state["projects"])
            self.assertTrue((root / "Course/learning-system/curriculum.json").exists())

class MigrationTests(unittest.TestCase):
    def test_dry_run_and_apply_preserve_controller_and_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); project = root / "Course"; (project / "learning-sessions").mkdir(parents=True)
            raw = b"# transcript\n\xff\n"
            (project / "learning-sessions/log.md").write_bytes(raw)
            controller = """---\ntitle: Course\nprior_coverage: Chapter 1\nactive_module: null\nreview_calendar:\n  - id: A-cold\n    module: A\n    kind: cold\n    due: 2026-09-19\n    status: scheduled\ncompletion_journal: []\n---\n|ID|Source packet|Module|Prerequisite|State|Score|\n|---|---|---|---|---|---|\n|A|source.txt:1-2|Foundations|None|REVIEW_DUE|0/0/0/0|\n"""
            old = project / "Learning.md"; old.write_text(controller, encoding="utf-8")
            (project / "content").mkdir(); (project / "content/source.txt").write_text("source", encoding="utf-8")
            _, plan = mig.inspect_controller(root, old)
            report = mig.migrate(plan, root)
            self.assertEqual(report["status"], "dry-run")
            self.assertEqual(old.read_text(), controller)
            def fake(*args): pass
            mig.migrate(plan, root, apply=True, register=fake)
            self.assertEqual((project / "learning-sessions/log.md").read_bytes(), raw)
            self.assertEqual((project / "learning-system/archive/legacy/Learning.md").read_bytes(), old.read_bytes() if False else controller.encode())
            self.assertTrue((project / "learning-system/curriculum.json").exists())

if __name__ == "__main__": unittest.main()
