"""Regression tests for the deterministic scheduler contract."""
import contextlib
import io
import json
import tempfile
import unittest
import sys
from datetime import date
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # keep the regression file runnable with stdlib only
    from contextlib import contextmanager
    class _Pytest:
        @staticmethod
        @contextmanager
        def raises(expected):
            try:
                yield
            except expected:
                return
            raise AssertionError(f"expected {expected.__name__}")
    pytest = _Pytest()

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import learning_system as ls


def setup_vault(tmp_path, *, modules=None, project="Demo"):
    root = tmp_path
    (root / ".learning").mkdir()
    modules = modules or [
        {"id": "A-01", "title": "A", "prerequisites": [], "packet": f"{project}/learning-system/modules/A-01.md", "artifact": f"{project}/learning-artifacts/A-01.md", "blockers": [], "initial_mode": "learn"},
        {"id": "A-02", "title": "B", "prerequisites": ["A-01"], "packet": f"{project}/learning-system/modules/A-02.md", "artifact": f"{project}/learning-artifacts/A-02.md", "blockers": [], "initial_mode": "learn"},
    ]
    manifest = {"schema_version": 2, "id": project, "title": project,
                "project_path": project, "teaching_packet": f"{project}/learning-system/Teaching.md",
                "score_dimensions": ["E", "P", "R", "T"], "modules": modules}
    mp = root / project / "learning-system" / "curriculum.json"
    mp.parent.mkdir(parents=True)
    mp.write_text(json.dumps(manifest), encoding="utf-8")
    (root / project / "learning-system" / "Teaching.md").write_text("teaching", encoding="utf-8")
    for m in modules:
        (root / m["packet"]).parent.mkdir(parents=True, exist_ok=True)
        (root / m["packet"]).write_text(m["id"], encoding="utf-8")
        (root / m["artifact"]).parent.mkdir(parents=True, exist_ok=True)
        (root / m["artifact"]).write_text("evidence", encoding="utf-8")
    ls.register(root, str(mp))
    return root


def clock(d):
    return lambda: d


def state(root):
    return json.loads((root / ".learning/state.json").read_text())


def complete_first(root, day=date(2026, 9, 19), session="s1"):
    p = ls.prepare(root, "Demo", session, clock=clock(day))
    ls.confirm_log(root, "Demo", session, p["path"])
    b = ls.begin(root, "Demo", session, clock=clock(day))
    out = root / "out.json"
    out.write_text(json.dumps({"task_id": b["task_id"], "outcome": "passed", "scores": [2, 2, 2, 2], "exit_passed": True, "evidence_path": "Demo/learning-artifacts/A-01.md", "summary": "pass"}))
    return b, ls.finish(root, "Demo", session, "out.json", clock=clock(day))


def test_register_and_read_only_check(tmp_path):
    root = setup_vault(tmp_path)
    before = state(root)
    result = ls.check(root, "Demo", clock=clock(date(2026, 9, 19)))
    assert result["verdict"] == "READY" and result["module"] == "A-01"
    assert state(root) == before
    with pytest.raises(ls.ConflictError):
        ls.register(root, "Demo/learning-system/curriculum.json")


def test_five_same_day_passes_and_interval(tmp_path):
    # Five independent modules can all pass on one local calendar day.
    mods = [{"id": f"M-{i}", "title": str(i), "prerequisites": [], "packet": f"C/learning-system/modules/M-{i}.md", "artifact": f"C/learning-artifacts/M-{i}.md", "blockers": [], "initial_mode": "learn"} for i in range(5)]
    root = setup_vault(tmp_path, modules=mods, project="C")
    day = date(2026, 9, 19)
    for i in range(5):
        sid = f"s{i}"; p = ls.prepare(root, "C", sid, clock=clock(day)); ls.confirm_log(root, "C", sid, p["path"]); b = ls.begin(root, "C", sid, clock=clock(day))
        artifact = f"C/learning-artifacts/M-{i}.md"
        (root / "o.json").write_text(json.dumps({"task_id": b["task_id"], "outcome": "passed", "scores": [3]*4, "exit_passed": True, "evidence_path": artifact}))
        ls.finish(root, "C", sid, "o.json", clock=clock(day)); ls.settle_log(root, "C", sid, confirmed_unlinked=True)
    s = state(root)["projects"]["C"]
    assert len(s["reviews"]) == 5 and all(r["due"] == "2026-09-20" for r in s["reviews"])


def test_no_early_review_and_date_rollover(tmp_path):
    root = setup_vault(tmp_path); complete_first(root)
    early = ls.check(root, "Demo", clock=clock(date(2026, 9, 19)))
    assert early["module"] == "A-02" and early["due_count"] == 0
    due = ls.check(root, "Demo", clock=clock(date(2026, 9, 20)))
    assert due["kind"] == "review" and due["review_id"] == "A-01-R1"


def test_partial_over_midnight_and_late_rolling_interval(tmp_path):
    root = setup_vault(tmp_path); b, _ = complete_first(root)
    ls.settle_log(root, "Demo", "s1", confirmed_unlinked=True)
    p = ls.prepare(root, "Demo", "s2", clock=clock(date(2026, 9, 20))); ls.confirm_log(root, "Demo", "s2", p["path"]); b = ls.begin(root, "Demo", "s2", clock=clock(date(2026, 9, 20)))
    (root / "paused.json").write_text(json.dumps({"task_id": b["task_id"], "outcome": "paused", "scores": [1,2,1,2], "exit_passed": False, "evidence_path": "Demo/learning-artifacts/A-01.md", "resume_action": "fresh check"}))
    ls.finish(root, "Demo", "s2", "paused.json", clock=clock(date(2026, 9, 20)))
    resumed = ls.check(root, "Demo", clock=clock(date(2026, 9, 21)))
    assert resumed["task_id"] == b["task_id"]  # active work remains selected
    # finish the same review late; R2 anchors on actual completion date +3.
    (root / "pass.json").write_text(json.dumps({"task_id": b["task_id"], "outcome": "passed", "scores": [2]*4, "exit_passed": True, "evidence_path": "Demo/learning-artifacts/A-01.md"}))
    ls.finish(root, "Demo", "s2", "pass.json", clock=clock(date(2026, 9, 25)))
    assert next(r for r in state(root)["projects"]["Demo"]["reviews"] if r["module"] == "A-01")["due"] == "2026-09-28"


