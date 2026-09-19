#!/usr/bin/env python3
"""Deterministic, local-only learning scheduler.

The public functions in this module return JSON-serialisable dictionaries and
are intentionally usable by curriculum generators and migration tools.  The
CLI is a thin wrapper around the same functions.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

SCHEMA_VERSION = 2
INTERVALS = (1, 3, 7, 14, 30, 60)
REVIEW_KINDS = {"cold", "legacy", "spaced"}
LOG_STATUSES = {"none", "pending_confirmation", "linked", "awaiting_unlink", "unconfirmed"}
# Lowercase v2 values plus the uppercase legacy labels deliberately retained
# during migration.  Unknown labels are rejected rather than silently
# translating an evidence claim.
MASTERY_VALUES = {"unverified", "mastered", "interview_ready", "MASTERED", "INTERVIEW_READY", "LEARNING", "LOCKED", "READY"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LearningSystemError(Exception):
    """A user-correctable scheduler or input error."""
    code = "learning_error"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(LearningSystemError):
    code = "validation_error"


class ConflictError(LearningSystemError):
    code = "conflict"


class NotFoundError(LearningSystemError):
    code = "not_found"


def _json_load(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise NotFoundError(f"file not found: {path}")
    except (OSError, json.JSONDecodeError) as e:
        raise ValidationError(f"invalid JSON at {path}: {e}")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_rel(value: str, *, label: str = "path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} must be a non-empty relative path")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValidationError(f"{label} escapes its allowed root: {value}")
    return p.as_posix()


def _inside(root: Path, value: str, *, label: str = "path") -> Path:
    rel = _safe_rel(value, label=label)
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValidationError(f"{label} escapes vault: {value}")
    return candidate


def _date(value: Any, *, label: str = "date") -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError(f"{label} must be YYYY-MM-DD")
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"invalid {label}: {value}")


def _today(clock: Optional[Callable[[], Any]] = None) -> _dt.date:
    if clock is None:
        return _dt.date.today()
    value = clock()
    return _date(value, label="clock date")


def _date_str(d: _dt.date) -> str:
    return d.isoformat()


def _default_module() -> dict:
    return {"scores": [0, 0, 0, 0], "prerequisite_ready": False,
            "initial_exit_passed_on": None, "mastery": "unverified", "last_review": None}


def _default_state() -> dict:
    return {"schema_version": 2, "revision": 0, "projects": {}}


def _state_path(root: Path) -> Path:
    return root / ".learning" / "state.json"


def discover_root(start: Optional[Path] = None) -> Path:
    """Find the nearest ancestor containing ``.learning/state.json``."""
    here = (start or Path.cwd()).resolve()
    if here.is_file():
        here = here.parent
    for p in (here, *here.parents):
        if (_state_path(p)).is_file():
            return p
    raise NotFoundError("could not discover vault root; pass --root")


@contextlib.contextmanager
def _locked(root: Path, *, write: bool = False, initialize: bool = False) -> Iterator[dict]:
    root = root.resolve()
    state_file = _state_path(root)
    learning_dir = root / ".learning"
    if learning_dir.is_symlink():
        raise ValidationError(".learning must not be a symlink")
    if not learning_dir.is_dir():
        if write and initialize:
            learning_dir.mkdir(parents=True, exist_ok=False)
        else:
            raise NotFoundError(f"missing .learning directory in vault: {root}")
    lock_file = learning_dir / "state.lock"
    state_file = learning_dir / "state.json"
    if lock_file.is_symlink() or state_file.is_symlink():
        raise ValidationError("state registry and lock must not be symlinks")
    # Read-only commands never create controller files or directories.
    if not lock_file.exists() and not write:
        if not state_file.exists(): raise NotFoundError(f"missing state registry in vault: {state_file}")
        # Read-only inspection must not create a lock file. Mutations still
        # require the durable lock and will create it during registration.
        yield _json_load(state_file)
        return
    with lock_file.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        if state_file.exists():
            state = _json_load(state_file)
        else:
            state = _default_state()
        if write:
            _reconcile_prepared(state)
        original = json.dumps(state, sort_keys=True, ensure_ascii=False)
        yield state
        if write:
            validate_state(root, state)
            changed = json.dumps(state, sort_keys=True, ensure_ascii=False) != original
            if changed:
                state["revision"] = int(state.get("revision", 0)) + 1
                _atomic_write(state_file, state)
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _reconcile_prepared(state: dict) -> None:
    """Finish a durable prepared completion without replaying advancement."""
    for p in state.get("projects", {}).values():
        for event in p.get("completion_journal", []):
            if event.get("state") != "prepared":
                continue
            rid = event.get("consumed_review_id")
            active = p.get("active")
            # Never let a stale journal event clear unrelated newly-started
            # work. Recovery is safe only when task ownership matches.
            if not active or active.get("task_id") != event.get("task_id"):
                continue
            if rid:
                p["reviews"] = [r for r in p.get("reviews", []) if r.get("id") != rid]
            nxt = event.get("next_review")
            if nxt and not any(r.get("id") == nxt.get("id") for r in p.get("reviews", [])):
                p.setdefault("reviews", []).append(nxt)
            p["active"] = None
            if isinstance(p.get("log"), dict):
                p["log"]["status"] = "awaiting_unlink"
            event["state"] = "applied"


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        try:
            dfd = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError:
            pass
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _manifest(root: Path, project: dict) -> tuple[dict, str]:
    path = _inside(root, project["manifest"], label="manifest")
    manifest = _json_load(path)
    validate_manifest(root, manifest)
    return manifest, manifest["id"]


def validate_manifest(root: Path, manifest: dict) -> list[str]:
    """Validate and return manifest errors (raises when used as an API)."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        raise ValidationError("manifest must be an object")
    if manifest.get("schema_version") != 2: errors.append("schema_version must be 2")
    for key in ("id", "title", "project_path", "teaching_packet", "score_dimensions", "modules"):
        if key not in manifest: errors.append(f"manifest missing {key}")
    if errors: raise ValidationError("invalid manifest", details=errors)
    if not isinstance(manifest["id"], str) or not ID_RE.match(manifest["id"]): errors.append("invalid manifest id")
    try: project_path = _safe_rel(manifest["project_path"], label="project_path")
    except LearningSystemError as e: errors.append(e.message); project_path = ""
    for key in ("teaching_packet",):
        try: _safe_rel(manifest[key], label=key)
        except LearningSystemError as e: errors.append(e.message)
    dims = manifest["score_dimensions"]
    if not isinstance(dims, list) or len(dims) != 4 or any(not isinstance(x, str) or not x for x in dims) or len(set(dims)) != 4:
        errors.append("score_dimensions must contain four distinct nonempty strings")
    modules = manifest["modules"]
    if not isinstance(modules, list): errors.append("modules must be a list"); modules = []
    ids: set[str] = set()
    for i, m in enumerate(modules):
        if not isinstance(m, dict): errors.append(f"module {i} must be an object"); continue
        for key in ("id", "title", "prerequisites", "packet", "artifact", "blockers", "initial_mode"):
            if key not in m: errors.append(f"module {i} missing {key}")
        mid = m.get("id")
        if not isinstance(mid, str) or not ID_RE.match(mid): errors.append(f"invalid module id: {mid}")
        elif mid in ids: errors.append(f"duplicate module id: {mid}")
        else: ids.add(mid)
        if m.get("initial_mode") not in ("learn", "cold"): errors.append(f"invalid initial_mode: {mid}")
        for key in ("packet", "artifact"):
            if key in m:
                try: _safe_rel(m[key], label=f"module {mid} {key}")
                except LearningSystemError as e: errors.append(e.message)
        if not isinstance(m.get("prerequisites"), list): errors.append(f"prerequisites must be list: {mid}")
        if not isinstance(m.get("blockers"), list): errors.append(f"blockers must be list: {mid}")
    for m in modules:
        if isinstance(m, dict):
            for pre in m.get("prerequisites", []):
                if pre not in ids: errors.append(f"missing prerequisite {pre} for {m.get('id')}")
    # Detect graph cycles with DFS.
    graph = {m.get("id"): m.get("prerequisites", []) for m in modules if isinstance(m, dict) and m.get("id") in ids}
    visiting: set[str] = set(); visited: set[str] = set()
    def dfs(n: str) -> None:
        if n in visiting: errors.append(f"prerequisite cycle involving {n}"); return
        if n in visited: return
        visiting.add(n)
        for p in graph.get(n, []):
            if p in graph: dfs(p)
        visiting.remove(n); visited.add(n)
    for n in graph: dfs(n)
    if errors: raise ValidationError("invalid manifest", details=errors)
    # Every declared controller/source/evidence path is confined to the
    # registered project. Packets and Teaching.md are generated before
    # registration; artifact files are learner-owned and may be created later,
    # but their parent directory must already be inside the project.
    project_prefix = project_path.rstrip("/") + "/"
    for key in ("teaching_packet",):
        if not (str(manifest[key]) == project_path or str(manifest[key]).startswith(project_prefix)):
            errors.append(f"{key} is outside project_path")
    project_dir = (root / project_path).resolve() if project_path else root
    if project_path and not (root / project_path).is_dir(): errors.append("project_path does not exist")
    for declared in [manifest.get("teaching_packet")] + [m.get("packet") for m in modules if isinstance(m, dict)] + [m.get("artifact") for m in modules if isinstance(m, dict)]:
        if isinstance(declared, str):
            try: _inside(root, declared, label="manifest path").relative_to(project_dir)
            except (LearningSystemError, ValueError): errors.append(f"manifest path resolves outside project: {declared}")
    for m in modules:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str): continue
        for key in ("packet", "artifact"):
            value = m.get(key)
            if isinstance(value, str) and not (value == project_path or value.startswith(project_prefix)):
                errors.append(f"module {m['id']} {key} is outside project_path")
        if isinstance(m.get("packet"), str) and not (root / m["packet"]).is_file(): errors.append(f"module {m['id']} packet is missing")
        if isinstance(m.get("artifact"), str) and not (root / m["artifact"]).parent.is_dir(): errors.append(f"module {m['id']} artifact parent is missing")
    if not (root / manifest["teaching_packet"]).is_file(): errors.append("teaching_packet is missing")
    if errors: raise ValidationError("invalid manifest", details=errors)
    return []


