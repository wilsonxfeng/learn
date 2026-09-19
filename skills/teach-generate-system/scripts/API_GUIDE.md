# Learning scheduler API

`learning_system.py` is Python 3 stdlib-only and can be invoked either as a CLI or imported by the generator. All paths in state/manifests are vault-relative; registration never replaces an existing project.

## Generator registration

```sh
python3 .pi/skills/teach-generate-system/scripts/learning_system.py \
  --root /path/to/vault register \
  --manifest 'Course Notes/learning-system/curriculum.json' \
  --initial-state /path/to/validated-initial-state.json
```

`--initial-state` is optional. It may contain a complete state document or the project object. Its project object has `manifest`, `modules` (four scores, readiness, initial exit date, mastery, last review), `reviews`, `active`, `log`, `last_session_log`, `last_session`, `completed`, and `sequence`; optional completion journal/attempt history are preserved. The command validates schema, IDs, path confinement, packets, prerequisites, cycles, and readiness score floors, then atomically adds the project to `.learning/state.json`. It refuses duplicate project IDs and preserves legacy mastery labels such as `INTERVIEW_READY` without awarding them from exposure alone.

Equivalent import:

```python
from pathlib import Path
from learning_system import register
register(Path(vault), "Course Notes/learning-system/curriculum.json", initial_state)
```

## Scheduler calls

Import `check`, `agenda`, `validate`, `prepare`, `confirm_log`, `begin`, `finish`, `settle_log`, and `resume`. They return JSON-serialisable dictionaries and raise `LearningSystemError` subclasses on invalid input. Pass `clock=lambda: date(2026, 9, 19)` to APIs that select or mutate by date; CLI `--today` is intentionally accepted only by read-only commands.

The session lifecycle is `prepare` → caller runs the returned `md_log_command` (for example `/md-log Course Notes/learning-sessions/2026-09-19-0000-course-A-01-learn-1--A-01--learn.md`) → caller confirms the exact footer shown by md-log (`expected_footer`, for example `🗒 <basename>`) → `confirm_log` → `begin` → `finish` → learner unlinks → `settle_log`. Prepared/recovery files are truly zero-byte and the scheduler never writes their headers. `resume(..., confirmed_handoff=True)` creates a fresh zero-byte transcript while retaining the stable task ID/checkpoint. `finish` requires an outcome JSON object containing `task_id`, `outcome`, four `scores`, `exit_passed`, and `evidence_path` exactly equal to the selected module manifest artifact; unfinished outcomes also carry `resume_action`.

Only one substantive task may be started for a parent session ID across all registered projects. A same-session overnight `begin` retains the original `started_on` and checkpoint; branch recovery explicitly creates a new raw log. `settle_log` accepts an explicitly confirmed migrated `unconfirmed` pointer with no historical owner.
