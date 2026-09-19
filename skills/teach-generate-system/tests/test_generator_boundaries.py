"""Independent preservation and source-authority regression gates."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import generate_system as gen
import migrate_legacy as mig


class GeneratorBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='source generation ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'notes.txt').write_text('Definition and source-only notation.\n')

    def spec(self):
        return dict(topic='Course: source fidelity', project=dict(id='sample', path='Sample'),
                    score_dimensions=['E', 'P', 'R', 'T'],
                    score_rubric={d: {str(i): f'{d} level {i}' for i in range(4)} for d in 'EPRT'},
                    authority_inputs=[dict(path='notes.txt', label='Authoritative Notes')],
                    authority_policy='Only the supplied TXT is authoritative; no web enrichment.',
                    teaching_notes='Retain source notation and theorem hypotheses exactly.',
                    modules=[dict(id='A-01', title='Concept: definition', prerequisites=[],
                                  packet=dict(sources=[dict(path='notes.txt', label='Authoritative Notes')], sections=['lines 1–1']),
                                  exit_target='Independently derive and apply the source definition.')])

    def generate(self, spec, callback=lambda *args: None):
        file = self.root / 'approved.json'
        file.write_text(json.dumps(spec))
        return gen.generate(file, self.root, register=callback)

    def test_exact_source_path_hash_and_policy_survive_rendering(self):
        self.generate(self.spec())
        packet = (self.root / 'Sample/learning-system/modules/A-01.md').read_text()
        teaching = (self.root / 'Sample/learning-system/Teaching.md').read_text()
        digest = hashlib.sha256((self.root / 'notes.txt').read_bytes()).hexdigest()
        self.assertIn('notes.txt', packet)
        self.assertIn('Authoritative Notes', packet)
        self.assertIn(digest, packet)
        self.assertIn(self.spec()['authority_policy'], teaching)
        self.assertIn(self.spec()['teaching_notes'], teaching)

    def test_environment_source_and_symlink_alias_rejected(self):
        secret = self.root / '.env.local'
        secret.write_text('DO_NOT_READ=secret\n')
        alias = self.root / 'innocent.txt'
        alias.symlink_to(secret)
        for name in ('.env.local', 'innocent.txt'):
            s = self.spec()
            s['authority_inputs'] = [dict(path=name)]
            with self.subTest(name=name), self.assertRaises(gen.SpecError):
                gen.validate_spec(s, self.root)

    def test_reserved_or_parent_segment_project_paths_rejected(self):
        for path in ('.pi/generated-course', '.learning/generated-course', 'safe/../Sample'):
            s = self.spec()
            s['project']['path'] = path
            with self.subTest(path=path), self.assertRaises(gen.SpecError):
                gen.validate_spec(s, self.root)

    def test_lost_register_response_does_not_delete_committed_project(self):
        def committed_then_lost(root, engine, manifest, initial):
            data = json.loads(initial.read_text())
            (root / '.learning').mkdir(exist_ok=True)
            (root / '.learning/state.json').write_text(json.dumps(data))
            raise RuntimeError('response lost after commit')
        try:
            self.generate(self.spec(), committed_then_lost)
        except RuntimeError:
            pass  # reporting uncertainty is acceptable; deleting committed files is not
        state = json.loads((self.root / '.learning/state.json').read_text())
        self.assertTrue((self.root / state['projects']['sample']['manifest']).is_file())


class MigrationBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='legacy migration ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'Course'
        (self.project / 'learning-artifacts').mkdir(parents=True)
        (self.project / 'learning-sessions').mkdir()
        (self.project / 'content').mkdir()
        (self.project / 'content/ch1.txt').write_text('Source chapter.\n')
        self.log = self.project / 'learning-sessions/2026-09-18-1000--A-01--review-1.md'
        self.log.write_bytes(b'Historical transcript\n')
        self.controller = self.project / 'Learning.md'
        self.controller.write_text('''---
title: Course
active_module: null
active_node: null
active_session_log: Course/learning-sessions/2026-09-18-1000--A-01--review-1.md
session_log_state: unconfirmed
last_session: 2026-09-18
review_calendar:
  - {id: A-01-R2, module: A-01, kind: spaced, stage: 2, due: 2026-09-21, status: scheduled, started_on: null}
completion_journal: []
---
# 1. Mission and finish line
Independently reconstruct and apply each result.
# 2. Source-of-truth and notation contract
Only content/ch1.txt is authoritative. Preserve its notation; no outside theorem variants.
# 4.2 Four independent scores
E — Explain; P — Perform; R — Reason; T — Transfer. Each uses levels 0–3.
# 6. Curriculum
|ID|Source packet|Module|Prerequisite|State|Score|Exit focus|
|---|---|---|---|---|---|---|
|A-01|`ch1.txt:1–1`, §1.1|Definition|None|MASTERED|2/2/2/2|Derive the exact definition and solve a changed-assumption problem.|
|A-02|`ch1.txt:1–1`, §1.2|Next concept|Unresolved family of prerequisites|LOCKED|0/0/0/0|Explain the new relation using source notation.|
''')
        self.artifact = self.project / 'learning-artifacts/A-01.md'
        self.artifact.write_text('''---
module: A-01
status: MASTERED
scores: {E: 2, P: 2, R: 2, T: 2}
initial_exit_passed_on: 2026-09-17
prerequisite_ready: true
last_review: 2026-09-18
next_review: 2026-09-21
---
# Learner evidence
Independent derivation and successful delayed retrieval.
''')

    def test_migration_preserves_mastery_log_uncertainty_and_dates(self):
        _, plan = mig.inspect_controller(self.root, self.controller)
        p = plan['state_project']
        self.assertEqual(p['log']['status'], 'unconfirmed')
        self.assertTrue(p['modules']['A-01']['prerequisite_ready'])
        self.assertIn(p['modules']['A-01']['mastery'], ('mastered', 'MASTERED'))
        self.assertEqual(p['reviews'][0]['due'], '2026-09-21')
        self.assertEqual(p['modules']['A-01']['scores'], [2]*4)

    def test_packets_preserve_exit_policy_and_only_valid_graph_edges(self):
        _, plan = mig.inspect_controller(self.root, self.controller, project_id='legacy-test')
        files, _ = mig.build_migration(plan, self.root)
        packet = files[Path('Course/learning-system/modules/A-01.md')].decode()
        policy = files[Path('Course/learning-system/Teaching.md')].decode()
        manifest = json.loads(files[Path('Course/learning-system/curriculum.json')])
        self.assertIn('Derive the exact definition and solve a changed-assumption problem.', packet)
        self.assertIn('Preserve its notation; no outside theorem variants.', policy)
        self.assertIn('legacy-test', files[Path('Course/Learning.md')].decode())
        missing = manifest['modules'][1]
        self.assertEqual(missing['prerequisites'], [])
        self.assertTrue(missing['blockers'])

    def test_interview_ready_label_and_evidence_blockers_preserved(self):
        self.artifact.write_text(self.artifact.read_text().replace('MASTERED', 'INTERVIEW_READY'))
        self.controller.write_text(self.controller.read_text().replace('|LOCKED|0/0/0/0|', '|BLOCKED_EVIDENCE|0/0/0/0|'))
        _, plan = mig.inspect_controller(self.root, self.controller)
        self.assertEqual(plan['state_project']['modules']['A-01']['mastery'], 'INTERVIEW_READY')
        self.assertTrue(any('BLOCKED_EVIDENCE' in b for b in plan['module_blockers']['A-02']))

    def test_pending_review_with_unknown_module_is_not_silently_dropped(self):
        self.controller.write_text(self.controller.read_text().replace('module: A-01, kind: spaced', 'module: MISSING, kind: spaced'))
        with self.assertRaises(mig.MigrationError):
            mig.inspect_controller(self.root, self.controller)

    def test_artifact_archive_is_root_relative_and_byte_exact(self):
        _, plan = mig.inspect_controller(self.root, self.controller)
        files, _ = mig.build_migration(plan, self.root)
        archived = Path('Course/learning-system/archive/legacy/artifacts/A-01.md')
        self.assertIn(archived, files)
        self.assertEqual(files[archived], self.artifact.read_bytes())


if __name__ == '__main__':
    unittest.main()