def _review_valid(r: dict, modules: set[str], errors: list[str], idx: int) -> None:
    for key in ("id", "module", "kind", "stage", "due", "status", "started_on"):
        if key not in r: errors.append(f"review {idx} missing {key}")
    if r.get("module") not in modules: errors.append(f"review {idx} references unknown module")
    if r.get("kind") not in REVIEW_KINDS: errors.append(f"review {idx} invalid kind")
    stage = r.get("stage")
    if r.get("kind") == "spaced" and stage not in range(1, 7): errors.append(f"review {idx} invalid spaced stage")
    if r.get("kind") != "spaced" and stage is not None: errors.append(f"review {idx} non-spaced stage must be null")
    try: _date(r.get("due"), label="review due")
    except LearningSystemError as e: errors.append(e.message)
    if r.get("status") not in ("scheduled", "in_progress"): errors.append(f"review {idx} invalid status")


def validate_state(root: Path, state: dict, *, project_id: Optional[str] = None) -> list[str]:
    """Validate state, manifests, references, and scheduler invariants."""
    errors: list[str] = []
    if not isinstance(state, dict) or state.get("schema_version") != 2: errors.append("state schema_version must be 2")
    if not isinstance(state.get("revision"), int) or state.get("revision", -1) < 0: errors.append("revision must be nonnegative integer")
    projects = state.get("projects")
    if not isinstance(projects, dict): errors.append("projects must be an object"); projects = {}
    selected = [project_id] if project_id else list(projects)
    for pid in selected:
        if pid not in projects: errors.append(f"unknown project: {pid}"); continue
        p = projects[pid]
        if not isinstance(p, dict): errors.append(f"project {pid} must be object"); continue
        try:
            manifest, _ = _manifest(root, p)
        except LearningSystemError as e:
            errors.append(f"{pid}: {e.message}"); continue
        if manifest.get("id") != pid: errors.append(f"{pid}: manifest id mismatch")
        try:
            _inside(root, p["manifest"], label="manifest").relative_to((root / manifest["project_path"]).resolve())
        except (LearningSystemError, ValueError, KeyError):
            errors.append(f"{pid}: manifest is outside project")
        mids = {m["id"] for m in manifest["modules"]}
        mods = p.get("modules")
        if not isinstance(mods, dict): errors.append(f"{pid}: modules must be object"); mods = {}
        if set(mods) != mids: errors.append(f"{pid}: module state does not match manifest")
        for mid, ms in mods.items():
            if not isinstance(ms, dict): errors.append(f"{pid}/{mid}: state must be object"); continue
            scores = ms.get("scores")
            if not isinstance(scores, list) or len(scores) != 4 or any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 3 for x in scores): errors.append(f"{pid}/{mid}: invalid scores")
            if ms.get("mastery", "unverified") not in MASTERY_VALUES: errors.append(f"{pid}/{mid}: invalid mastery label")
            if ms.get("prerequisite_ready") and (not isinstance(scores, list) or len(scores) != 4 or any(x < 2 for x in scores) or not ms.get("initial_exit_passed_on")):
                errors.append(f"{pid}/{mid}: prerequisite readiness lacks a score/exit gate")
        reviews = p.get("reviews", [])
        if not isinstance(reviews, list): errors.append(f"{pid}: reviews must be list"); reviews = []
        ids: set[str] = set(); bymod: set[str] = set()
        for i, r in enumerate(reviews):
            if not isinstance(r, dict): errors.append(f"{pid}: review {i} must be object"); continue
            _review_valid(r, mids, errors, i)
            if r.get("id") in ids: errors.append(f"{pid}: duplicate review id {r.get('id')}")
            ids.add(r.get("id"))
            if r.get("module") in bymod: errors.append(f"{pid}: multiple pending reviews for {r.get('module')}")
            bymod.add(r.get("module"))
            if r.get("status") == "in_progress" and (not p.get("active") or p["active"].get("review_id") != r.get("id")): errors.append(f"{pid}: orphan in_progress review")
        active = p.get("active")
        if active is not None:
            if not isinstance(active, dict) or not active.get("task_id") or active.get("kind") not in ("learn", "review", "repair"):
                errors.append(f"{pid}: invalid active")
            else:
                if active.get("module") not in mids: errors.append(f"{pid}: active module missing")
                if not isinstance(active.get("session_id"), str) or not active.get("session_id"): errors.append(f"{pid}: active session missing")
                if not isinstance(active.get("started_on"), str) or not active.get("started_on"): errors.append(f"{pid}: active start missing")
                if active.get("kind") == "review":
                    if active.get("review_id") not in ids: errors.append(f"{pid}: active review missing")
                    elif _review_for(p, active["review_id"])["module"] != active.get("module"): errors.append(f"{pid}: active review/module mismatch")
                elif active.get("review_id") is not None: errors.append(f"{pid}: non-review active has review ID")
        log = p.get("log", {})
        if not isinstance(log, dict) or log.get("status") not in LOG_STATUSES: errors.append(f"{pid}: invalid log status")
        elif log.get("path") is not None:
            try: _safe_rel(log["path"], label="session log path")
            except LearningSystemError as e: errors.append(f"{pid}: {e.message}")
        completed = p.get("completed", {})
        if not isinstance(completed, dict): errors.append(f"{pid}: completed must be object")
        else:
            for tid, rec in completed.items():
                if not isinstance(rec, dict) or rec.get("task_id", tid) != tid: errors.append(f"{pid}: invalid completion {tid}")
    if errors: raise ValidationError("state validation failed", details=errors)
    return []


