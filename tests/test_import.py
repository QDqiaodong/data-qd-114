import hashlib, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from survey_estimate.survey_import import build
from survey_estimate.design import parse

ROOT = Path(__file__).resolve().parents[1]
HEADER = 'unit_id,revision,op,stratum,value\n'


def write_csv(directory, name, rows, header=HEADER, raw=None, newline='\n'):
    path = Path(directory) / name
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(header + ''.join(row + newline for row in rows), encoding='utf-8')
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(directory, files, strata=None):
    payload = {'strata': strata or [{'id': 'h', 'population': 8}, {'id': 'g', 'population': 5}],
               'files': [{'path': name, 'sha256': digest(Path(directory) / name)} for name in files]}
    path = Path(directory) / 'manifest.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def cli(out, manifest_path, cwd=None):
    return subprocess.run([sys.executable, '-m', 'survey_estimate.survey_import',
                           '--manifest', str(manifest_path), '--output', str(out)],
                          capture_output=True, cwd=cwd)


class ImportTests(unittest.TestCase):
    def test_merge_orders_output_and_replays_identical_rows(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['v2,1,upsert,g,20', 'u1,1,upsert,h,2', 'u1,1,upsert,h,2', 'u4,1,upsert,g,7'])
            write_csv(td, 'b.csv', ['u1,3,upsert,h,9', 'v2,3,delete,,', 'u3,1,delete,,', 'v1,2,upsert,h,6'])
            result = build(manifest(td, ['b.csv', 'a.csv']))
            self.assertEqual(result['strata'], [{'id': 'g', 'population': 5}, {'id': 'h', 'population': 8}])
            self.assertEqual(result['sample'], [
                {'unit_id': 'u1', 'stratum': 'h', 'value': '9'},
                {'unit_id': 'u4', 'stratum': 'g', 'value': '7'},
                {'unit_id': 'v1', 'stratum': 'h', 'value': '6'}])
            parse(result)

    def test_high_version_delete_removes_unit_and_late_low_version_upsert_does_not_resurrect(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1', 'u1,4,delete,,', 'u5,1,upsert,h,5'])
            write_csv(td, 'b.csv', ['u1,2,upsert,h,3', 'u2,2,upsert,g,4'])
            result = build(manifest(td, ['a.csv', 'b.csv']))
            self.assertEqual(result['sample'], [{'unit_id': 'u2', 'stratum': 'g', 'value': '4'},
                                                {'unit_id': 'u5', 'stratum': 'h', 'value': '5'}])

    def test_delete_appearing_before_upserts_is_replayed_by_revision(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'delete_first.csv', ['u1,2,delete,,', 'u2,2,delete,,' ])
            write_csv(td, 'upsert_later.csv', ['u1,1,upsert,h,1', 'u2,5,upsert,g,5', 'u3,1,upsert,h,3'])
            result = build(manifest(td, ['delete_first.csv', 'upsert_later.csv']))
            self.assertEqual(result['sample'], [{'unit_id': 'u2', 'stratum': 'g', 'value': '5'},
                                                {'unit_id': 'u3', 'stratum': 'h', 'value': '3'}])

    def test_leading_zero_revisions_are_the_same_version(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,007,upsert,h,1'])
            write_csv(td, 'b.csv', ['u1,7,upsert,h,1', 'u2,1,upsert,h,2'])
            self.assertEqual(build(manifest(td, ['a.csv', 'b.csv'], [{'id': 'h', 'population': 8}]))['sample'],
                             [{'unit_id': 'u1', 'stratum': 'h', 'value': '1'},
                              {'unit_id': 'u2', 'stratum': 'h', 'value': '2'}])
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,007,upsert,h,1'])
            write_csv(td, 'b.csv', ['u1,7,upsert,h,2'])
            with self.assertRaises(ValueError) as ctx:
                build(manifest(td, ['a.csv', 'b.csv'], [{'id': 'h', 'population': 8}]))
            self.assertIn('revision 7', str(ctx.exception))

    def test_cross_file_conflict_at_same_revision_fails(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,2,upsert,h,1'])
            write_csv(td, 'b.csv', ['u1,2,delete,,' ])
            with self.assertRaises(ValueError) as ctx:
                build(manifest(td, ['a.csv', 'b.csv']))
            self.assertIn('u1', str(ctx.exception))
            self.assertIn('revision 2', str(ctx.exception))
            self.assertIn('a.csv', str(ctx.exception))
            self.assertIn('b.csv', str(ctx.exception))

    def test_bad_data_at_low_revision_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,NaN', 'u1,9,upsert,h,1'])
            with self.assertRaises(ValueError) as ctx:
                build(manifest(td, ['a.csv']))
            self.assertIn('a.csv', str(ctx.exception))
            self.assertIn('record 1', str(ctx.exception))

    def test_revision_is_positive_integer_and_compared_numerically(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,10,upsert,h,10', 'u1,9,upsert,h,9'])
            result = build(manifest(td, ['a.csv'], [{'id': 'h', 'population': 8}]))
            self.assertEqual(result['sample'], [{'unit_id': 'u1', 'stratum': 'h', 'value': '10'}])
        for bad in ['0', '-1', 'x', ' 1', '1.0', '01x']:
            with tempfile.TemporaryDirectory() as td:
                write_csv(td, 'a.csv', [f'u1,{bad},upsert,h,1'])
                with self.assertRaises(ValueError) as ctx:
                    build(manifest(td, ['a.csv']))
                self.assertIn('record 1', str(ctx.exception))

    def test_delete_requires_empty_stratum_and_value(self):
        for row in ['u1,1,delete,h,', 'u1,1,delete,,1', 'u1,1,delete,h,1']:
            with tempfile.TemporaryDirectory() as td:
                write_csv(td, 'a.csv', [row])
                with self.assertRaises(ValueError) as ctx:
                    build(manifest(td, ['a.csv']))
                self.assertIn('delete requires empty stratum and value', str(ctx.exception))

    def test_header_shape_and_column_count_are_strict(self):
        cases = {'bad_header.csv': ('unit_id,revision,op,value,stratum\n', []),
                 'dup_header.csv': (HEADER, [HEADER.strip()]),
                 'short_row.csv': (HEADER, ['u1,1,upsert,h']),
                 'long_row.csv': (HEADER, ['u1,1,upsert,h,1,extra']),
                 'empty.csv': ('', []),
                 'bom.csv': ('﻿' + HEADER, []),
                 'utf16.csv': (HEADER, [])}
        for name, (header, rows) in cases.items():
            with tempfile.TemporaryDirectory() as td:
                if name == 'utf16.csv':
                    path = Path(td) / name
                    path.write_bytes((HEADER + 'u1,1,upsert,h,1\n').encode('utf-16'))
                else:
                    write_csv(td, name, rows, header=header)
                with self.assertRaises(ValueError) as ctx:
                    build(manifest(td, [name]))
                self.assertIn(name, str(ctx.exception))

    def test_crlf_quoted_fields_and_blank_lines_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1'], newline='\r\n')
            result = build(manifest(td, ['a.csv'], [{'id': 'h', 'population': 8}]))
            self.assertEqual(result['sample'], [{'unit_id': 'u1', 'stratum': 'h', 'value': '1'}])
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['"u1",1,upsert,"h","1"'])
            result = build(manifest(td, ['a.csv'], [{'id': 'h', 'population': 8}]))
            self.assertEqual(result['sample'], [{'unit_id': 'u1', 'stratum': 'h', 'value': '1'}])
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1', '', 'u2,1,upsert,h,2'])
            result = build(manifest(td, ['a.csv'], [{'id': 'h', 'population': 8}]))
            self.assertEqual([r['unit_id'] for r in result['sample']], ['u1', 'u2'])

    def test_hash_mismatch_modified_file_and_missing_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1'])
            m = manifest(td, ['a.csv'])
            write_csv(td, 'a.csv', ['u1,1,upsert,h,2'])
            with self.assertRaises(ValueError) as ctx:
                build(m)
            self.assertIn('sha256 mismatch', str(ctx.exception))
            self.assertIn('a.csv', str(ctx.exception))
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1'])
            m = manifest(td, ['a.csv'])
            (Path(td) / 'a.csv').unlink()
            with self.assertRaises(ValueError):
                build(m)

    def test_paths_outside_manifest_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            outside = Path(td) / 'outside'; outside.mkdir()
            write_csv(outside, 'evil.csv', ['u1,1,upsert,h,1'])
            inner = Path(td) / 'inner'; inner.mkdir()
            for rel in ('../outside/evil.csv', str(outside / 'evil.csv')):
                m = inner / 'manifest.json'
                m.write_text(json.dumps({'strata': [{'id': 'h', 'population': 8}],
                                         'files': [{'path': rel, 'sha256': digest(outside / 'evil.csv')}]}))
                with self.assertRaises(ValueError) as ctx:
                    build(m)
                self.assertIn('manifest directory', str(ctx.exception))
            sub = inner / 'sub'; sub.mkdir()
            write_csv(sub, 'a.csv', ['u1,1,upsert,h,1'])
            m = inner / 'manifest.json'
            m.write_text(json.dumps({'strata': [{'id': 'h', 'population': 8}],
                                     'files': [{'path': '../sub/a.csv', 'sha256': digest(sub / 'a.csv')}]}))
            with self.assertRaises(ValueError):
                build(m)

    def test_directory_and_symlink_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'dir').mkdir()
            m = Path(td) / 'manifest.json'
            m.write_text(json.dumps({'strata': [{'id': 'h', 'population': 8}],
                                     'files': [{'path': 'dir', 'sha256': '0' * 64}]}))
            with self.assertRaises(ValueError) as ctx:
                build(m)
            self.assertIn('regular file', str(ctx.exception))
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'real.csv', ['u1,1,upsert,h,1'])
            (Path(td) / 'link.csv').symlink_to(Path(td) / 'real.csv')
            m = Path(td) / 'manifest.json'
            m.write_text(json.dumps({'strata': [{'id': 'h', 'population': 8}],
                                     'files': [{'path': 'link.csv', 'sha256': digest(Path(td) / 'real.csv')}]}))
            with self.assertRaises(ValueError):
                build(m)

    def test_unknown_stratum_and_extra_or_duplicate_manifest_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,nowhere,1'])
            with self.assertRaises(ValueError) as ctx:
                build(manifest(td, ['a.csv']))
            self.assertIn('unknown stratum', str(ctx.exception))
        for payload in ['{"strata": [], "files": [], "extra": 1}',
                        '{"strata": [{"id": "h", "population": 8, "note": "x"}], "files": []}',
                        '{"strata": [{"id": "h", "population": 8}, {"id": "h", "population": 8}], "files": []}',
                        '{"strata": [{"id": "h", "population": 8}], "files": [{"path": "a.csv"}]}',
                        '{"strata": [], "strata": [], "files": []}']:
            with tempfile.TemporaryDirectory() as td:
                m = Path(td) / 'manifest.json'
                m.write_text(payload)
                with self.assertRaises(ValueError):
                    build(m)

    def test_result_must_satisfy_existing_source_validation(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,delete,,' ])
            with self.assertRaises(ValueError) as ctx:
                build(manifest(td, ['a.csv']))
            self.assertIn('stratum must have 1..population', str(ctx.exception))
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,g,1', 'u1,1,upsert,h,1'])
            with self.assertRaises(ValueError):
                build(manifest(td, ['a.csv']))

    def test_order_of_files_and_rows_does_not_change_result(self):
        with tempfile.TemporaryDirectory() as td:
            a = write_csv(td, 'a.csv', ['u1,1,upsert,h,1', 'u2,1,upsert,g,2'])
            b = write_csv(td, 'b.csv', ['u3,1,upsert,h,3', 'u2,2,delete,,', 'u4,2,upsert,g,4'])
            first = build(manifest(td, ['a.csv', 'b.csv']))
            second = build(manifest(td, ['b.csv', 'a.csv']))
            self.assertEqual(first, second)
            with tempfile.TemporaryDirectory() as td2:
                write_csv(td2, 'x.csv', ['u2,2,delete,,', 'u4,2,upsert,g,4'])
                write_csv(td2, 'y.csv', ['u3,1,upsert,h,3', 'u1,1,upsert,h,1', 'u2,1,upsert,g,2'])
                third = build(manifest(td2, ['y.csv', 'x.csv']))
            self.assertEqual(first, third)

    def test_cli_publishes_and_failure_leaves_output_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1', 'u2,1,upsert,g,2'])
            good = manifest(td, ['a.csv'])
            out = Path(td) / 'out.json'
            r = cli(out, good)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_bytes()), build(good))
            before = out.read_bytes()
            write_csv(td, 'a.csv', ['u1,1,upsert,h,999'])
            r = cli(out, good)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'a.csv', r.stderr)
            self.assertIn(b' sha256 mismatch', r.stderr)
            self.assertEqual(out.read_bytes(), before)
            bad = Path(td) / 'bad.json'
            bad.write_text('{}')
            self.assertEqual(cli(out, bad).returncode, 2)
            self.assertEqual(out.read_bytes(), before)
            missing = Path(td) / 'missing.json'
            r = cli(out, missing)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'missing.json', r.stderr)
            self.assertEqual(out.read_bytes(), before)

    def test_cli_refuses_output_equal_to_manifest_or_input(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,1'])
            m = manifest(td, ['a.csv'])
            r = cli(m, m)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'manifest and output must differ', r.stderr)
            r = cli(Path(td) / 'a.csv', m)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'output must differ from input file: a.csv', r.stderr)
            self.assertEqual((Path(td) / 'a.csv').read_text(), HEADER + 'u1,1,upsert,h,1\n')

    def test_failed_cli_leaves_no_temporary_output(self):
        with tempfile.TemporaryDirectory() as td:
            write_csv(td, 'a.csv', ['u1,1,upsert,h,oops'])
            m = manifest(td, ['a.csv'])
            out = Path(td) / 'out.json'
            self.assertEqual(cli(out, m).returncode, 2)
            self.assertFalse(out.exists())
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ['a.csv', 'manifest.json'])

    def test_fixture_reproduces_survey_sample(self):
        result = build(ROOT / 'fixtures/revisions/manifest.json')
        expected = json.loads((ROOT / 'fixtures/survey.json').read_text())
        self.assertEqual(result['strata'], sorted(expected['strata'], key=lambda s: s['id']))
        self.assertEqual(result['sample'], sorted(expected['sample'], key=lambda r: r['unit_id']))


if __name__ == '__main__':
    unittest.main()
