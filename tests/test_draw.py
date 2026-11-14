import hashlib
import json
import os
import random
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from survey_estimate import draw
from survey_estimate.draw import iter_sample, parse_quotas, process, publish

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FRAME = ROOT / 'fixtures/frame.jsonl'
QUOTAS = {'alpha': 2, 'beta': 2, 'gamma': 1}
QUOTAS_JSON = json.dumps(QUOTAS, sort_keys=True, separators=(',', ':'))
SEED = 'demo-1'


def frame_lines(units):
    """Serialize (unit_id, stratum) pairs as JSONL, one compact object per line."""
    return ''.join(json.dumps({'unit_id': uid, 'stratum': sid}, ensure_ascii=False,
                              separators=(',', ':')) + '\n' for uid, sid in units)


def write_frame(directory, units, name='frame.jsonl'):
    path = Path(directory) / name
    path.write_text(frame_lines(units), encoding='utf-8')
    return path


def expected_draw(units, quotas, seed):
    """Independent hash-ordered draw: sort by (digest, unit_id), take quota per stratum."""
    by_stratum = {}
    for uid, sid in units:
        digest = hashlib.sha256(seed.encode('utf-8') + b'\x00' + uid.encode('utf-8')).digest()
        by_stratum.setdefault(sid, []).append((digest, uid))
    expected = []
    for sid, entries in by_stratum.items():
        entries.sort()
        expected.extend((uid, sid) for _, uid in entries[:quotas[sid]])
    return sorted(expected)


def run_cli(frame, quotas, seed, state, output):
    return subprocess.run([sys.executable, '-m', 'survey_estimate.draw',
                           '--frame', str(frame), '--quotas', quotas, '--seed', seed,
                           '--state', str(state), '--output', str(output)],
                          capture_output=True, text=True)