def _project(root: Path, state: dict, pid: str) -> tuple[dict, dict]:
    if pid not in state.get("projects", {}): raise NotFoundError(f"unknown project: {pid}")
    p = state["projects"][pid]
    manifest, _ = _manifest(root, p)
    return p, manifest


def _module_manifest(manifest: dict, mid: str) -> dict:
    for m in manifest["modules"]:
        if m["id"] == mid: return m
    raise NotFoundError(f"unknown module: {mid}")


def _refresh_reviews(p: dict, today: _dt.date) -> list[dict]:
    return sorted([r for r in p.get("reviews", []) if _date(r["due"]) <= today], key=lambda r: (_date(r["due"]), r["_order"] if "_order" in r else 0, r["id"]))


def _prereq_ready(p: dict, manifest: dict, mid: str, _seen: Optional[set[str]] = None) -> tuple[bool, Optional[str]]:
    """Walk the whole prerequisite frontier, returning the deepest gap."""
    seen = _seen or set()
    if mid in seen: return False, mid
    seen.add(mid)
    m = _module_manifest(manifest, mid)
    for pre in m["prerequisites"]:
        pre_def = _module_manifest(manifest, pre)
        if pre_def.get("blockers"): return False, pre
        if not p["modules"].get(pre, {}).get("prerequisite_ready", False):
            ready, blocker = _prereq_ready(p, manifest, pre, seen)
            return False, blocker or pre
    return True, None


