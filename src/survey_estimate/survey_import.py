"""Merge questionnaire revision CSVs into a validated survey sample JSON.

The manifest lists the original strata and the revision files. Every CSV row is
validated, replayed at the same (unit, revision) only when its normalized fields
agree, and the highest revision per unit wins. A winning ``delete`` drops the
unit. The result contains only ``strata`` and ``sample`` and must pass the same
validation as ``--source`` input before it is published atomically.
"""
import argparse, csv, hashlib, io, json, sys
from pathlib import Path
from .design import outcome, parse
from .io import array, fields, integer, load, publish, text

HEADER = ('unit_id', 'revision', 'op', 'stratum', 'value')
OPS = ('upsert', 'delete')
MAX_BYTES = 8_000_000


def _fail(display, number, line, message):
    raise ValueError(f'{display}: record {number} (line {line}): {message}')


def _revision(raw):
    """Canonical positive-integer text, ordered by (digit length, digits)."""
    if not raw.isascii() or not raw.isdigit():
        raise ValueError('revision must be a positive integer')
    canonical = raw.lstrip('0')
    if not canonical:
        raise ValueError('revision must be a positive integer')
    return canonical


def _newer(left, right):
    """True when canonical revision left is numerically higher than right."""
    return (len(left), left) > (len(right), right)


def _regular_file(base, rel):
    """Resolve a manifest-relative path, requiring a regular file under base."""
    p = Path(rel)
    if p.is_absolute():
        raise ValueError('path must be relative to the manifest directory')
    if '..' in p.parts:
        raise ValueError('path must not escape the manifest directory')
    candidate = base / p
    if candidate.is_symlink():
        raise ValueError('path must be a regular file inside the manifest directory')
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ValueError('file not found') from None
    if not resolved.is_relative_to(base):
        raise ValueError('path resolves outside the manifest directory')
    if not resolved.is_file():
        raise ValueError('path must be a regular file inside the manifest directory')
    return resolved


def _rows(display, raw):
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError(f'{display}: file is not valid UTF-8') from None
    if content.startswith('\ufeff'):
        raise ValueError(f'{display}: UTF-8 byte order mark is not allowed')
    reader = csv.reader(io.StringIO(content, newline=''), strict=True)
    try:
        header = next(reader, None)
    except csv.Error as e:
        raise ValueError(f'{display}: invalid CSV: {e}') from None
    if header is None:
        raise ValueError(f'{display}: missing header')
    if tuple(header) != HEADER:
        raise ValueError(f'{display}: header must be ' + ','.join(HEADER))
    number = 0
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            raise ValueError(f'{display}: record {number + 1} (line {reader.line_num}): invalid CSV: {e}') from None
        line = reader.line_num
        if not row:
            continue
        if tuple(row) == HEADER:
            raise ValueError(f'{display}: record {number + 1} (line {line}): duplicate header')
        number += 1
        if len(row) != len(HEADER):
            _fail(display, number, line, f'expected {len(HEADER)} columns, got {len(row)}')
        yield number, line, row


def _collect(display, raw, strata, seen):
    for number, line, row in _rows(display, raw):
        try:
            uid = text(row[0])
        except ValueError as e:
            _fail(display, number, line, str(e))
        try:
            revision = _revision(row[1])
        except ValueError as e:
            _fail(display, number, line, str(e))
        op = row[2]
        if op not in OPS:
            _fail(display, number, line, 'op must be upsert or delete')
        if op == 'delete':
            if row[3] != '' or row[4] != '':
                _fail(display, number, line, 'delete requires empty stratum and value')
            identity = (op, None, None)
        else:
            try:
                stratum = text(row[3])
            except ValueError as e:
                _fail(display, number, line, str(e))
            if stratum not in strata:
                _fail(display, number, line, 'unknown stratum: ' + stratum)
            try:
                outcome(row[4])
            except ValueError as e:
                _fail(display, number, line, str(e))
            identity = (op, stratum, row[4])
        key = (uid, revision)
        origin = f'{display} record {number} (line {line})'
        if key in seen:
            previous, previous_origin = seen[key]
            if previous != identity:
                raise ValueError(f'conflicting records for unit {uid} at revision {revision}: '
                                 f'{previous_origin} vs {origin}')
        else:
            seen[key] = (identity, origin)


def load_manifest(path):
    """Return (strata id->population, [(display path, resolved path, sha256)])."""
    path = Path(path)
    try:
        data = load(path)
        fields(data, 'strata files')
        strata = {}
        for s in array(data['strata']):
            fields(s, 'id population')
            sid = text(s['id'])
            if sid in strata:
                raise ValueError('duplicate stratum: ' + sid)
            strata[sid] = integer(s['population'], 1, 10**9)
    except ValueError as e:
        raise ValueError(f'{path}: {e}') from None
    base = path.resolve().parent
    files = []
    for i, item in enumerate(array(data['files'])):
        label = f'files[{i}]'
        rel = None
        try:
            fields(item, 'path sha256')
            rel = text(item['path'])
            label = f'files[{i}] ({rel})'
            digest = item['sha256']
            if not isinstance(digest, str) or len(digest) != 64 \
                    or any(c not in '0123456789abcdefABCDEF' for c in digest):
                raise ValueError('sha256 must be 64 hexadecimal characters')
            resolved = _regular_file(base, rel)
        except ValueError as e:
            raise ValueError(f'{path}: {label}: {e}') from None
        files.append((rel, resolved, digest.lower()))
    return strata, files


def build(manifest_path, output=None):
    """Build the merged sample document, validating before publication."""
    manifest_path = Path(manifest_path)
    strata, files = load_manifest(manifest_path)
    out = None if output is None else Path(output).resolve()
    if out is not None and manifest_path.resolve() == out:
        raise ValueError('manifest and output must differ')
    if out is not None and Path(output).is_symlink():
        raise ValueError('output symlink is not allowed')
    if out is not None:
        for rel, path, _ in files:
            if path == out:
                raise ValueError('output must differ from input file: ' + rel)
    seen = {}
    for rel, path, digest in files:
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError(f'{rel}: file exceeds {MAX_BYTES} bytes')
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise ValueError(f'{rel}: sha256 mismatch (expected {digest}, got {actual})')
        _collect(rel, raw, strata, seen)
    best = {}
    for (uid, revision), (identity, _) in seen.items():
        if uid not in best or _newer(revision, best[uid][0]):
            best[uid] = (revision, identity)
    sample = []
    for uid in sorted(best):
        revision, (op, stratum, value) = best[uid]
        if op == 'upsert':
            sample.append({'unit_id': uid, 'stratum': stratum, 'value': value})
    result = {
        'strata': [{'id': sid, 'population': strata[sid]} for sid in sorted(strata)],
        'sample': sample,
    }
    try:
        parse(result)
    except ValueError as e:
        raise ValueError(f'merged output: {e}') from None
    return result


def main():
    parser = argparse.ArgumentParser(description='Merge questionnaire revision CSVs into a survey sample JSON')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.manifest, args.output)
        publish(args.output, result)
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
