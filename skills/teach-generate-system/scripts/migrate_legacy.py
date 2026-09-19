#!/usr/bin/env python3
"""Dry-run-first migration of legacy Learning.md controllers to v2.

Only explicitly named controllers are inspected. Raw session transcripts are
copied byte-for-byte and never parsed or rewritten. Applying requires --apply;
without it this command only validates and reports a plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

class MigrationError(ValueError):
    pass


def _frontmatter(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw.startswith(b"---"):
        raise MigrationError(f"{path} has no YAML frontmatter")
    end = raw.find(b"\n---", 3)
    if end < 0:
        raise MigrationError(f"{path} frontmatter is unterminated")
    text = raw[4:end + 1].decode("utf-8")
    # Ruby's stdlib YAML is explicitly permitted for migration and handles the
    # nested review/completion structures in historical controllers.
    script = "require 'yaml'; require 'json'; require 'date'; puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true, permitted_classes: [Date, Time, Symbol]) || {})"
    result = subprocess.run(["ruby", "-e", script], input=text, text=True, capture_output=True)
    if result.returncode:
        raise MigrationError(f"Ruby YAML parser failed for {path}: {result.stderr.strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"invalid YAML result for {path}: {exc}")
    return value if isinstance(value, dict) else {}


def _split_row(line: str) -> list[str]:
    return [x.strip() for x in line.strip().strip("|").split("|")]


def _catalog(controller: str) -> list[dict[str, Any]]:
    lines = controller.splitlines()
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not (line.startswith("|") and "ID" in line and "|" in line):
            continue
        header = _split_row(line)
        lower = [x.lower() for x in header]
        if "id" not in lower:
            continue
        # Require a curriculum-like table, not an unrelated markdown table.
        if not any("module" in x or "source packet" in x for x in lower):
            continue
        sep = i + 1
        if sep >= len(lines) or not lines[sep].startswith("|"):
            continue
        positions = {name: lower.index(name) for name in ("id",) if name in lower}
        title_idx = next((j for j, x in enumerate(lower) if x == "module"), None)
        source_idx = next((j for j, x in enumerate(lower) if "source packet" in x), None)
        anchor_idx = next((j for j, x in enumerate(lower) if "grounding" in x or "exit target" in x or "exit focus" in x), source_idx)
        exit_idx = next((j for j, x in enumerate(lower) if "exit target" in x or "exit focus" in x), None)
        prereq_idx = next((j for j, x in enumerate(lower) if "prerequisite" in x), None)
        score_idx = next((j for j, x in enumerate(lower) if "score" in x), None)
        status_idx = next((j for j, x in enumerate(lower) if x == "state" or "status" in x), None)
        if title_idx is None:
            continue
        j = sep + 1
        while j < len(lines) and lines[j].startswith("|"):
            row = _split_row(lines[j]); j += 1
            if not row or len(row) <= positions["id"]:
                continue
            mid = row[positions["id"]].strip(" `")
            if not SAFE_ID.fullmatch(mid) or mid.lower() in {"id", "---"}:
                continue
            def cell(idx: int | None) -> str:
                return row[idx] if idx is not None and idx < len(row) else ""
            title = cell(title_idx)
            if not title:
                continue
            # Remove inline code and status backticks while retaining meaning.
            prereq = [x.strip(" `") for x in re.split(r",|<br\s*/?>", cell(prereq_idx)) if x.strip(" `-—") and x.strip(" `-—").lower() not in {"none", "—", "-"}]
            score_match = re.search(r"([0-3])\s*/\s*([0-3])\s*/\s*([0-3])\s*/\s*([0-3])", cell(score_idx))
            scores = [int(x) for x in score_match.groups()] if score_match else [0, 0, 0, 0]
            status = cell(status_idx).strip(" `") or "LOCKED"
            source = cell(source_idx).strip() if source_idx is not None else ""
            anchor = source or (cell(anchor_idx).strip() if anchor_idx is not None else "")
            exit_target = cell(exit_idx).strip() if exit_idx is not None else ""
            out.append({"id": mid, "title": title.strip(" `"), "prerequisites": prereq,
                        "legacy_prerequisites": cell(prereq_idx).strip() if prereq_idx is not None else "",
                        "scores": scores, "status": status, "source_packet": source,
                        "source_anchor": anchor, "exit_target": exit_target})
    seen: set[str] = set()
    for row in out:
        if row["id"] in seen:
            raise MigrationError(f"duplicate module ID in legacy catalog: {row['id']}")
        seen.add(row["id"])
    return out


def _source_path(project_dir: Path, packet: str) -> Path | None:
    """Resolve only an unambiguous course TXT locator.

    Composite repository/application locators are retained as textual scope;
    treating `app/...`, `hooks/...`, etc. as files in the legacy course would
    create bogus blockers.
    """
    if not packet:
        return None
    quoted = re.search(r"`([^`]+)`", packet)
    candidate = (quoted.group(1) if quoted else packet.strip().split(":", 1)[0]).strip(" `")
    if not candidate or candidate.startswith("http"):
        return None
    if not re.search(r"(?:^|/)(?:ch\d+)\.txt(?:$|:)", candidate):
        return None
    p = Path(candidate.split(":", 1)[0])
    if p.is_absolute():
        return p
    return project_dir / "content" / p


def _artifact_data(path: Path) -> dict[str, Any]:
    try:
        return _frontmatter(path)
    except MigrationError:
        return {}


def _reviews(meta: dict[str, Any], modules: set[str]) -> list[dict[str, Any]]:
    if "review_calendar" not in meta and meta.get("due_reviews"):
        raise MigrationError("legacy due_reviews has no structured review_calendar; resolve its stages explicitly before migration")
    values = meta.get("review_calendar", [])
    if not isinstance(values, list):
        raise MigrationError("review_calendar must be a list; refusing to discard review history")
    result = []
    for r in values:
        if not isinstance(r, dict) or not r.get("id") or r.get("module") not in modules:
            raise MigrationError(f"invalid or unresolvable pending review: {r!r}")
        item = {"id": str(r["id"]), "module": str(r["module"]), "kind": str(r.get("kind", "legacy")),
                "stage": r.get("stage"), "due": r.get("due"), "status": str(r.get("status", "scheduled")),
                "started_on": r.get("started_on")}
        result.append(item)
    return result


def _completed(meta: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Migrate only operationally complete events with explicit evidence.

    Historical journal rows are always retained in the archive. Missing scores
    or outcome are not silently converted into a zero-score pass.
    """
    journal = meta.get("completion_journal", [])
    if not isinstance(journal, list):
        return {}, []
    result: dict[str, Any] = {}
    omitted: list[dict[str, Any]] = []
    for event in journal:
        if not isinstance(event, dict) or not event.get("task_id"):
            continue
        scores = event.get("scores")
        outcome = event.get("outcome")
        if outcome not in {"passed", "paused", "failed"}:
            omitted.append(event); continue
        if not isinstance(scores, list) or len(scores) != 4 or any(not isinstance(x, int) or not 0 <= x <= 3 for x in scores):
            omitted.append(event); continue
        task = str(event["task_id"])
        result[task] = {"completed_on": event.get("completed_on"), "outcome": outcome,
                        "scores": scores, "artifact_path": event.get("artifact_path"), "artifact_hash": event.get("artifact_hash"),
                        "resulting_review": event.get("next_review")}
    return result, omitted