def wait_offset(state, minimum, timeout=60):
    """Wait until the checkpoint reports a committed offset of at least minimum."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            db = sqlite3.connect('file:%s?mode=ro' % state, uri=True)
            try:
                row = db.execute("SELECT value FROM meta WHERE key='committed_offset'").fetchone()
                if row and int(row[0]) >= minimum:
                    return int(row[0])
            finally:
                db.close()
        except sqlite3.Error:
            pass
        time.sleep(0.05)
    raise AssertionError('no committed offset >= %d within timeout' % minimum)


def db_dump(state):
    """Logical content of the checkpoint database, order-independent per table."""
    db = sqlite3.connect('file:%s?mode=ro' % state, uri=True)
    try:
        dump = {}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            dump[table] = sorted(tuple(str(v) for v in row)
                                 for row in db.execute('SELECT * FROM ' + table))
        return dump
    finally:
        db.close()


class QuotasTests(unittest.TestCase):
    def test_valid_quotas(self):
        self.assertEqual(parse_quotas('{"a": 2, "b": 0}'), {'a': 2, 'b': 0})
        self.assertEqual(parse_quotas('{}'), {})
        self.assertEqual(parse_quotas('{"a": 100000}'), {'a': 100000})

    def test_rejects_non_object_and_bad_values(self):
        for raw in ('[]', '"x"', '1', 'null', '{"a": -1}', '{"a": 1.5}', '{"a": "2"}',
                    '{"a": true}', '{"a": null}', '{"a": 1e3}', '{"a": NaN}',
                    '{"a": 1, "a": 2}', '{bad}', '{"": 1}', '{"a": 100001}',
                    '{"a": 50001, "b": 50000}'):
            with self.assertRaises(ValueError, msg=raw):
                parse_quotas(raw)

    def test_rejects_long_and_control_stratum_ids(self):
        with self.assertRaises(ValueError):
            parse_quotas(json.dumps({'x' * 257: 1}))
        with self.assertRaises(ValueError):
            parse_quotas('{"a\\tb": 1}')


class DrawTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def draw(self, units, quotas, seed=SEED, state_name='state.db'):
        frame = write_frame(self.dir, units)
        lock_fd, db = process(frame, quotas, seed, self.dir / state_name)
        try:
            return list(iter_sample(db))
        finally:
            db.close()
            os.close(lock_fd)

    def test_selection_matches_independent_hash_order(self):
        rng = random.Random(11)
        units = [('u%03d' % i, rng.choice('abcd')) for i in range(2000)]
        quotas = {'a': 50, 'b': 0, 'c': 400, 'd': 3}
        got = self.draw(units, quotas)
        self.assertEqual(got, expected_draw(units, quotas, SEED))
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), 453)

    def test_hand_computed_hash_vector(self):
        # SHA256("vector-seed" + NUL + uid) fixes this exact order.
        units = [('u3', 'h'), ('u1', 'h'), ('u2', 'h')]
        got = self.draw(units, {'h': 2}, seed='vector-seed')
        self.assertEqual(got, [('u1', 'h'), ('u2', 'h')])
        got = self.draw(units, {'h': 3}, seed='vector-seed', state_name='s2.db')
        self.assertEqual(got, [('u1', 'h'), ('u2', 'h'), ('u3', 'h')])

    def test_census_when_quota_equals_population(self):
        units = [('a%d' % i, 'h') for i in range(5)]
        got = self.draw(units, {'h': 5})
        self.assertEqual(got, sorted((uid, 'h') for uid, _ in units))

    def test_zero_quota_stratum_units_are_accepted_but_not_drawn(self):
        units = [('a1', 'x'), ('a2', 'y'), ('a3', 'x')]
        got = self.draw(units, {'x': 0, 'y': 1})
        self.assertEqual(got, [('a2', 'y')])

    def test_empty_frame_and_empty_quotas_yield_empty_sample(self):
        got = self.draw([], {})
        self.assertEqual(got, [])

    def test_order_independence(self):
        rng = random.Random(3)
        units = [('u%04d' % i, 's%d' % (i % 3)) for i in range(500)]
        shuffled = units[:]
        rng.shuffle(shuffled)
        quotas = {'s0': 20, 's1': 20, 's2': 20}
        self.assertEqual(self.draw(units, quotas), self.draw(shuffled, quotas, state_name='s2.db'))

    def test_unknown_stratum_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.draw([('u1', 'ghost')], {'h': 1})
        self.assertIn('unknown stratum', str(ctx.exception))

    def test_duplicate_unit_rejected_within_and_across_batches(self):
        with self.assertRaises(ValueError) as ctx:
            self.draw([('u1', 'h'), ('u2', 'h'), ('u1', 'h')], {'h': 1})
        self.assertIn('duplicate unit_id', str(ctx.exception))
        original = draw.BATCH_ROWS
        draw.BATCH_ROWS = 2
        try:
            with self.assertRaises(ValueError) as ctx:
                self.draw([('u1', 'h'), ('u2', 'h'), ('u3', 'h'), ('u4', 'h'), ('u1', 'h')],
                          {'h': 1}, state_name='s2.db')
            self.assertIn('duplicate unit_id', str(ctx.exception))
        finally:
            draw.BATCH_ROWS = original

    def test_bad_lines_rejected(self):
        bad = ['{"unit_id":"u1"}',
               '{"unit_id":"u1","stratum":"h","extra":1}',
               '{"unit_id":"u1","unit_id":"u2","stratum":"h"}',
               '{"unit_id":"","stratum":"h"}',
               '{"unit_id":"u1","stratum":""}',
               '{"unit_id":"' + 'x' * 300 + '","stratum":"h"}',
               '{"unit_id":123,"stratum":"h"}',
               '{"unit_id":"u1","stratum":["h"]}',
               '{"unit_id":"u1","stratum":"h"}\t{"unit_id":"u2","stratum":"h"}',
               '[1, 2]', '"text"', '123', 'not json', '{"unit_id":"u1",}',
               ]
        quotas = {'h': 1}
        for i, line in enumerate(bad):
            frame = self.dir / ('bad%d.jsonl' % i)
            frame.write_text('{"unit_id":"ok","stratum":"h"}\n' + line + '\n', encoding='utf-8')
            with self.assertRaises(ValueError, msg=line):
                process(frame, quotas, SEED, self.dir / ('bad%d.db' % i))

    def test_bad_utf8_and_control_characters_rejected(self):
        frame = self.dir / 'bad.jsonl'
        frame.write_bytes(b'{"unit_id":"u1","stratum":"h"}\n{"unit_id":"\xff","stratum":"h"}\n')
        with self.assertRaises(ValueError):
            process(frame, {'h': 1}, SEED, self.dir / 'bad.db')
        frame.write_bytes(b'{"unit_id":"u\\t1","stratum":"h"}\n')
        with self.assertRaises(ValueError):
            process(frame, {'h': 1}, SEED, self.dir / 'bad2.db')
        frame.write_bytes(b'\xef\xbb\xbf{"unit_id":"u1","stratum":"h"}\n')
        with self.assertRaises(ValueError):
            process(frame, {'h': 1}, SEED, self.dir / 'bad3.db')

    def test_blank_lines_are_skipped(self):
        frame = self.dir / 'blank.jsonl'
        frame.write_text('\n{"unit_id":"u1","stratum":"h"}\n   \n{"unit_id":"u2","stratum":"h"}\n',
                         encoding='utf-8')
        lock_fd, db = process(frame, {'h': 2}, SEED, self.dir / 'blank.db')
        try:
            self.assertEqual(len(list(iter_sample(db))), 2)
        finally:
            db.close()
            os.close(lock_fd)

    def test_insufficient_population_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.draw([('u1', 'h'), ('u2', 'h')], {'h': 3})
        self.assertIn('below quota', str(ctx.exception))
        with self.assertRaises(ValueError):
            self.draw([('u1', 'h')], {'h': 1, 'ghost': 1}, state_name='s2.db')

    def test_seed_validation(self):
        frame = write_frame(self.dir, [('u1', 'h')])
        for bad in ('', '   ', 'x' * 257, 'a\tb'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                process(frame, {'h': 1}, bad, self.dir / 'seed.db')

    def test_resume_after_partial_state_skips_committed_records(self):
        # A parse failure rolls back the current batch; the committed prefix
        # stays valid and a rerun continues after it without re-parsing it.
        original = draw.BATCH_ROWS
        draw.BATCH_ROWS = 2
        try:
            units = [('u%d' % i, 'h') for i in range(4)]
            frame = self.dir / 'frame.jsonl'
            frame.write_text(frame_lines(units) + '{"unit_id":"broken"}\n', encoding='utf-8')
            state = self.dir / 'state.db'
            with self.assertRaises(ValueError):
                process(frame, {'h': 6}, SEED, state)
            # Two batches are committed and the checkpoint is still running.
            probe = sqlite3.connect('file:%s?mode=ro' % state, uri=True)
            try:
                self.assertEqual(probe.execute("SELECT value FROM meta WHERE key='status'")
                                 .fetchone()[0], 'running')
                self.assertEqual(probe.execute('SELECT COUNT(*) FROM units').fetchone()[0], 4)
            finally:
                probe.close()
            # Same committed prefix, valid tail: the rerun completes the draw.
            frame.write_text(frame_lines(units + [('u4', 'h'), ('u5', 'h')]), encoding='utf-8')
            lock_fd, db = process(frame, {'h': 6}, SEED, state)
            try:
                self.assertEqual([uid for uid, _ in iter_sample(db)],
                                 ['u0', 'u1', 'u2', 'u3', 'u4', 'u5'])
            finally:
                db.close()
                os.close(lock_fd)
        finally:
            draw.BATCH_ROWS = original

    def test_changed_committed_prefix_rejected_without_touching_db(self):
        original = draw.BATCH_ROWS
        draw.BATCH_ROWS = 2
        try:
            units = [('u%d' % i, 'h') for i in range(4)]
            frame = self.dir / 'frame.jsonl'
            frame.write_text(frame_lines(units) + '{"unit_id":"broken"}\n', encoding='utf-8')
            state = self.dir / 'state.db'
            with self.assertRaises(ValueError):
                process(frame, {'h': 2}, SEED, state)
            before = db_dump(state)
            # Flip one byte inside the first committed batch, keeping length.
            raw = bytearray(frame.read_bytes())
            raw[15] ^= 0x01
            frame.write_bytes(bytes(raw))
            with self.assertRaises(ValueError) as ctx:
                process(frame, {'h': 2}, SEED, state)
            self.assertIn('frame changed', str(ctx.exception))
            self.assertEqual(db_dump(state), before)
        finally:
            draw.BATCH_ROWS = original

    def test_binding_changes_rejected(self):
        frame = write_frame(self.dir, [('u1', 'h'), ('u2', 'h')])
        state = self.dir / 'state.db'
        lock_fd, db = process(frame, {'h': 1}, SEED, state)
        db.close()
        os.close(lock_fd)
        before = db_dump(state)
        with self.assertRaises(ValueError) as ctx:
            process(frame, {'h': 1}, 'other-seed', state)
        self.assertIn('seed differs', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            process(frame, {'h': 2}, SEED, state)
        self.assertIn('quotas differ', str(ctx.exception))
        self.assertEqual(db_dump(state), before)

    def test_foreign_and_corrupt_state_rejected(self):
        frame = write_frame(self.dir, [('u1', 'h')])
        garbage = self.dir / 'garbage.db'
        garbage.write_bytes(b'this is not a sqlite database at all........')
        with self.assertRaises(Exception):
            process(frame, {'h': 1}, SEED, garbage)
        foreign = self.dir / 'foreign.db'
        db = sqlite3.connect(foreign)
        db.execute('CREATE TABLE something(x)')
        db.commit()
        db.close()
        with self.assertRaises(ValueError) as ctx:
            process(frame, {'h': 1}, SEED, foreign)
        self.assertIn('not a survey-draw checkpoint', str(ctx.exception))

    def test_publish_format_matches_json_dump(self):
        rows = [('u1', 'h'), ('u2', 'h'), ('a b', 's/ü')]
        out = self.dir / 'out.json'
        publish(out, iter(rows))
        expected = {'sample': [{'unit_id': uid, 'stratum': sid} for uid, sid in rows]}
        dumped = json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        self.assertEqual(out.read_text(encoding='utf-8'), dumped)
        self.assertEqual(json.loads(out.read_text(encoding='utf-8')), expected)
        empty = self.dir / 'empty.json'
        publish(empty, iter([]))
        self.assertEqual(empty.read_text(encoding='utf-8'),
                         json.dumps({'sample': []}, indent=2) + '\n')

    def test_publish_is_atomic_and_replaces(self):
        out = self.dir / 'out.json'
        out.write_text('previous', encoding='utf-8')
        publish(out, iter([('u1', 'h')]))
        self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['sample'][0]['unit_id'], 'u1')
        self.assertFalse(list(self.dir.glob('tmp*')))
        with self.assertRaises(ValueError):
            publish(out, self._failing_rows())
        self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['sample'][0]['unit_id'], 'u1')

    def _failing_rows(self):
        yield ('u1', 'h')
        raise ValueError('boom')


class CliTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def paths(self, frame='frame.jsonl', state='state.db', output='out.json'):
        return self.dir / frame, self.dir / state, self.dir / output

    def test_cli_success_output_shape_and_stdout(self):
        frame, state, out = self.paths()
        shutil.copy(FIXTURE_FRAME, frame)
        r = run_cli(frame, QUOTAS_JSON, SEED, state, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(set(doc), {'sample'})
        self.assertTrue(all(set(row) == {'unit_id', 'stratum'} for row in doc['sample']))
        ids = [row['unit_id'] for row in doc['sample']]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 5)
        self.assertEqual(json.loads(r.stdout), doc)
        # Independent recomputation from the fixture file.
        units = []
        for line in FIXTURE_FRAME.read_text(encoding='utf-8').splitlines():
            obj = json.loads(line)
            units.append((obj['unit_id'], obj['stratum']))
        self.assertEqual([(row['unit_id'], row['stratum']) for row in doc['sample']],
                         expected_draw(units, QUOTAS, SEED))

    def test_cli_rerun_and_kill_recovery_produce_identical_files(self):
        units = [('u%06d' % i, 's%d' % (i % 4)) for i in range(60000)]
        quotas = {'s0': 40, 's1': 40, 's2': 40, 's3': 40}
        quotas_json = json.dumps(quotas)
        frame, _, reference_out = self.paths(output='ref.json')
        write_frame(self.dir, units)
        reference_state = self.dir / 'refstate.db'
        r = run_cli(frame, quotas_json, SEED, reference_state, reference_out)
        self.assertEqual(r.returncode, 0, r.stderr)
        reference = reference_out.read_bytes()

        # Kill after several committed batches, then resume with the same command.
        state, out = self.dir / 'state.db', self.dir / 'out.json'
        proc = subprocess.Popen([sys.executable, '-m', 'survey_estimate.draw',
                                 '--frame', str(frame), '--quotas', quotas_json, '--seed', SEED,
                                 '--state', str(state), '--output', str(out)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_offset(state, 1)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        self.assertNotEqual(proc.returncode, 0)
        r = run_cli(frame, quotas_json, SEED, state, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_bytes(), reference)

        # Kill before the first commit, then resume.
        state2, out2 = self.dir / 'state2.db', self.dir / 'out2.json'
        proc = subprocess.Popen([sys.executable, '-m', 'survey_estimate.draw',
                                 '--frame', str(frame), '--quotas', quotas_json, '--seed', SEED,
                                 '--state', str(state2), '--output', str(out2)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        r = run_cli(frame, quotas_json, SEED, state2, out2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out2.read_bytes(), reference)

        # Kill after publication, then rerun: identical file again.
        state3, out3 = self.dir / 'state3.db', self.dir / 'out3.json'
        proc = subprocess.Popen([sys.executable, '-m', 'survey_estimate.draw',
                                 '--frame', str(frame), '--quotas', quotas_json, '--seed', SEED,
                                 '--state', str(state3), '--output', str(out3)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        while not out3.exists() and time.time() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        r = run_cli(frame, quotas_json, SEED, state3, out3)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out3.read_bytes(), reference)

    def test_cli_cross_batch_duplicate_rejected(self):
        units = [('u%06d' % i, 'h') for i in range(25000)]
        units.append(('u000007', 'h'))  # duplicate far beyond the first batches
        frame, state, out = self.paths()
        write_frame(self.dir, units)
        r = run_cli(frame, '{"h": 10}', SEED, state, out)
        self.assertEqual(r.returncode, 2)
        self.assertIn('duplicate unit_id', r.stderr)
        self.assertFalse(out.exists())

    def test_cli_binding_changes_keep_output_and_db(self):
        frame, state, out = self.paths()
        shutil.copy(FIXTURE_FRAME, frame)
        r = run_cli(frame, QUOTAS_JSON, SEED, state, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        before_out = out.read_bytes()
        before_db = db_dump(state)
        for quotas, seed in ((QUOTAS_JSON, 'other-seed'),
                             (json.dumps({'alpha': 3, 'beta': 2, 'gamma': 1}), SEED)):
            r = run_cli(frame, quotas, seed, state, out)
            self.assertEqual(r.returncode, 2, (quotas, seed))
            self.assertEqual(out.read_bytes(), before_out)
            self.assertEqual(db_dump(state), before_db)
        # Frame byte change after completion is rejected the same way.
        with open(frame, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'unit_id': 'Z9', 'stratum': 'alpha'}) + '\n')
        r = run_cli(frame, QUOTAS_JSON, SEED, state, out)
        self.assertEqual(r.returncode, 2)
        self.assertIn('frame changed', r.stderr)
        self.assertEqual(out.read_bytes(), before_out)
        self.assertEqual(db_dump(state), before_db)

    def test_cli_order_independence(self):
        rng = random.Random(99)
        units = [('u%05d' % i, 's%d' % (i % 5)) for i in range(3000)]
        shuffled = units[:]
        rng.shuffle(shuffled)
        quotas_json = json.dumps({'s0': 10, 's1': 10, 's2': 10, 's3': 10, 's4': 10})
        frame_a = write_frame(self.dir, units, 'a.jsonl')
        frame_b = write_frame(self.dir, shuffled, 'b.jsonl')
        out_a, out_b = self.dir / 'a.json', self.dir / 'b.json'
        r = run_cli(frame_a, quotas_json, SEED, self.dir / 'a.db', out_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli(frame_b, quotas_json, SEED, self.dir / 'b.db', out_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())

    def test_cli_concurrent_run_rejected(self):
        import fcntl
        frame, state, out = self.paths()
        shutil.copy(FIXTURE_FRAME, frame)
        fd = os.open(state, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            r = run_cli(frame, QUOTAS_JSON, SEED, state, out)
            self.assertEqual(r.returncode, 2)
            self.assertIn('locked', r.stderr)
            self.assertFalse(out.exists())
        finally:
            os.close(fd)

    def test_cli_failures_keep_previous_output(self):
        frame, state, out = self.paths()
        shutil.copy(FIXTURE_FRAME, frame)
        r = run_cli(frame, QUOTAS_JSON, SEED, state, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        before = out.read_bytes()
        bad = self.dir / 'bad.jsonl'
        bad.write_text('{"unit_id":"u1","stratum":"ghost"}\n', encoding='utf-8')
        r = run_cli(bad, QUOTAS_JSON, SEED, self.dir / 'bad.db', out)
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown stratum', r.stderr)
        self.assertEqual(out.read_bytes(), before)
        r = run_cli(frame, QUOTAS_JSON, SEED, state, self.dir / 'missing/out.json')
        self.assertEqual(r.returncode, 0)  # missing parent directories are created
        r = run_cli(self.dir / 'missing.jsonl', QUOTAS_JSON, SEED, self.dir / 'm.db', out)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(out.read_bytes(), before)

    def test_cli_rejects_bad_arguments_and_path_collisions(self):
        frame, state, out = self.paths()
        shutil.copy(FIXTURE_FRAME, frame)
        for quotas in ('not json', '[1]', '{"alpha": -1}', '{"alpha": 100001}'):
            r = run_cli(frame, quotas, SEED, state, out)
            self.assertEqual(r.returncode, 2, quotas)
            self.assertFalse(out.exists())
        for seed in ('', '   '):
            r = run_cli(frame, QUOTAS_JSON, seed, state, out)
            self.assertEqual(r.returncode, 2, repr(seed))
        r = run_cli(frame, QUOTAS_JSON, SEED, state, frame)
        self.assertEqual(r.returncode, 2)
        self.assertIn('frame and output must differ', r.stderr)
        r = run_cli(frame, QUOTAS_JSON, SEED, frame, out)
        self.assertEqual(r.returncode, 2)
        self.assertIn('frame and state must differ', r.stderr)
        r = run_cli(frame, QUOTAS_JSON, SEED, out, out)
        self.assertEqual(r.returncode, 2)
        self.assertIn('state and output must differ', r.stderr)


@unittest.skipUnless(os.environ.get('SURVEY_DRAW_BIGTEST'),
                     'ten-million-row memory test is gated by SURVEY_DRAW_BIGTEST=1')
class TenMillionRowMemoryTest(unittest.TestCase):
    """Stream a 10M-row frame; the draw must stay within 64 MiB peak RSS."""

    def test_ten_million_rows_within_memory_limit(self):
        quotas = {'st%d' % i: 1000 for i in range(10)}
        quotas_json = json.dumps(quotas)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frame = work / 'frame.jsonl'
            rng = random.Random(7)
            with open(frame, 'w', encoding='utf-8') as f:
                for i in range(10_000_000):
                    f.write(json.dumps({'unit_id': 'unit-%08d' % i,
                                        'stratum': 'st%d' % rng.randint(0, 9)},
                                       separators=(',', ':')) + '\n')
            wrapper = (
                'import subprocess, resource, sys;'
                'r = subprocess.run([sys.executable, "-m", "survey_estimate.draw",'
                ' "--frame", sys.argv[1], "--quotas", sys.argv[2], "--seed", "bench",'
                ' "--state", sys.argv[3], "--output", sys.argv[4]],'
                ' capture_output=True);'
                'sys.stderr.write(r.stderr.decode());'
                'print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss);'
                'sys.exit(r.returncode)')
            env = dict(os.environ)
            env['PYTHONPATH'] = str(ROOT / 'src')
            started = time.time()
            r = subprocess.run([sys.executable, '-c', wrapper, str(frame), quotas_json,
                                str(work / 'state.db'), str(work / 'out.json')],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            peak_kib = int(r.stdout.strip().splitlines()[-1])
            self.assertLessEqual(peak_kib, 64 * 1024,
                                 'peak RSS %.1f MiB exceeds 64 MiB' % (peak_kib / 1024))
            doc = json.loads((work / 'out.json').read_text(encoding='utf-8'))
            self.assertEqual(len(doc['sample']), 10_000)
            # Independently recompute the exact draw with a bounded-memory pass:
            # per stratum keep only the quota smallest (digest, unit_id) keys.
            import bisect
            tops = {sid: [] for sid in quotas}
            seed = 'bench'
            with open(frame, encoding='utf-8') as f:
                for line in f:
                    obj = json.loads(line)
                    uid, sid = obj['unit_id'], obj['stratum']
                    digest = hashlib.sha256(seed.encode() + b'\x00' + uid.encode()).digest()
                    bisect.insort(tops[sid], (digest, uid))
                    if len(tops[sid]) > quotas[sid]:
                        tops[sid].pop()
            expected = sorted((uid, sid) for sid, lst in tops.items() for _, uid in lst)
            self.assertEqual([(row['unit_id'], row['stratum']) for row in doc['sample']],
                             expected)
            print('\n10M rows: peak RSS %.1f MiB in %.0fs'
                  % (peak_kib / 1024, time.time() - started))


if __name__ == '__main__':
    unittest.main()