def _selection(p: dict, manifest: dict, today: _dt.date) -> dict:
    """Pure selection; never changes state."""
    order = {m["id"]: i for i, m in enumerate(manifest["modules"])}
    due = sorted([r for r in p.get("reviews", []) if _date(r["due"]) <= today], key=lambda r: (_date(r["due"]), order.get(r["module"], 10**9), r["id"]))
    active = p.get("active")
    if active:
        return {"task_id": active["task_id"], "module": active["module"], "kind": active["kind"], "review_id": active.get("review_id"), "resume_action": active.get("resume_action"), "reason": "resume", "due_count": len(due)}
    for r in due:
        module_def = _module_manifest(manifest, r["module"])
        if module_def.get("blockers"):
            return {"task_id": None, "module": r["module"], "kind": "review", "review_id": r["id"], "reason": "source_blocker", "blockers": list(module_def["blockers"]), "blocked_review": r["id"], "due_count": len(due)}
        ready, blocker = _prereq_ready(p, manifest, r["module"])
        if not ready and blocker and _module_manifest(manifest, blocker).get("blockers"):
            return {"task_id": None, "module": blocker, "kind": "repair", "review_id": None, "reason": "source_blocker", "blockers": list(_module_manifest(manifest, blocker)["blockers"]), "blocked_review": r["id"], "due_count": len(due)}
        if ready:
            return {"task_id": r["id"], "module": r["module"], "kind": "review", "review_id": r["id"], "stage": r.get("stage"), "due": r["due"], "reason": "due_review", "due_count": len(due)}
        # A due prerequisite review gets priority; otherwise a repair task.
        pre_reviews = [x for x in due if x["module"] == blocker]
        if pre_reviews:
            x = pre_reviews[0]
            return {"task_id": x["id"], "module": x["module"], "kind": "review", "review_id": x["id"], "stage": x.get("stage"), "due": x["due"], "reason": "prerequisite_review", "due_count": len(due)}
        return {"task_id": None, "module": blocker, "kind": "repair", "review_id": None, "reason": "prerequisite_repair", "blocked_review": r["id"], "due_count": len(due)}
    blocked_new = None
    for m in manifest["modules"]:
        mid = m["id"]; ms = p["modules"][mid]
        if ms.get("initial_exit_passed_on") or any(r["module"] == mid for r in p.get("reviews", [])): continue
        if m.get("blockers"):
            blocked_new = blocked_new or {"task_id": None, "module": mid, "kind": "learn", "review_id": None, "reason": "source_blocker", "blockers": list(m["blockers"]), "due_count": len(due)}
            continue
        ready, _ = _prereq_ready(p, manifest, mid)
        if ready:
            return {"task_id": None, "module": mid, "kind": "learn", "review_id": None, "reason": "new_module", "due_count": 0}
    if blocked_new: return blocked_new
    future = sorted(p.get("reviews", []), key=lambda r: (_date(r["due"]), r["id"]))
    return {"task_id": None, "module": None, "kind": None, "review_id": None, "reason": "unavailable", "due_count": 0, "future": future[0] if future else None}


def _handoff(root: Path, pid: str, p: dict, manifest: dict, today: _dt.date, *, selection: Optional[dict] = None) -> dict:
    sel = selection or _selection(p, manifest, today)
    module_id = sel.get("module")
    order = {m["id"]: i for i, m in enumerate(manifest["modules"])}
    future_reviews = sorted((r for r in p.get("reviews", []) if _date(r["due"]) > today), key=lambda r: (_date(r["due"]), order.get(r["module"], 10**9), r["id"]))
    future = future_reviews[0] if future_reviews else None
    result = {"verdict": "READY" if module_id else "UNAVAILABLE", "as_of": _date_str(today), "project": pid, "revision": None,
              "module": module_id, "task_id": sel.get("task_id"), "kind": sel.get("kind"), "review_id": sel.get("review_id"),
              "reason": sel.get("reason"), "resume_action": sel.get("resume_action"), "teaching_packet": manifest["teaching_packet"],
              "module_packet": None, "artifact": None, "due_count": len([r for r in p.get("reviews", []) if _date(r["due"]) <= today]),
              "next_future_review": future, "log": p.get("log", {}).get("status", "none")}
    if module_id:
        mm = _module_manifest(manifest, module_id)
        ms = p["modules"][module_id]
        result["module_packet"] = mm["packet"]; result["artifact"] = mm["artifact"]
        result["score_dimensions"] = list(manifest["score_dimensions"])
        result["scores"] = list(ms.get("scores", [0, 0, 0, 0]))
        result["prerequisite_ready"] = bool(ms.get("prerequisite_ready", False))
        result["mastery"] = ms.get("mastery", "unverified")
    if sel.get("reason") == "source_blocker":
        result["verdict"] = "BLOCKED"; result["blocked_review"] = sel.get("blocked_review"); result["blockers"] = sel.get("blockers", [sel.get("module")])
    elif sel.get("reason") == "prerequisite_repair":
        result["verdict"] = "READY"; result["repair_required"] = True; result["blocked_review"] = sel.get("blocked_review")
    log_status = p.get("log", {}).get("status", "none")
    if log_status in ("awaiting_unlink", "unconfirmed", "pending_confirmation") or (log_status == "linked" and not p.get("active")):
        result["verdict"] = "LOG_REQUIRED"; result["log_requirement"] = "confirm or settle the existing transcript before starting another task"
    return result


def check(root: Path, project: str, *, clock: Optional[Callable[[], Any]] = None, today: Optional[str] = None) -> dict:
    """Validate and return a scoped, non-mutating handoff."""
    root = root.resolve(); d = _date(today, label="today") if today else _today(clock)
    with _locked(root) as state:
        validate_state(root, state, project_id=project)
        p, m = _project(root, state, project)
        result = _handoff(root, project, p, m, d)
        result["revision"] = state["revision"]
        result["advisory_due_projects"] = {pid: sum(_date(r["due"]) <= d for r in q.get("reviews", [])) for pid, q in state["projects"].items() if pid != project}
        return result