def inspect_controller(root: Path, source: Path, project_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    source = source.resolve()
    if not source.is_file() or source.name != "Learning.md":
        raise MigrationError(f"explicit controller is not a Learning.md file: {source}")
    project_dir = source.parent
    meta = _frontmatter(source)
    text = source.read_text(encoding="utf-8")
    pid = project_id or project_dir.name
    if not SAFE_ID.fullmatch(pid):
        raise MigrationError(f"unsafe project ID: {pid}")
    catalog = _catalog(text)
    if not catalog:
        raise MigrationError(f"no curriculum module table found in {source}")
    ids = {m["id"] for m in catalog}
    blockers = []
    module_blockers: dict[str, list[str]] = {m["id"]: [] for m in catalog}
    executable_prereqs: dict[str, list[str]] = {}
    for m in catalog:
        if m["status"] in {"BLOCKED_SOURCE", "BLOCKED_EVIDENCE", "REVERIFY", "OCR_BLOCKED"}:
            message = f"{m['id']}: preserved legacy {m['status']}: {m.get('exit_target') or m.get('source_anchor') or m['title']}"
            blockers.append(message); module_blockers[m["id"]].append(message)
        executable = [x for x in m["prerequisites"] if x in ids]
        unresolved = [x for x in m["prerequisites"] if x not in ids]
        m["legacy_prerequisites"] = m.get("legacy_prerequisites") or ", ".join(m["prerequisites"])
        m["prerequisites"] = executable
        executable_prereqs[m["id"]] = executable
        if unresolved:
            message = f"{m['id']}: unresolved prerequisite expression preserved: {m['legacy_prerequisites']}"
            blockers.append(message); module_blockers[m["id"]].append(message)
        sp = _source_path(project_dir, m["source_packet"])
        if m["source_packet"] and sp and not sp.exists():
            # A source may be an external URL or an imprecise historical anchor;
            # report rather than inventing a dependency or reading alternatives.
            message = f"{m['id']}: source packet path not accessible: {sp}"
            blockers.append(message); module_blockers[m["id"]].append(message)
    artifacts = {}
    artifact_dir = project_dir / "learning-artifacts"
    if artifact_dir.is_dir():
        for child in artifact_dir.iterdir():  # deliberately non-recursive
            if child.is_file() and child.suffix == ".md":
                artifacts[child.name] = _artifact_data(child)
    dimensions = ["E", "T", "R", "D"] if project_dir.name == "interview" else ["E", "P", "R", "T"]
    # Detect cycles only in the executable ID graph; unresolved expressions are
    # blockers, not manifest references that would make engine validation fail.
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            raise MigrationError(f"cycle in legacy prerequisite graph at {node}")
        if node in visited: return
        visiting.add(node)
        for dep in executable_prereqs[node]: visit(dep)
        visiting.remove(node); visited.add(node)
    for node in executable_prereqs: visit(node)
    modules = {}
    reviews = _reviews(meta, ids)
    review_by_module = {r["module"]: r for r in reviews}
    prior = set()
    # Prior exposure is inferred only from explicit review/status evidence;
    # prose such as `prior_coverage: Chapters 1–2` is not enough to mark every
    # module as exposed.
    for m in catalog:
        data = artifacts.get(f"{m['id']}.md", {})
        scores = data.get("scores", m["scores"])
        if isinstance(scores, dict):
            aliases = {"E": ("E", "e", "Explain", "explain"), "P": ("P", "p", "Perform", "perform"),
                       "R": ("R", "r", "Reason", "reason"), "T": ("T", "t", "Transfer", "transfer", "Trace", "trace"),
                       "D": ("D", "d", "Defend", "defend")}
            scores = [next((scores[k] for k in aliases[d] if k in scores), 0) for d in dimensions]
        if not isinstance(scores, list) or len(scores) != 4 or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 3 for x in scores):
            raise MigrationError(f"{m['id']}: invalid legacy scores; refusing to reset or invent progress")
        status = str(data.get("status", m["status"]))
        exposed = status in {"REVIEW_DUE", "LEARNED", "INTERVIEW_READY", "MASTERED", "REVERIFY"} or m["id"] in review_by_module
        if exposed:
            prior.add(m["id"])
        passed_on = data.get("initial_exit_passed_on")
        explicit_ready = data.get("prerequisite_ready")
        if not isinstance(explicit_ready, bool):
            explicit_ready = False
        mastery = data.get("mastery")
        if not isinstance(mastery, str) or not mastery:
            # Preserve either curriculum's earned label only with its recorded
            # exit evidence. Never turn INTERVIEW_READY into unverified merely
            # because that curriculum uses a different mastery vocabulary.
            mastery = status if status in {"MASTERED", "INTERVIEW_READY"} and passed_on and all(x >= 2 for x in scores) else "unverified"
        modules[m["id"]] = {"scores": scores, "prerequisite_ready": explicit_ready,
                             "initial_exit_passed_on": passed_on, "mastery": mastery, "last_review": data.get("last_review")}
    active_module = meta.get("active_module")
    active = None
    if active_module and meta.get("active_task_id"):
        active = {"task_id": meta["active_task_id"], "module": active_module,
                  "kind": meta.get("active_kind", "learn"), "review_id": meta.get("active_review_id"),
                  "started_on": meta.get("date_checked_at"), "node": meta.get("active_node"),
                  "resume_action": meta.get("resume_action"), "session_id": meta.get("active_session_id")}
    log_path = meta.get("active_session_log")
    log_status = str(meta.get("session_log_state", "none"))
    if log_status not in {"none", "pending_confirmation", "linked", "awaiting_unlink", "unconfirmed"}: log_status = "none"
    project_rel = str(project_dir.relative_to(root.resolve()))
    completed, omitted_events = _completed(meta)
    state_project = {"manifest": f"{project_rel}/learning-system/curriculum.json", "modules": modules,
                     "reviews": reviews, "active": active, "log": {"path": log_path, "status": log_status, "session_id": meta.get("active_session_id")},
                     "last_session_log": meta.get("last_session_log"), "last_session": meta.get("last_session"),
                     "completed": completed, "sequence": 0}
    plan = {"project_id": pid, "source": str(source), "project_path": project_rel, "topic": str(meta.get("title", pid)),
            "score_dimensions": dimensions, "modules": catalog, "prior_exposure": sorted(prior), "reviews": reviews,
            "state_project": state_project, "blockers": blockers, "module_blockers": module_blockers, "artifact_files": sorted(artifacts),
            "legacy_meta": meta, "legacy_text": text, "omitted_events": omitted_events,
            "raw_logs": [str(x.relative_to(root.resolve())) for x in (project_dir / "learning-sessions").iterdir() if x.is_file()] if (project_dir / "learning-sessions").is_dir() else []}
    return meta, plan


