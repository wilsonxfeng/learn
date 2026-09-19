"""Independent public-API regression gates; Python standard library only."""
import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import learning_system as ls


class SchedulerBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='learning boundaries ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.day = date(2026, 9, 19)

    def clock(self, day=None):
        return lambda: day or self.day

    def register(self, pid='course', path='Course Notes', modules=None, reviews=None, log=None):
        modules = modules or [('A-01', [], 'learn'), ('A-02', ['A-01'], 'learn')]
        defs = []
        for mid, deps, mode in modules:
            packet = f'{path}/learning-system/modules/{mid}.md'
            artifact = f'{path}/learning-artifacts/{mid}.md'
            defs.append(dict(id=mid, title=mid, prerequisites=deps, initial_mode=mode,
                             packet=packet, artifact=artifact, blockers=[]))
            for rel, body in ((packet, '# Source packet\nExit: independent reconstruction and transfer.\n'),
                              (artifact, '# Learner evidence\nIndependently reconstructed the source model, applied it and explained a changed assumption.\n')):
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
        teaching = f'{path}/learning-system/Teaching.md'
        (self.root / teaching).write_text('# Authority\nUse only approved sources.\n')
        manifest = dict(schema_version=2, id=pid, title=pid, project_path=path,
                        teaching_packet=teaching, score_dimensions=['E', 'P', 'R', 'T'], modules=defs)
        mp = self.root / path / 'learning-system/curriculum.json'
        mp.write_text(json.dumps(manifest))
        initial = dict(modules={m['id']: dict(scores=[0]*4, prerequisite_ready=False,
                                             initial_exit_passed_on=None, mastery='unverified', last_review=None)
                                for m in defs}, reviews=reviews or [], active=None,
                       log=log or dict(path=None, status='none', session_id=None),
                       completed={}, sequence=0, last_session=None, last_session_log=None)
        ip = self.root / f'{pid}-initial.json'
        ip.write_text(json.dumps(initial))
        ls.register(self.root, str(mp), str(ip))
        return manifest

    def state(self):
        return json.loads((self.root / '.learning/state.json').read_text())

    def start(self, pid='course', sid='teacher-1', day=None):
        prep = ls.prepare(self.root, pid, sid, clock=self.clock(day))
        ls.confirm_log(self.root, pid, sid, prep['path'])
        began = ls.begin(self.root, pid, sid, clock=self.clock(day))
        return prep, began

    def outcome(self, began, pid='course', status='passed', scores=None, artifact=None):
        p = self.state()['projects'][pid]
        manifest = json.loads((self.root / p['manifest']).read_text())
        definition = next(m for m in manifest['modules'] if m['id'] == began['module'])
        body = dict(task_id=began['task_id'], outcome=status, scores=scores or [2]*4,
                    exit_passed=status == 'passed', evidence_path=artifact or definition['artifact'],
                    summary='Independent source reconstruction and changed-assumption application.')
        if status != 'passed':
            body.update(node='fresh-application', resume_action='Generate a fresh application unaided.')
        rel = f"{manifest['project_path']}/learning-system/outcomes/{began['task_id']}-{status}.json"
        op = self.root / rel
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(body))
        return rel

    def test_prepare_uses_registered_path_and_truly_empty_canonical_log(self):
        self.register()
        p = ls.prepare(self.root, 'course', 'teacher', clock=self.clock())
        log = self.root / p['path']
        self.assertEqual(log.parent, self.root / 'Course Notes/learning-sessions')
        self.assertEqual(log.read_bytes(), b'')
        self.assertRegex(log.name, r'^\d{4}-\d{2}-\d{2}-\d{4}(?:[^/]*)--A-01--learn\.md$')
        self.assertIsNone(self.state()['projects']['course']['active'])

    def test_cold_pass_unlocks_next_module_but_not_delayed_mastery(self):
        self.register(modules=[('A-01', [], 'cold'), ('A-02', ['A-01'], 'learn')],
                      reviews=[dict(id='A-01-cold', module='A-01', kind='cold', stage=None,
                                    due='2026-09-18', status='scheduled', started_on=None)])
        _, b = self.start()
        ls.finish(self.root, 'course', 'teacher-1', self.outcome(b), clock=self.clock())
        p = self.state()['projects']['course']
        self.assertTrue(p['modules']['A-01']['prerequisite_ready'])
        self.assertEqual(p['modules']['A-01']['initial_exit_passed_on'], '2026-09-19')
        self.assertNotIn(p['modules']['A-01']['mastery'], ('mastered', 'MASTERED', 'INTERVIEW_READY'))
        self.assertEqual(p['reviews'][0]['due'], '2026-09-20')
        self.assertEqual(ls.check(self.root, 'course', clock=self.clock())['module'], 'A-02')

    def test_due_sort_uses_manifest_order_not_lexicographic_id(self):
        self.register(modules=[('Z-01', [], 'cold'), ('A-01', [], 'cold')], reviews=[
            dict(id=f'{m}-cold', module=m, kind='cold', stage=None, due='2026-09-18', status='scheduled', started_on=None)
            for m in ('A-01', 'Z-01')])
        self.assertEqual(ls.check(self.root, 'course', clock=self.clock())['module'], 'Z-01')

    def test_dependency_repair_recurses_to_root_and_is_actionable(self):
        self.register(modules=[('A-01', [], 'learn'), ('A-02', ['A-01'], 'learn'),
                               ('A-03', ['A-02'], 'cold')], reviews=[
            dict(id='A-03-cold', module='A-03', kind='cold', stage=None, due='2026-09-18', status='scheduled', started_on=None)])
        handoff = ls.check(self.root, 'course', clock=self.clock())
        self.assertEqual((handoff['module'], handoff['kind']), ('A-01', 'repair'))
        self.assertNotEqual(handoff['verdict'], 'BLOCKED')
        _, b = self.start()
        ls.finish(self.root, 'course', 'teacher-1', self.outcome(b), clock=self.clock())
        p = self.state()['projects']['course']
        self.assertTrue(p['modules']['A-01']['prerequisite_ready'])
        self.assertTrue(any(r['module'] == 'A-01' and r['due'] == '2026-09-20' for r in p['reviews']))

    def test_finish_rejects_another_modules_artifact(self):
        self.register()
        _, b = self.start()
        wrong = self.outcome(b, artifact='Course Notes/learning-artifacts/A-02.md')
        before = self.state()
        with self.assertRaises(ls.LearningSystemError):
            ls.finish(self.root, 'course', 'teacher-1', wrong, clock=self.clock())
        self.assertEqual(self.state(), before)

    def test_pause_records_evidence_scores_and_actual_date(self):
        self.register()
        _, b = self.start()
        outcome = self.outcome(b, status='paused', scores=[1, 2, 1, 2])
        ls.finish(self.root, 'course', 'teacher-1', outcome, clock=self.clock(date(2026, 9, 20)))
        p = self.state()['projects']['course']
        self.assertEqual(p['modules']['A-01']['scores'], [1, 2, 1, 2])
        self.assertEqual(p['last_session'], '2026-09-20')
        self.assertEqual(p['active']['node'], 'fresh-application')
        self.assertEqual(p['reviews'], [])

    def test_same_session_resumes_next_day_without_resetting_task(self):
        self.register()
        _, b = self.start()
        ls.finish(self.root, 'course', 'teacher-1', self.outcome(b, status='paused'), clock=self.clock())
        prior = self.state()['projects']['course']['active']
        resumed = ls.begin(self.root, 'course', 'teacher-1', clock=self.clock(date(2026, 9, 20)))
        self.assertEqual(resumed['task_id'], b['task_id'])
        after = self.state()['projects']['course']['active']
        self.assertEqual(after['started_on'], prior['started_on'])
        self.assertEqual(after['node'], prior['node'])

    def test_idempotent_retry_does_not_change_state_or_date(self):
        self.register()
        _, b = self.start()
        out = self.outcome(b)
        result = ls.finish(self.root, 'course', 'teacher-1', out, clock=self.clock())
        self.assertEqual(result['revision'], self.state()['revision'])
        before = (self.root / '.learning/state.json').read_bytes()
        ls.finish(self.root, 'course', 'teacher-1', out, clock=self.clock(date(2026, 9, 23)))
        self.assertEqual((self.root / '.learning/state.json').read_bytes(), before)
        altered = json.loads((self.root / out).read_text())
        altered['outcome'] = 'failed'
        altered['exit_passed'] = False
        (self.root / out).write_text(json.dumps(altered))
        with self.assertRaises(ls.LearningSystemError):
            ls.finish(self.root, 'course', 'teacher-1', out, clock=self.clock())

    def test_migrated_unknown_log_can_be_settled_without_fabricating_owner(self):
        path = 'Course Notes/learning-sessions/2026-09-18-1200--A-01--review-1.md'
        p = self.root / path
        p.parent.mkdir(parents=True)
        p.write_text('Historical transcript.\n')
        self.register(log=dict(path=path, status='unconfirmed', session_id=None))
        ls.settle_log(self.root, 'course', 'new-teacher', confirmed_unlinked=True)
        self.assertEqual(self.state()['projects']['course']['last_session_log'], path)
        self.assertEqual(p.read_text(), 'Historical transcript.\n')

    def test_same_session_branch_recovery_allocates_new_empty_log(self):
        self.register()
        prep, b = self.start()
        old = self.root / prep['path']
        old.write_text('Logged historical branch.\n')
        new = ls.resume(self.root, 'course', 'teacher-1', confirmed_handoff=True, clock=self.clock())
        self.assertEqual(new['task_id'], b['task_id'])
        self.assertNotEqual(new['path'], prep['path'])
        self.assertEqual((self.root / new['path']).read_bytes(), b'')
        self.assertEqual(old.read_text(), 'Logged historical branch.\n')

    def test_read_only_check_does_not_create_unregistered_workspace_state(self):
        with self.assertRaises(ls.LearningSystemError):
            ls.check(self.root, 'missing', clock=self.clock())
        self.assertFalse((self.root / '.learning').exists())

    def test_validate_rejects_manifest_artifact_escape_to_other_project(self):
        self.register()
        mp = self.root / 'Course Notes/learning-system/curriculum.json'
        manifest = json.loads(mp.read_text())
        manifest['modules'][0]['artifact'] = 'Other/learning-artifacts/A-01.md'
        mp.write_text(json.dumps(manifest))
        with self.assertRaises(ls.LearningSystemError):
            ls.validate(self.root, project='course')

    def test_validate_rejects_missing_teaching_packet(self):
        self.register()
        (self.root / 'Course Notes/learning-system/Teaching.md').unlink()
        with self.assertRaises(ls.LearningSystemError):
            ls.validate(self.root, project='course')

    def test_validate_rejects_readiness_without_score_evidence(self):
        self.register()
        state = self.state()
        state['projects']['course']['modules']['A-01']['prerequisite_ready'] = True
        (self.root / '.learning/state.json').write_text(json.dumps(state))
        with self.assertRaises(ls.LearningSystemError):
            ls.validate(self.root, project='course')

    def test_registry_symlink_cannot_write_outside_vault(self):
        outside = tempfile.TemporaryDirectory(prefix='outside learning boundary ')
        self.addCleanup(outside.cleanup)
        (self.root / '.learning').symlink_to(Path(outside.name), target_is_directory=True)
        with self.assertRaises(ls.LearningSystemError):
            self.register()
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_one_substantive_task_per_session_across_projects(self):
        self.register()
        self.register(pid='second', path='Second Course', modules=[('B-01', [], 'learn')])
        _, b = self.start()
        ls.finish(self.root, 'course', 'teacher-1', self.outcome(b), clock=self.clock())
        ls.settle_log(self.root, 'course', 'teacher-1', confirmed_unlinked=True)
        with self.assertRaises(ls.LearningSystemError):
            ls.prepare(self.root, 'second', 'teacher-1', clock=self.clock())


if __name__ == '__main__':
    unittest.main()
