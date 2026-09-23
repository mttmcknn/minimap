import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from replay_text import collect


class CharacterEncoder:
    def encode_ordinary(self, text):
        return list(text)


class ReplayTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / 'cohort'
        self.run_dir.mkdir()
        self.skill = self.root / 'skill.md'
        self.skill.write_text('skill')
        self.rows, self.calls = [], []
        # Setup text is retained in the audit but must not enter navigation totals.
        streams = [('setup', 'large setup text', ''), ('raw', 'abcd', '!'),
                   ('baseline', 'abc', ''), ('candidate', 'xy', '')]
        for index, (phase, stdout, stderr) in enumerate(streams):
            call = dict(phase=phase, seconds=1, exit_code=0,
                        stdout_bytes=len(stdout), stderr_bytes=len(stderr))
            self.calls.append(call)
            for name, text in (('stdout', stdout), ('stderr', stderr)):
                (self.run_dir / f'{index:03d}.{name}').write_text(text)
            if phase != 'setup':
                self.rows.append(dict(api='36', sample='jetsnack', repeat=1, arm=phase,
                    phase=phase, setup_passed=True, reported_ok=True, oracle_reached_target=True,
                    graph_unchanged=True, error=None, commands=1, seconds=1,
                    stdout_bytes=len(stdout), stderr_bytes=len(stderr)))
        self.encoders = {name: CharacterEncoder() for name in ('o200k_base', 'cl100k_base')}

    def summary(self):
        report = {'metadata': {'suite': 'fixture'}, 'trials': self.rows, 'calls': self.calls}
        path = self.run_dir / 'results.json'
        path.write_text(json.dumps(report))
        return {'inputs': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'metadata': report['metadata']}],
                'trials': self.rows, 'planned_trials': 3, 'recorded_trials': 3,
                'groups': [{'api': '36', 'sample': 'jetsnack'}]}

    def test_streams_count_once_setup_and_skill_stay_separate_from_usage(self):
        result = collect(self.summary(), self.root, self.encoders, self.skill)
        self.assertEqual([row['o200k_base'] for row in result['trials']], [5, 3, 2])
        self.assertEqual(result['groups'][0]['paired'][0]['percent_less_text']['o200k_base'], 60)
        self.assertEqual(result['skill']['tokens']['o200k_base'], 5)
        self.assertIsNone(result['model_usage'])
        self.assertIsNone(result['api_cost_saving'])
        self.assertEqual(len(result['calls']), 4)

    def test_failed_attempt_keeps_its_text_and_denominator_but_has_no_savings_pair(self):
        self.rows[-1]['reported_ok'] = False
        self.rows[-1]['oracle_reached_target'] = False
        result = collect(self.summary(), None, self.encoders, self.skill)
        self.assertEqual((result['recorded_trials'], result['successes']), (3, 2))
        self.assertEqual(result['trials'][-1]['o200k_base'], 2)
        self.assertEqual(result['groups'][0]['paired'], [])
        self.assertIsNone(result['groups'][0]['arms']['candidate']['median_successful_tokens']['o200k_base'])

    def test_missing_output_bytes_are_rejected(self):
        summary = self.summary()
        (self.run_dir / '001.stdout').write_text('a')
        with self.assertRaisesRegex(ValueError, 'Byte count mismatch'):
            collect(summary, None, self.encoders, self.skill)

    def test_source_report_changes_are_rejected(self):
        summary = self.summary()
        with (self.run_dir / 'results.json').open('a') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, 'Source hash mismatch'):
            collect(summary, None, self.encoders, self.skill)


if __name__ == '__main__':
    unittest.main()
