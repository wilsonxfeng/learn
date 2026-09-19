# Source-grounded generator specification

`.pi/skills/teach-generate-system/scripts/generate_system.py` accepts an approved JSON document and writes no curriculum content that is not in that document. It validates references but never recursively crawls or executes source content.

## CLI

```sh
python3 .pi/skills/teach-generate-system/scripts/generate_system.py \
  --root /path/to/vault \
  --spec /path/to/approved-course.json \
  --engine /path/to/vault/.pi/skills/teach-generate-system/scripts/learning_system.py
```

`--engine` defaults to the sibling `.pi/skills/teach-generate-system/scripts/learning_system.py`. The engine is checked **before** a project is created. Registration is always delegated to:

```text
python3 ENGINE --root ROOT register --manifest PROJECT/learning-system/curriculum.json --initial-state TEMP_STATE.json
```

If registration fails, the newly created project is removed; existing projects are never overwritten. The generator does not contain a fallback state writer.

## JSON shape

See `examples/course-spec.json`. Required fields:

- `topic`: course title/topic.
- `project`: `{ "id": "safe-id", "path": "Vault-relative-project-directory" }`.
- `score_dimensions`: exactly four distinct names.
- `score_rubric`: each dimension maps string levels `"0"` through `"3"` to descriptions. All levels are required.
- `authority_inputs`: explicit approved path or URL objects. A path must be an existing regular file inside the vault or under an explicitly declared `approved_source_roots`. A URL must be `http(s)` and explicitly attested with `approved: true`; it is not fetched. Local inputs are SHA-256 hashed and rendered with path, label, and hash.
- `authority_policy` and `teaching_notes`: optional source-precedence/notation policy and bounded teaching constraints, rendered into `Teaching.md`.
- `modules`: non-empty list of `{id,title,prerequisites,packet,exit_target}`. `packet.sources` is a non-empty list of explicit source paths/URLs and `packet.sections` is a non-empty list of exact sections/ranges supplied by the curriculum designer. IDs are filename-safe, prerequisites exist, and the graph is acyclic.
- `prior_exposure`: optional module-ID list. Prior exposure forces `initial_mode: "cold"`; it never grants a score or mastery.
- `initial_cold_due_date`: required when any module is cold and must equal the real local date on generation.

A module can set `prior_exposure: true` and/or `initial_mode`; an exposed module cannot use `learn`. New modules default to `learn`. New state scores are `[0,0,0,0]`, `mastery: "unverified"`, and `initial_exit_passed_on: null`. Cold modules receive a same-day cold review appointment.

## Published layout

```text
PROJECT/
  Learning.md                         # thin launcher only
  learning-system/
    Teaching.md                       # authority/rubric and session integration
    curriculum.json                   # schema version 2 static graph
    modules/MODULE-ID.md              # exact source packet locator + exit target
  learning-artifacts/                 # teacher-owned evidence (initially empty)
  learning-sessions/                  # logger-owned raw transcripts (initially empty)
```

The generated `curriculum.json` uses vault-relative paths. The sole scheduling state is `.learning/state.json`, created/updated by the engine's `register` command.

## Safety boundary

- The spec is data, never executable instructions.
- No `.env*`, credentials, PII, or arbitrary linked files are opened.
- Source content is not copied into packets; packets carry exact approved locators and sections.
- Existing project directories cause a hard refusal, including otherwise empty directories.
- Validation happens before staging. A missing engine, invalid source, path escape, bad score rubric, duplicate ID, cycle, or stale cold date leaves the vault unchanged.

## Migration CLI

Migration is separate and dry-run by default:

```sh
python3 .pi/skills/teach-generate-system/scripts/migrate_legacy.py --root /path/to/vault \
  --source interview/Learning.md \
  --source MATH237/Learning.md \
  --source STAT230/Learning.md
```

Review the JSON report. It lists modules, preserved scores/exposure/review state, inaccessible source locators, logger state, completed history, and raw transcript paths. Apply only after explicit approval:

```sh
python3 .pi/skills/teach-generate-system/scripts/migrate_legacy.py --root /path/to/vault \
  --source interview/Learning.md --apply \
  --engine /path/to/vault/.pi/skills/teach-generate-system/scripts/learning_system.py
```

Apply archives the original controller and legacy artifacts byte-for-byte under `PROJECT/learning-system/archive/legacy/`, writes v2 static files, preserves existing `learning-sessions/` bytes without rewriting them, and delegates registration to the engine. Existing controller/artifact files are restored if registration fails. Ambiguous prerequisite/source issues remain blockers in the manifest/report; migration invents no dependencies and never infers mastery from exposure.