def _headings(text: str) -> list[tuple[int, int, str]]:
    """Real Markdown headings only, not examples inside fenced templates."""
    found = []
    fence = None
    for i, line in enumerate(text.splitlines()):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if fence is None and match:
            found.append((i, len(match.group(1)), match.group(2)))
    return found


def _section(text: str, heading: str) -> str:
    """Extract one exact Markdown section, including nested subsections."""
    lines = text.splitlines()
    headings = _headings(text)
    for index, (i, level, title) in enumerate(headings):
        if title == heading:
            end = next((j for j, depth, _ in headings[index + 1:] if depth <= level), len(lines))
            return "\n".join(lines[i:end])
    return ""


def _packet_context(plan: dict[str, Any], module: dict[str, Any]) -> str:
    text = plan.get("legacy_text", "")
    if module["id"].startswith("F-"):
        return _section(text, "Foundation source anchors")
    sections = {
        "SD-": "5.2 Social Dojo current truth",
        "RZ-": "5.3 RZ current truth",
        "CL-": "5.4 Climbing Efficiency Analyzer current truth",
        "BZ-": "5.5 Hypixel Bazaar Market Analysis current truth",
        "H-": "5.6 Heart-disease Naive Bayes project current truth",
    }
    for prefix, heading in sections.items():
        if module["id"].startswith(prefix):
            body = _section(text, heading)
            return ("## Pinned project evidence context\n\nThis is the legacy audit snapshot, not proof of unchanged current behavior. Verify the source revision before teaching.\n\n" + body) if body else ""
    return ""