def agenda(root: Path, *, clock: Optional[Callable[[], Any]] = None, today: Optional[str] = None) -> dict:
    """Return all projects' date-filtered tasks without changing state."""
    root = root.resolve(); d = _date(today, label="today") if today else _today(clock)
    with _locked(root) as state:
        validate_state(root, state)
        projects = []
        for pid, p in state["projects"].items():
            m = _manifest(root, p)[0]; sel = _selection(p, m, d)
            reviews = []
            for r in p.get("reviews", []):
                ready, blocker = _prereq_ready(p, m, r["module"])
                definition = _module_manifest(m, r["module"])
                blocked = bool(r.get("blocked_by")) or bool(definition.get("blockers")) or not ready
                reviews.append({"id": r["id"], "event_id": f"{pid}:{r['id']}", "project": pid, "module": r["module"], "due": r["due"], "status": r["status"], "kind": r["kind"], "stage": r.get("stage"), "blocked": blocked, "blocked_by": r.get("blocked_by", ([blocker] if blocker else [])), "packet": definition["packet"], "artifact": definition["artifact"]})
            if sel.get("task_id"): sel["event_id"] = f"{pid}:{sel['task_id']}"
            projects.append({"project": pid, "selection": sel, "reviews": reviews, "paths": {"manifest": p["manifest"]}})
        return {"as_of": _date_str(d), "revision": state["revision"], "projects": projects}


def validate(root: Path, *, project: Optional[str] = None) -> dict:
    root = root.resolve()
    with _locked(root) as state:
        validate_state(root, state, project_id=project)
        return {"valid": True, "revision": state["revision"], "project": project}


def _new_log_path(root: Path, pid: str, project_path: str, session: str, task_id: str, module: str, kind: str, today: _dt.date, suffix: str = "") -> str:
    if not isinstance(session, str) or not ID_RE.match(session): raise ValidationError("session ID must be filename-safe")
    safe_pid = re.sub(r"[^A-Za-z0-9_.-]", "_", pid)
    safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    safe_kind = re.sub(r"[^A-Za-z0-9_.-]", "_", kind)
    safe_module = re.sub(r"[^A-Za-z0-9_.-]", "_", module)
    rel = f"{project_path}/learning-sessions/{today.strftime('%Y-%m-%d')}-0000-{safe_pid}-{safe_task}{suffix}--{safe_module}--{safe_kind}.md"
    _safe_rel(rel)
    return rel


def prepare(root: Path, project: str, session: str, *, clock: Optional[Callable[[], Any]] = None) -> dict:
    """Allocate a proposed task and exclusively create its empty transcript."""
    d = _today(clock); root = root.resolve()
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, m = _project(root, state, project)
        if p.get("log", {}).get("status") in ("linked", "awaiting_unlink", "unconfirmed"):
            raise ConflictError("existing transcript requires settlement")
        if p.get("last_teaching_session") == session:
            raise ConflictError("one substantive task per Pi session; use a fresh session ID")
        if any((q.get("last_teaching_session") == session or (q.get("active") or {}).get("session_id") == session) for q in state.get("projects", {}).values() if q is not p):
            raise ConflictError("one substantive task per Pi session across projects; use a fresh session ID")
        sel = _selection(p, m, d)
        if sel.get("reason") == "source_blocker":
            return {"verdict": "BLOCKED", "as_of": _date_str(d), "project": project, "selection": sel, "revision": state["revision"]}
        if not sel.get("module"): return {"verdict": "UNAVAILABLE", "as_of": _date_str(d), "project": project, "selection": sel, "revision": state["revision"]}
        if sel["kind"] == "repair": task_id = f"{sel['module']}-repair-{p.get('sequence', 0) + 1}"
        elif sel["kind"] == "learn": task_id = f"{sel['module']}-learn-{p.get('sequence', 0) + 1}"
        else: task_id = sel["task_id"]
        if p.get("log", {}).get("status") == "pending_confirmation":
            old = p["log"].get("path")
            if old and (root / old).exists(): pass  # preserve the stale empty file
        rel = _new_log_path(root, project, m["project_path"], session, task_id, sel["module"], sel["kind"], d); path = _inside(root, rel, label="log path")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8"):
                pass
        except FileExistsError: raise ConflictError(f"log already exists: {rel}")
        p["sequence"] = int(p.get("sequence", 0)) + (0 if sel["kind"] == "review" else 1)
        p["log"] = {"path": rel, "status": "pending_confirmation", "session_id": session, "prepared_on": _date_str(d), "proposed": {**sel, "task_id": task_id}}
        return {"verdict": "LOG_REQUIRED", "as_of": _date_str(d), "project": project, "session": session, "task_id": task_id, "path": rel, "md_log_command": f"/md-log {rel}", "expected_footer": f"🗒 {Path(rel).name}", "revision": state["revision"]}


def confirm_log(root: Path, project: str, session: str, path: str) -> dict:
    """Record the caller's attestation that the exact prepared footer was seen."""
    root = root.resolve()
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, _ = _project(root, state, project); log = p.get("log", {})
        if log.get("status") != "pending_confirmation" or log.get("session_id") != session: raise ConflictError("no matching pending log")
        rel = _safe_rel(path, label="log path")
        if rel != log.get("path"): raise ConflictError("path does not match pending log")
        if not _inside(root, rel).is_file(): raise ValidationError("prepared log does not exist")
        log["status"] = "linked"; log["confirmed"] = True
        return {"verdict": "LOG_LINKED", "project": project, "session": session, "path": rel, "revision": state["revision"]}