def test_mastery_floor_and_initial_readiness(tmp_path):
    root = setup_vault(tmp_path)
    p = ls.prepare(root, "Demo", "s", clock=clock(date(2026, 9, 19))); ls.confirm_log(root, "Demo", "s", p["path"]); b = ls.begin(root, "Demo", "s", clock=clock(date(2026, 9, 19)))
    (root / "bad.json").write_text(json.dumps({"task_id": b["task_id"], "outcome": "passed", "scores": [2,2,1,2], "exit_passed": True, "evidence_path": "Demo/learning-artifacts/A-01.md"}))
    with pytest.raises(ls.ValidationError): ls.finish(root, "Demo", "s", "bad.json", clock=clock(date(2026,9,19)))
    assert state(root)["projects"]["Demo"]["active"] is not None
    (root / "good.json").write_text(json.dumps({"task_id": b["task_id"], "outcome": "passed", "scores": [2]*4, "exit_passed": True, "evidence_path": "Demo/learning-artifacts/A-01.md"}))
    ls.finish(root, "Demo", "s", "good.json", clock=clock(date(2026,9,19)))
    ms = state(root)["projects"]["Demo"]["modules"]["A-01"]
    assert ms["prerequisite_ready"] is True and ms["mastery"] == "unverified"


def test_idempotent_and_conflicting_retry(tmp_path):
    root = setup_vault(tmp_path); b, _ = complete_first(root)
    retry = ls.finish(root, "Demo", "s1", "out.json", clock=clock(date(2026, 9, 20)))
    assert retry["verdict"] == "COMMITTED"
    raw = json.loads((root / "out.json").read_text()); raw["scores"] = [3]*4; (root / "out2.json").write_text(json.dumps(raw))
    with pytest.raises(ls.ConflictError): ls.finish(root, "Demo", "s1", "out2.json", clock=clock(date(2026, 9, 20)))


def test_log_handshake_stale_and_recovery(tmp_path):
    root = setup_vault(tmp_path)
    p = ls.prepare(root, "Demo", "old", clock=clock(date(2026,9,19)))
    # An empty, never-confirmed setup can be superseded after the date changes.
    p2 = ls.prepare(root, "Demo", "new", clock=clock(date(2026,9,20)))
    assert p2["path"] != p["path"]
    ls.confirm_log(root, "Demo", "new", p2["path"]); ls.begin(root, "Demo", "new", clock=clock(date(2026,9,20)))
    r = ls.resume(root, "Demo", "other", confirmed_handoff=True, clock=clock(date(2026,9,20)))
    assert r["task_id"] == p2["task_id"] and r["path"] != p2["path"]


def test_path_escape_duplicate_cycle_and_utf8_space(tmp_path):
    root = setup_vault(tmp_path)
    with pytest.raises(ls.ValidationError): ls._safe_rel("../secret")
    # Validation catches a cycle in a manifest before registration.
    m = json.loads((root / "Demo/learning-system/curriculum.json").read_text())
    m["modules"][0]["prerequisites"] = ["A-02"]
    (root / "Demo/learning-system/cycle.json").write_text(json.dumps(m))
    with pytest.raises(ls.ValidationError): ls.register(root, "Demo/learning-system/cycle.json")


def test_scoped_agenda_and_cli_readonly_today(tmp_path, capsys):
    root = setup_vault(tmp_path)
    out = ls.agenda(root, today="2026-09-19")
    assert out["as_of"] == "2026-09-19" and out["projects"]
    assert ls.main(["--root", str(root), "--today", "2026-09-19", "check", "--project", "Demo"]) == 0
    captured = capsys.readouterr(); assert "READY" in captured.out
    assert ls.main(["--root", str(root), "--today", "2026-09-19", "prepare", "--project", "Demo", "--session", "s"]) != 0


class TestLearningSystemStdlib(unittest.TestCase):
    """stdlib discovery adapter; the functions above remain pytest-friendly."""
    def _run(self, fn):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))

    def test_register_check(self): self._run(test_register_and_read_only_check)
    def test_five_passes(self): self._run(test_five_same_day_passes_and_interval)
    def test_no_early(self): self._run(test_no_early_review_and_date_rollover)
    def test_rollover(self): self._run(test_partial_over_midnight_and_late_rolling_interval)
    def test_mastery(self): self._run(test_mastery_floor_and_initial_readiness)
    def test_idempotency(self): self._run(test_idempotent_and_conflicting_retry)
    def test_recovery(self): self._run(test_log_handshake_stale_and_recovery)
    def test_validation(self): self._run(test_path_escape_duplicate_cycle_and_utf8_space)

    def test_cli_readonly(self):
        class Capture:
            def readouterr(self): return type("Output", (), {"out": self.out.getvalue(), "err": self.err.getvalue()})()
        with tempfile.TemporaryDirectory() as td:
            cap = Capture(); cap.out = io.StringIO(); cap.err = io.StringIO()
            with contextlib.redirect_stdout(cap.out), contextlib.redirect_stderr(cap.err):
                test_scoped_agenda_and_cli_readonly_today(Path(td), cap)


if __name__ == "__main__":
    unittest.main()