def _packet(plan: dict[str, Any], module: dict[str, Any]) -> str:
    source = module.get("source_packet") or "(legacy controller did not specify a packet)"
    anchor = module.get("source_anchor") or source
    scores = "/".join(str(x) for x in module.get("scores", [0, 0, 0, 0]))
    executable = ", ".join(module.get("prerequisites", [])) or "none"
    exact_prereqs = module.get("legacy_prerequisites") or executable
    exit_target = module.get("exit_target") or "(no separate exit column; preserved grounding anchor is below)"
    return f"""---\nmodule: {json.dumps(module['id'])}\nlegacy_migration: true\n---\n# {module['title']}\n\n## Preserved legacy catalog section\n- State: `{module.get('status', 'unknown')}`\n- Score at migration: `{scores}` (untested dimensions remain untested)\n- Prerequisites exactly as recorded: `{exact_prereqs}`\n- Executable prerequisite IDs after blocker filtering: `{executable}`\n\n## Exact source/grounding anchor\n{anchor}\n\n## Exact exit target\n{exit_target}\n\n{_packet_context(plan, module)}\n\n## Migration boundary\nThis packet contains the bounded legacy anchor and exit target. Read the explicitly named source packet only; the archived controller is historical evidence, not a required scheduler/teacher input. Unresolved prerequisite expressions remain blockers and are not executable dependencies.\n"""