def begin(root: Path, project: str, session: str, *, clock: Optional[Callable[[], Any]] = None) -> dict:
    """Activate a confirmed proposed task after refreshing date-based selection."""
    d = _today(clock); root = root.resolve()
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, m = _project(root, state, project); log = p.get("log", {})
        if log.get("status") != "linked" or log.get("session_id") != session: raise ConflictError("matching confirmed log required")
        existing_active = p.get("active")
        # Date rollover alone is harmless. Recompute selection and reject only
        # when the actual module/kind/review changed.
        sel = _selection(p, m, d); proposed = log.get("proposed", {})
        if not sel.get("module") or sel.get("module") != proposed.get("module") or sel.get("kind") != proposed.get("kind") or (sel.get("review_id") != proposed.get("review_id")):
            raise ConflictError("selection changed; prepare a fresh task")
        task_id = proposed["task_id"]
        if existing_active:
            if existing_active.get("task_id") != task_id or existing_active.get("session_id") != session: raise ConflictError("active work belongs to another session")
            # Overnight continuation keeps its original start/checkpoint.
            p["active"] = existing_active
        else:
            p["active"] = {"task_id": task_id, "module": proposed["module"], "kind": proposed["kind"], "review_id": proposed.get("review_id"), "started_on": _date_str(d), "node": proposed.get("node"), "resume_action": proposed.get("resume_action"), "session_id": session}
        if proposed.get("review_id"):
            for r in p["reviews"]:
                if r["id"] == proposed["review_id"]: r["status"] = "in_progress"; r["started_on"] = _date_str(d)
        return {"verdict": "STARTED", "project": project, "module": proposed["module"], "task_id": task_id, "kind": proposed["kind"], "revision": state["revision"]}


def _read_outcome(root: Path, outcome: str) -> dict:
    p = _inside(root, outcome, label="outcome path")
    data = _json_load(p)
    if not isinstance(data, dict): raise ValidationError("outcome must be a JSON object")
    return data


def _artifact_ok(root: Path, rel: str) -> tuple[str, int]:
    p = _inside(root, rel, label="evidence path")
    if not p.is_file(): raise ValidationError(f"artifact missing: {rel}")
    raw = p.read_bytes()
    if not raw.strip(): raise ValidationError(f"artifact is empty: {rel}")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _review_for(p: dict, rid: str) -> dict:
    for r in p.get("reviews", []):
        if r["id"] == rid: return r
    raise ValidationError(f"review not found: {rid}")


def _next_review(r: dict, completed: _dt.date) -> Optional[dict]:
    if r.get("kind") != "spaced": stage = 1
    else: stage = int(r["stage"]) + 1
    if stage > 6: return None
    return {"id": f"{r['module']}-R{stage}", "module": r["module"], "kind": "spaced", "stage": stage, "interval_days": INTERVALS[stage - 1], "due": _date_str(completed + _dt.timedelta(days=INTERVALS[stage - 1])), "status": "scheduled", "started_on": None, "blocked_by": []}


def _apply_completion(p: dict, manifest: dict, outcome: dict, active: dict, completed_on: _dt.date, evidence_path: str, evidence_hash: str) -> dict:
    task_id = active["task_id"]; kind = active["kind"]; module = active["module"]
    existing = p.setdefault("completed", {}).get(task_id)
    immutable = {"task_id": task_id, "module": module, "kind": kind, "session_id": active.get("session_id"), "completed_on": _date_str(completed_on), "outcome": "passed", "scores": outcome["scores"], "exit_passed": True, "evidence_path": evidence_path, "artifact_hash": evidence_hash, "summary": outcome.get("summary", ""), "resulting_review": None}
    if existing:
        if any(existing.get(k) != immutable.get(k) for k in ("module", "kind", "completed_on", "scores", "evidence_path", "artifact_hash")):
            raise ConflictError("conflicting reuse of completed task ID")
        return existing
    rid = active.get("review_id")
    resulting = None
    old = None
    if rid:
        old = _review_for(p, rid); resulting = _next_review(old, completed_on)
        p["reviews"] = [r for r in p["reviews"] if r["id"] != rid]
    ms = p["modules"][module]; ms["scores"] = outcome["scores"]; ms["last_review"] = _date_str(completed_on)
    # A successful cold/spaced retrieval is also evidence for prerequisite
    # readiness; delayed mastery remains a separate, intentionally unverified
    # label until later reviews.
    ms["prerequisite_ready"] = True
    if not ms.get("initial_exit_passed_on"): ms["initial_exit_passed_on"] = _date_str(completed_on)
    if old and old.get("kind") == "spaced" and old.get("stage") == 6:
        ms["mastery"] = "mastered"
    if kind in ("learn", "repair") and not any(r["module"] == module for r in p["reviews"]):
        resulting = {"id": f"{module}-R1", "module": module, "kind": "spaced", "stage": 1, "interval_days": 1, "due": _date_str(completed_on + _dt.timedelta(days=1)), "status": "scheduled", "started_on": None, "blocked_by": []}
    if resulting and not any(r["id"] == resulting["id"] for r in p["reviews"]): p["reviews"].append(resulting)
    immutable["resulting_review"] = resulting
    p["completed"][task_id] = immutable
    p["last_session"] = _date_str(completed_on); p["last_completed_module"] = module
    p["last_teaching_session"] = active.get("session_id")
    p["active"] = None; p["log"]["status"] = "awaiting_unlink"
    p["log"]["completed_task_id"] = task_id
    return immutable


def finish(root: Path, project: str, session: str, outcome_path: str, *, clock: Optional[Callable[[], Any]] = None) -> dict:
    """Atomically commit a teacher outcome and schedule its next appointment."""
    d = _today(clock); root = root.resolve(); outcome = _read_outcome(root, outcome_path)
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, m = _project(root, state, project); active = p.get("active")
        # A caller may retry after receiving a lost response.  The immutable
        # completion record is authoritative and makes that retry idempotent.
        requested_id = outcome.get("task_id")
        if not active and requested_id in p.get("completed", {}):
            prior = p["completed"][requested_id]
            if (outcome.get("outcome") != "passed" or prior.get("outcome") != "passed" or prior.get("scores") != outcome.get("scores") or
                    prior.get("evidence_path") != outcome.get("evidence_path") or prior.get("exit_passed", True) != outcome.get("exit_passed") or
                    (prior.get("session_id") is not None and prior.get("session_id") != session) or prior.get("artifact_hash") != _artifact_ok(root, outcome.get("evidence_path"))[0]):
                raise ConflictError("conflicting reuse of completed task ID")
            return {"verdict": "COMMITTED", "project": project, "task_id": requested_id,
                    "event_id": f"{requested_id}-pass", "revision": state["revision"]}
        if not active or active.get("session_id") != session: raise ConflictError("active work belongs to another session")
        if outcome.get("task_id") != active.get("task_id"): raise ConflictError("outcome task_id does not match active task")
        status = outcome.get("outcome")
        if status not in ("passed", "paused", "failed"): raise ValidationError("outcome must be passed, paused, or failed")
        scores = outcome.get("scores")
        if not isinstance(scores, list) or len(scores) != 4 or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 3 for x in scores): raise ValidationError("scores must be four integers from 0 to 3")
        if not isinstance(outcome.get("evidence_path"), str): raise ValidationError("evidence_path required")
        expected_artifact = _module_manifest(m, active["module"])["artifact"]
        if outcome["evidence_path"] != expected_artifact: raise ValidationError("evidence_path must equal the module manifest artifact")
        digest, _ = _artifact_ok(root, outcome["evidence_path"])
        if status == "passed" and (not outcome.get("exit_passed") or any(x < 2 for x in scores)):
            raise ValidationError("passed outcome requires exit_passed=true and all scores >= 2")
        if status != "passed" and outcome.get("exit_passed") is True: raise ValidationError("only passed outcomes may set exit_passed=true")
        if status != "passed":
            active["resume_action"] = outcome.get("resume_action") or "repeat an unaided exit check"
            p.setdefault("attempts", []).append({"task_id": active["task_id"], "module": active["module"], "kind": active["kind"], "session_id": session, "outcome": status, "completed_on": _date_str(d), "scores": scores, "evidence_path": outcome["evidence_path"], "artifact_hash": digest, "summary": outcome.get("summary", "")})
            p["modules"][active["module"]]["scores"] = scores
            p["last_session"] = _date_str(d)
            active["node"] = outcome.get("node")
            if active.get("kind") == "review" and any(x < 2 for x in scores): p["modules"][active["module"]]["prerequisite_ready"] = False
            return {"verdict": "PAUSED" if status == "paused" else "REPAIR_REQUIRED", "task_id": active["task_id"], "revision": state["revision"], "resume_action": active["resume_action"]}
        event_id = f"{active['task_id']}-pass"
        existing = p.setdefault("completion_journal", [])
        prior = next((e for e in existing if e.get("event_id") == event_id), None)
        if prior and prior.get("state") == "applied":
            # A replay must be idempotent; do not mutate queue a second time.
            return {"verdict": "COMMITTED", "task_id": active["task_id"], "event_id": event_id, "revision": state["revision"]}
        rec = _apply_completion(p, m, outcome, active, d, outcome["evidence_path"], digest)
        if prior is None: existing.append({"event_id": event_id, "task_id": active["task_id"], "module": active["module"], "kind": active["kind"], "completed_on": _date_str(d), "consumed_review_id": active.get("review_id"), "next_review": rec.get("resulting_review"), "state": "applied"})
        else: prior["state"] = "applied"
        sel = _selection(p, m, d)
        return {"verdict": "COMMITTED", "project": project, "task_id": active["task_id"], "event_id": event_id, "completed_on": _date_str(d), "next_review": rec.get("resulting_review"), "next_selection": sel, "revision": state["revision"] + 1}


def settle_log(root: Path, project: str, session: str, *, confirmed_unlinked: bool = False) -> dict:
    """Clear a transcript pointer only after caller confirms it is unlinked."""
    if not confirmed_unlinked: raise ValidationError("--confirmed-unlinked is required")
    root = root.resolve()
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, _ = _project(root, state, project); log = p.get("log", {})
        # Legacy/unconfirmed pointers may have no recorded owner. The caller
        # explicitly attests unlinking in that case; never infer ownership.
        if log.get("session_id") not in (session, None): raise ConflictError("session does not own transcript")
        if log.get("status") not in ("awaiting_unlink", "pending_confirmation", "linked", "unconfirmed"): raise ConflictError("no transcript to settle")
        old = log.get("path"); p["last_session_log"] = old; p["log"] = {"path": None, "status": "none", "session_id": None}
        return {"verdict": "SETTLED", "project": project, "last_session_log": old, "revision": state["revision"]}


def resume(root: Path, project: str, session: str, *, confirmed_handoff: bool = False, clock: Optional[Callable[[], Any]] = None) -> dict:
    """Transfer unfinished active work to a fresh session and fresh transcript."""
    if not confirmed_handoff: raise ValidationError("--confirmed-handoff is required")
    d = _today(clock); root = root.resolve()
    with _locked(root, write=True) as state:
        validate_state(root, state, project_id=project); p, m = _project(root, state, project); active = p.get("active")
        if not active: raise ConflictError("no unfinished active work")
        old_session = active.get("session_id")
        # Explicit handoff confirmation permits a branch/fork even when the
        # parent teacher session ID is intentionally retained.
        task_id = active["task_id"]
        attempt = 1
        while True:
            suffix = "-resume" if attempt == 1 else f"-resume{attempt}"
            rel = _new_log_path(root, project, m["project_path"], session, task_id, active["module"], active["kind"], d, suffix=suffix)
            path = _inside(root, rel); path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8"):
                    pass
                break
            except FileExistsError:
                attempt += 1
        old_path = p.get("log", {}).get("path")
        if old_path: p["last_session_log"] = old_path
        p["log"] = {"path": rel, "status": "pending_confirmation", "session_id": session, "prepared_on": _date_str(d), "proposed": {**active, "task_id": task_id}}
        active["session_id"] = session
        return {"verdict": "LOG_REQUIRED", "project": project, "task_id": task_id, "path": rel, "md_log_command": f"/md-log {rel}", "expected_footer": f"🗒 {Path(rel).name}", "resume_action": active.get("resume_action"), "revision": state["revision"]}