def _bounded_sections(text: str) -> str:
    """Keep mission/authority/rubric/notation policy, not the course catalog."""
    lines = text.splitlines(); chunks = []
    wanted = re.compile(r"mission|finish line|source[- ]of[- ]truth|notation|authority|four independent scores|four scores|rubric|method|evidence and honesty standard|exit demonstration for every technical module|module exit demonstration", re.I)
    headings = _headings(text)
    for index, (i, level, title) in enumerate(headings):
        if not wanted.search(title):
            continue
        end = next((j for j, depth, _ in headings[index + 1:] if depth <= level), len(lines))
        chunk = lines[i:min(end, i + 100)]
        chunks.append("\n".join(chunk))
    return "\n\n".join(chunks[:6]) or "Legacy controller policy sections were not machine-extractable; consult the archived bytes only for historical audit."


def build_migration(plan: dict[str, Any], root: Path) -> tuple[dict[Path, bytes], dict[str, Any]]:
    project = Path(plan["project_path"])
    files: dict[Path, bytes] = {}
    # New controller is intentionally tiny; old bytes are archived separately.
    files[project / "Learning.md"] = (f"# {plan['topic']}\n\nThis is project `{plan['project_id']}`. Legacy curriculum migrated to v2. Use `.pi/skills/teach/references/curriculum-session.md` and the `learning-scheduler` subagent. Read `learning-system/Teaching.md` and the selected module packet; `.learning/state.json` is authoritative.\n").encode()
    policy = _bounded_sections(plan.get("legacy_text", ""))
    meta = plan.get("legacy_meta", {})
    source_keys = ("source_authority", "social_dojo_root", "social_dojo_commit", "rz_root", "rz_commit", "climbing_repo", "climbing_commit", "bazaar_repo", "bazaar_commit", "heart_project_source")
    locations = "\n".join(f"- `{key}`: `{meta[key]}`" for key in source_keys if key in meta)
    if locations:
        policy += "\n\n## Approved source locations and pinned revisions\n\n" + locations + "\n\nCheck the selected source's current revision/hash before teaching. Never read `.env*`, credentials, or customer records. Documentation and pinned audit snapshots are not proof of current behavior."
    source_hashes = dict(re.findall(r"^ch(\d+)\s+([0-9a-fA-F]{64})\s*$", plan.get("legacy_text", ""), re.M))
    if source_hashes:
        source_manifest = {"source_directory": str(project / "content"), "sha256": {f"ch{k}.txt": v for k, v in source_hashes.items()}, "origin": str(project / "learning-system/archive/legacy/Learning.md")}
        files[project / "learning-system/source-manifest.json"] = (json.dumps(source_manifest, indent=2) + "\n").encode()
        policy += "\n\n## Source-manifest reference\n\nReferences to §8/source manifest in the preserved policy mean `learning-system/source-manifest.json`. Hash only the selected module's TXT input before teaching and compare to that manifest. A mismatch requires re-verification, not silently accepting a different source."
    files[project / "learning-system" / "Teaching.md"] = (f"# {plan['topic']}\n\nThis is project `{plan['project_id']}` migrated from a legacy controller. Score dimensions: {', '.join(plan['score_dimensions'])}.\n\n## Preserved course authority, mission, notation, and rubric policy\n{policy}\n\nDo not infer mastery from legacy exposure. Follow the helper's exact selected task and review stage: unverified prior exposure begins cold; completed modules must not restart as initial cold reviews. Each module packet contains its bounded source anchor and exact exit target; do not load the archived controller during ordinary teaching.\n\nRead `.pi/skills/teach/references/curriculum-session.md`; use the `learning-scheduler` subagent.\n").encode()
    manifest_modules = []
    for m in plan["modules"]:
        packet = project / "learning-system" / "modules" / f"{m['id']}.md"
        manifest_modules.append({"id": m["id"], "title": m["title"], "prerequisites": m["prerequisites"],
                                 "packet": str(packet), "artifact": str(project / "learning-artifacts" / f"{m['id']}.md"),
                                 "blockers": plan.get("module_blockers", {}).get(m["id"], []),
                                 "initial_mode": "cold" if m["id"] in plan["prior_exposure"] else "learn"})
        files[packet] = _packet(plan, m).encode()
    manifest = {"schema_version": 2, "id": plan["project_id"], "title": plan["topic"], "project_path": str(project),
                "teaching_packet": str(project / "learning-system" / "Teaching.md"), "score_dimensions": plan["score_dimensions"], "modules": manifest_modules}
    files[project / "learning-system" / "curriculum.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files[project / "learning-artifacts" / ".gitkeep"] = b""
    files[project / "learning-sessions" / ".gitkeep"] = b""
    archive = project / "learning-system" / "archive" / "legacy"
    source = Path(plan["source"])
    files[archive / "Learning.md"] = source.read_bytes()
    # Preserve legacy artifacts byte-for-byte in archive; do not alter raw logs.
    for name in plan["artifact_files"]:
        old = root / project / "learning-artifacts" / name
        if old.is_file(): files[archive / "artifacts" / name] = old.read_bytes()
    # Keep a copy of existing scored state/journal as machine-readable input.
    files[archive / "completion-journal.json"] = (json.dumps(plan.get("legacy_meta", {}).get("completion_journal", []), indent=2, ensure_ascii=False) + "\n").encode()
    files[archive / "migration-report.json"] = (json.dumps({"blockers": plan["blockers"], "prior_exposure": plan["prior_exposure"], "raw_logs": plan["raw_logs"], "omitted_journal_events": plan["omitted_events"], "legacy_log_status": plan["state_project"]["log"]}, indent=2, ensure_ascii=False) + "\n").encode()
    state = {"schema_version": 2, "revision": 0, "projects": {plan["project_id"]: plan["state_project"]}}
    return files, state


def _engine_path(root: Path, engine: str | None) -> Path:
    # The runtime engine is shipped beside this migration script; --engine can
    # point at a separately deployed implementation.
    p = Path(engine) if engine else Path(__file__).with_name("learning_system.py")
    if not p.is_absolute():
        candidate = root / p
        p = candidate.resolve() if candidate.exists() else p.resolve()
    if not p.exists(): raise RuntimeError(f"learning engine not found at {p}; dry-run only, nothing was published")
    return p


def _register(root: Path, engine: Path, manifest: Path, initial: Path) -> None:
    result = subprocess.run([sys.executable, str(engine), "--root", str(root), "register", "--manifest", str(manifest), "--initial-state", str(initial)], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"learning engine register failed: {(result.stderr or result.stdout).strip()}")


def _registry_status(root: Path, project_id: str, manifest: Path) -> str:
    state_path = root / ".learning" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        project = state.get("projects", {}).get(project_id)
        if project is None: return "absent"
        expected = str(manifest.relative_to(root))
        return "committed" if project.get("manifest") == expected else "unknown"
    except FileNotFoundError:
        return "absent"
    except (OSError, json.JSONDecodeError, ValueError, AttributeError, TypeError):
        return "unknown"


def migrate(plan: dict[str, Any], root: Path, *, apply: bool = False, engine: str | None = None,
            register: Any | None = None) -> dict[str, Any]:
    if not apply:
        return {"status": "dry-run", "project": plan["project_id"], "source": plan["source"], "blockers": plan["blockers"],
                "modules": len(plan["modules"]), "scores": {m["id"]: m["scores"] for m in plan["modules"]},
                "prior_exposure": plan["prior_exposure"], "review_calendar": plan["reviews"],
                "log": plan["state_project"]["log"], "completed": plan["state_project"]["completed"],
                "omitted_journal_events": plan["omitted_events"], "raw_logs_byte_preserved": plan["raw_logs"], "action": "rerun with --apply only after reviewing this report"}
    engine_path = _engine_path(root, engine) if register is None else Path("<mocked-engine>")
    files, state = build_migration(plan, root)
    project = root / plan["project_path"]
    if os.path.lexists(project / "learning-system") or os.path.lexists(project / "learning-system" / "archive"):
        raise RuntimeError(f"refusing repeated migration: existing learning-system/archive at {project}")
    # Snapshot every destination before any write. Raw logs are not destinations
    # and are never moved. This permits rollback of write failures as well as
    # register failures, but only after registry non-commit is established.
    snapshots: dict[Path, bytes | None] = {}
    created: list[Path] = []
    initial: Path | None = None
    try:
        for rel in files:
            dest = root / rel
            if dest.exists() and not dest.is_file():
                raise RuntimeError(f"refusing output collision: {dest}")
            snapshots[dest] = dest.read_bytes() if dest.exists() else None
        for rel, content in files.items():
            dest = root / rel; dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content); created.append(dest)
        initial_fd, initial_name = tempfile.mkstemp(prefix=f".{plan['project_id']}.state-", suffix=".json", dir=root)
        os.close(initial_fd); initial = Path(initial_name)
        initial.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        manifest = root / plan["project_path"] / "learning-system" / "curriculum.json"
        try:
            (register or _register)(root, engine_path, manifest, initial)
        except Exception as exc:
            status = _registry_status(root, plan["project_id"], manifest)
            if status == "committed":
                raise RuntimeError(f"registration response lost after commit; preserved v2 files and registry: {exc}") from exc
            if status == "unknown":
                raise RuntimeError(f"registration outcome uncertain; preserved v2 files for recovery: {exc}") from exc
            for dest, old in snapshots.items():
                if old is None:
                    try: dest.unlink()
                    except FileNotFoundError: pass
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(old)
            raise
    except Exception:
        # For pre-register write/serialization failures, no registry commit is
        # possible; restore every touched destination.
        if not (root / ".learning" / "state.json").exists() or _registry_status(root, plan["project_id"], root / plan["project_path"] / "learning-system" / "curriculum.json") != "committed":
            for dest, old in snapshots.items():
                try:
                    if old is None: dest.unlink()
                    else: dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(old)
                except FileNotFoundError: pass
        raise
    finally:
        if initial is not None:
            try: initial.unlink()
            except FileNotFoundError: pass
    return {"status": "migrated", "project": plan["project_id"], "blockers": plan["blockers"], "raw_logs_byte_preserved": plan["raw_logs"], "archive": f"{plan['project_path']}/learning-system/archive/legacy"}


def _is_within(child: Path, parent: Path) -> bool:
    try: child.resolve().relative_to(parent.resolve()); return True
    except ValueError: return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source", action="append", required=True, type=Path, help="vault-root-relative Learning.md; repeatable")
    parser.add_argument("--project-id")
    parser.add_argument("--engine")
    parser.add_argument("--apply", action="store_true", help="publish and register; default is dry-run")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if len(args.source) > 1 and args.project_id:
            raise MigrationError("--project-id may only be used with one --source")
        reports = []
        for source_arg in args.source:
            source = Path(source_arg)
            if source.is_absolute():
                try: source.resolve().relative_to(root.resolve())
                except ValueError: raise MigrationError(f"--source must be inside --root: {source}")
            else:
                if ".." in source.parts:
                    raise MigrationError("--source may not contain ..")
                source = root / source
            _, plan = inspect_controller(root, source, project_id=args.project_id if len(args.source) == 1 else None)
            reports.append(migrate(plan, root, apply=args.apply, engine=args.engine))
        print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2, ensure_ascii=False))
        return 0
    except (OSError, MigrationError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