def _manifest_state(manifest: dict) -> dict:
    mods = {}
    for m in manifest["modules"]:
        mods[m["id"]] = _default_module()
    return {"manifest": manifest["project_path"].rstrip("/") + "/learning-system/curriculum.json", "modules": mods, "reviews": [], "active": None, "log": {"path": None, "status": "none", "session_id": None}, "last_session_log": None, "last_session": None, "completed": {}, "completion_journal": [], "sequence": 0}


def register(root: Path, manifest_path: str, initial_state: Optional[str] = None) -> dict:
    """Atomically register a validated generated/migrated project, never replace it."""
    root = root.resolve(); mp = Path(manifest_path)
    if not mp.is_absolute(): mp = _inside(root, manifest_path, label="manifest")
    else: mp = mp.resolve()
    manifest = _json_load(mp); validate_manifest(root, manifest)
    if initial_state is None and any(m.get("initial_mode") == "cold" for m in manifest["modules"]):
        raise ValidationError("initial-state is required for manifests containing cold modules")
    # State stores vault-relative manifest paths.
    try: rel_manifest = mp.relative_to(root).as_posix()
    except ValueError: raise ValidationError("manifest must be inside vault")
    with _locked(root, write=True, initialize=True) as state:
        pid = manifest["id"]
        if pid in state["projects"]: raise ConflictError(f"project already registered: {pid}")
        p = _manifest_state(manifest); p["manifest"] = rel_manifest
        if initial_state:
            ip = Path(initial_state)
            if not ip.is_absolute(): ip = _inside(root, initial_state, label="initial state")
            incoming = _json_load(ip)
            if isinstance(incoming, dict) and "projects" in incoming: incoming = incoming.get("projects", {}).get(pid)
            if isinstance(incoming, dict):
                # Preserve migration facts but retain required ownership scaffolding.
                for key in ("modules", "reviews", "active", "log", "last_session_log", "last_session", "last_teaching_session", "completed", "completion_journal", "attempts", "sequence"):
                    if key in incoming: p[key] = incoming[key]
        state["projects"][pid] = p
        validate_state(root, state)
        return {"verdict": "REGISTERED", "project": pid, "manifest": rel_manifest, "revision": state["revision"]}


def _parser() -> argparse.ArgumentParser:
    pa = argparse.ArgumentParser(description=__doc__)
    pa.add_argument("--root", help="vault root (defaults to ancestor discovery)")
    pa.add_argument("--today", help="read-only simulated date for check/agenda/validate")
    sub = pa.add_subparsers(dest="command", required=True)
    x = sub.add_parser("check"); x.add_argument("--project", required=True)
    sub.add_parser("agenda")
    x = sub.add_parser("validate"); x.add_argument("--project")
    x = sub.add_parser("prepare"); x.add_argument("--project", required=True); x.add_argument("--session", required=True)
    x = sub.add_parser("confirm-log"); x.add_argument("--project", required=True); x.add_argument("--session", required=True); x.add_argument("--path", required=True)
    x = sub.add_parser("begin"); x.add_argument("--project", required=True); x.add_argument("--session", required=True)
    x = sub.add_parser("finish"); x.add_argument("--project", required=True); x.add_argument("--session", required=True); x.add_argument("--outcome", required=True)
    x = sub.add_parser("settle-log"); x.add_argument("--project", required=True); x.add_argument("--session", required=True); x.add_argument("--confirmed-unlinked", action="store_true")
    x = sub.add_parser("resume"); x.add_argument("--project", required=True); x.add_argument("--session", required=True); x.add_argument("--confirmed-handoff", action="store_true")
    x = sub.add_parser("register"); x.add_argument("--manifest", required=True); x.add_argument("--initial-state")
    return pa


def main(argv: Optional[list[str]] = None) -> int:
    pa = _parser(); a = pa.parse_args(argv)
    try:
        root = Path(a.root).resolve() if a.root else discover_root()
        mutation = a.command in {"prepare", "confirm-log", "begin", "finish", "settle-log", "resume", "register"}
        if a.today and mutation: raise ValidationError("--today is read-only and cannot be used with mutation commands")
        if a.command == "check": out = check(root, a.project, today=a.today)
        elif a.command == "agenda": out = agenda(root, today=a.today)
        elif a.command == "validate": out = validate(root, project=a.project)
        elif a.command == "prepare": out = prepare(root, a.project, a.session)
        elif a.command == "confirm-log": out = confirm_log(root, a.project, a.session, a.path)
        elif a.command == "begin": out = begin(root, a.project, a.session)
        elif a.command == "finish": out = finish(root, a.project, a.session, a.outcome)
        elif a.command == "settle-log": out = settle_log(root, a.project, a.session, confirmed_unlinked=a.confirmed_unlinked)
        elif a.command == "resume": out = resume(root, a.project, a.session, confirmed_handoff=a.confirmed_handoff)
        else: out = register(root, a.manifest, a.initial_state)
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True)); return 0
    except LearningSystemError as e:
        print(json.dumps({"error": e.code, "message": e.message, **({"details": e.details} if e.details is not None else {})}, ensure_ascii=False), file=sys.stderr); return 2
    except Exception as e:
        print(json.dumps({"error": "internal_error", "message": str(e)}, ensure_ascii=False), file=sys.stderr); return 1


if __name__ == "__main__": sys.exit(main())
