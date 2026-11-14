"""Draw a stratified unit sample from a complete sampling frame.

The frame is a UTF-8 JSONL file with one JSON object per line carrying
exactly ``unit_id`` and ``stratum``, both under the shared text constraints
(nonempty, at most 256 characters, no control characters). ``--quotas`` is a
JSON object mapping stratum ids to non-negative integer quotas whose total is
at most 100000. Inside each stratum, units are ordered by
SHA256(UTF-8(seed + NUL + unit_id)) ascending with ties broken by unit_id,
and the first ``quota`` units are drawn. The output ``sample`` array carries
only these two fields per unit, sorted by unit_id, independent of frame
order.

The frame is streamed line by line: neither the whole frame nor all unit
ids are ever held in memory, so peak memory stays within 64 MiB even for
ten-million-row frames. Parsed units are inserted into the SQLite checkpoint
database and committed in batches; the unit_id primary key rejects duplicate
units and a per-stratum counter is kept alongside. A parse or constraint
failure rolls back the current batch while earlier committed batches stay
valid, so a killed run resumes after the last committed record without
re-parsing it. Before continuing, the committed prefix is verified against
per-batch SHA-256 digests recorded in the database (hashing raw bytes only,
never re-parsing). The checkpoint binds the whole-frame byte SHA-256, the
quotas and the seed; rerunning with changed inputs is rejected without
modifying the database. The output is written to a same-directory temporary
file and atomically renamed only after every check passed, so a failed run
leaves a previous output untouched and a rerun after a kill at any point
reproduces the identical file. An exclusive flock on the state database
forbids concurrent runs.
"""
import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .io import pairs, text

BATCH_ROWS = 10000          # frame rows committed to the state db per transaction
MAX_LINE_BYTES = 1 << 20    # defensive cap on one raw JSONL line
MAX_TOTAL_QUOTA = 100_000
CHUNK = 1 << 20             # read buffer for hashing / streaming

_SCHEMA = (
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE TABLE quotas(stratum TEXT PRIMARY KEY, quota INTEGER NOT NULL)',
    'CREATE TABLE batches(seq INTEGER PRIMARY KEY, offset INTEGER NOT NULL,'
    ' length INTEGER NOT NULL, sha256 TEXT NOT NULL)',
    'CREATE TABLE counts(stratum TEXT PRIMARY KEY, n INTEGER NOT NULL)',
    'CREATE TABLE units(unit_id TEXT PRIMARY KEY, stratum TEXT NOT NULL,'
    ' digest BLOB NOT NULL) WITHOUT ROWID',
    'CREATE TABLE sample(unit_id TEXT PRIMARY KEY, stratum TEXT NOT NULL) WITHOUT ROWID',
)
_META_KEYS = ('format_version', 'status', 'quotas_sha256', 'seed_sha256',
              'committed_offset', 'lines')
_STATUSES = ('running', 'validated', 'selected')


def _invalid_json(value):
    raise ValueError('non-finite JSON number: ' + value)


def _sha_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _check_quotas(quotas):
    if not isinstance(quotas, dict):
        raise ValueError('quotas must be a JSON object mapping stratum ids to quotas')
    for key, value in quotas.items():
        try:
            text(key)
        except ValueError as e:
            raise ValueError('quotas stratum %r: %s' % (key, e)) from None
        if type(value) is not int or value < 0:
            raise ValueError('quota must be a non-negative integer: ' + str(key))
    if sum(quotas.values()) > MAX_TOTAL_QUOTA:
        raise ValueError('total quota exceeds %d' % MAX_TOTAL_QUOTA)


def parse_quotas(raw):
    """Validate the --quotas JSON mapping into a {stratum: quota} dict."""
    try:
        data = json.loads(raw, object_pairs_hook=pairs, parse_constant=_invalid_json)
    except json.JSONDecodeError as e:
        raise ValueError('quotas must be a JSON object: %s' % e) from None
    _check_quotas(data)
    return data


def _canonical(quotas):
    return json.dumps(quotas, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _lock_and_connect(state):
    """Open the state database, taking an exclusive lock against other runs."""
    state = Path(state)
    if not state.parent.exists():
        state.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(state, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ValueError('state database is locked by another survey-draw process') from None
    db = None
    try:
        db = sqlite3.connect(os.fspath(state), timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        db.execute('PRAGMA cache_size=-2048')
        db.execute('PRAGMA temp_store=FILE')
    except BaseException:
        if db is not None:
            db.close()
        os.close(fd)
        raise
    return fd, db


def _meta(db):
    try:
        return dict(db.execute('SELECT key, value FROM meta').fetchall())
    except sqlite3.Error:
        raise ValueError('state database is not a survey-draw checkpoint') from None


def _is_empty(db):
    try:
        return db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] == 0
    except sqlite3.Error:
        raise ValueError('state database is not a survey-draw checkpoint') from None


def _init(db, quotas, seed):
    """Create the schema and bind quotas and seed in one transaction."""
    with db:
        for statement in _SCHEMA:
            db.execute(statement)
        db.executemany('INSERT INTO meta(key, value) VALUES(?, ?)', [
            ('format_version', '1'),
            ('status', 'running'),
            ('quotas_sha256', _sha_text(_canonical(quotas))),
            ('seed_sha256', _sha_text(seed)),
            ('committed_offset', '0'),
            ('lines', '0'),
        ])
        db.executemany('INSERT INTO quotas(stratum, quota) VALUES(?, ?)', quotas.items())


def _check_bindings(meta, quotas, seed):
    if meta.get('format_version') != '1' or any(k not in meta for k in _META_KEYS):
        raise ValueError('state database is not a survey-draw checkpoint')
    if meta['status'] not in _STATUSES:
        raise ValueError('state database has unknown status: ' + meta['status'])
    if meta['quotas_sha256'] != _sha_text(_canonical(quotas)):
        raise ValueError('quotas differ from the state checkpoint')
    if meta['seed_sha256'] != _sha_text(seed):
        raise ValueError('seed differs from the state checkpoint')


def _parse_line(line, lineno):
    """Parse one raw JSONL line into (unit_id, stratum); None when blank."""
    if not line.strip():
        return None
    try:
        content = line.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('line %d: not valid UTF-8' % lineno) from None
    try:
        obj = json.loads(content, object_pairs_hook=pairs, parse_constant=_invalid_json)
    except ValueError as e:
        raise ValueError('line %d: invalid JSON: %s' % (lineno, e)) from None
    if not isinstance(obj, dict) or set(obj) != {'unit_id', 'stratum'}:
        raise ValueError("line %d: expected an object with only 'unit_id' and 'stratum'" % lineno)
    try:
        uid = text(obj['unit_id'])
        sid = text(obj['stratum'])
    except ValueError as e:
        raise ValueError('line %d: %s' % (lineno, e)) from None
    return uid, sid


def _hash_exact(f, length, hasher):
    """Read exactly length bytes, feed the rolling hasher, return their SHA-256."""
    h = hashlib.sha256()
    remaining = length
    while remaining > 0:
        chunk = f.read(min(CHUNK, remaining))
        if not chunk:
            raise ValueError('frame changed: file is shorter than the committed checkpoint')
        h.update(chunk)
        hasher.update(chunk)
        remaining -= len(chunk)
    return h.hexdigest()


def _find_duplicate(db, batch):
    """Locate the duplicate unit_id that made a batch insert fail."""
    seen = set()
    for uid, _, _ in batch:
        if uid in seen:
            return uid
        seen.add(uid)
    for uid, _, _ in batch:
        if db.execute('SELECT 1 FROM units WHERE unit_id = ?', (uid,)).fetchone():
            return uid
    return None


def _commit_batch(db, batch, batch_counts, seq, offset, length, sha, lines):
    try:
        with db:
            db.executemany('INSERT INTO units(unit_id, stratum, digest) VALUES(?, ?, ?)', batch)
            db.executemany('INSERT INTO counts(stratum, n) VALUES(?, ?)'
                           ' ON CONFLICT(stratum) DO UPDATE SET n = n + excluded.n',
                           list(batch_counts.items()))
            db.execute('INSERT INTO batches(seq, offset, length, sha256) VALUES(?, ?, ?, ?)',
                       (seq, offset, length, sha))
            db.execute("UPDATE meta SET value = ? WHERE key = 'committed_offset'",
                       (str(offset + length),))
            db.execute("UPDATE meta SET value = ? WHERE key = 'lines'", (str(lines),))
    except sqlite3.IntegrityError:
        duplicate = _find_duplicate(db, batch)
        raise ValueError('duplicate unit_id in frame: ' + str(duplicate)) from None


def _pass1(db, frame, quotas, seed):
    """Stream the frame, verify committed batches, insert new ones, checkpoint."""
    meta = _meta(db)
    committed = int(meta['committed_offset'])
    lines = int(meta['lines'])
    ledger = db.execute('SELECT seq, offset, length, sha256 FROM batches ORDER BY seq').fetchall()
    expect = 0
    for _, offset, length, _ in ledger:
        if offset != expect:
            raise ValueError('state database batch ledger is corrupt')
        expect = offset + length
    if expect != committed:
        raise ValueError('state database batch ledger is corrupt')
    hasher = hashlib.sha256()
    base = hashlib.sha256(seed.encode('utf-8') + b'\x00')
    with open(frame, 'rb', buffering=CHUNK) as f:
        # Verify the committed prefix by hashing raw bytes only; never re-parse it.
        for _, offset, length, sha in ledger:
            if _hash_exact(f, length, hasher) != sha:
                raise ValueError('frame changed: bytes before the committed checkpoint differ')
        seq = (ledger[-1][0] + 1) if ledger else 1
        batch, batch_counts = [], {}
        batch_hasher = hashlib.sha256()
        batch_offset, batch_length = committed, 0
        while True:
            line = f.readline(MAX_LINE_BYTES + 2)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES + 1:
                raise ValueError('line %d: line exceeds %d bytes' % (lines + 1, MAX_LINE_BYTES))
            lines += 1
            hasher.update(line)
            batch_hasher.update(line)
            batch_length += len(line)
            parsed = _parse_line(line, lines)
            if parsed is not None:
                uid, sid = parsed
                if sid not in quotas:
                    raise ValueError('line %d: unknown stratum: %s' % (lines, sid))
                digest = base.copy()
                digest.update(uid.encode('utf-8'))
                batch.append((uid, sid, digest.digest()))
                batch_counts[sid] = batch_counts.get(sid, 0) + 1
                if len(batch) >= BATCH_ROWS:
                    _commit_batch(db, batch, batch_counts, seq, batch_offset,
                                  batch_length, batch_hasher.hexdigest(), lines)
                    seq += 1
                    batch_offset += batch_length
                    batch, batch_counts = [], {}
                    batch_hasher = hashlib.sha256()
                    batch_length = 0
        if batch or batch_length:
            _commit_batch(db, batch, batch_counts, seq, batch_offset,
                          batch_length, batch_hasher.hexdigest(), lines)
        with db:
            db.execute("REPLACE INTO meta(key, value) VALUES('frame_sha256', ?)",
                       (hasher.hexdigest(),))
            db.execute("UPDATE meta SET value = 'validated' WHERE key = 'status'")


def _verify_frame_hash(db, frame):
    expected = _meta(db).get('frame_sha256')
    if expected is None:
        raise ValueError('state database is missing the committed frame digest')
    hasher = hashlib.sha256()
    with open(frame, 'rb') as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise ValueError('frame changed: content differs from the state checkpoint')


def _validate_counts(db):
    rows = db.execute(
        'SELECT quotas.stratum, quotas.quota, COALESCE(counts.n, 0)'
        ' FROM quotas LEFT JOIN counts ON counts.stratum = quotas.stratum'
        ' WHERE COALESCE(counts.n, 0) < quotas.quota'
        ' ORDER BY quotas.stratum').fetchall()
    if rows:
        detail = ', '.join('%s (%d < %d)' % (sid, have, need) for sid, need, have in rows[:5])
        raise ValueError('stratum population below quota: ' + detail
                         + (' ...' if len(rows) > 5 else ''))


def _select(db):
    """Materialize the per-stratum hash-ordered draws into the sample table."""
    with db:
        db.execute('DELETE FROM sample')
        db.execute(
            'INSERT INTO sample(unit_id, stratum)'
            ' SELECT ranked.unit_id, ranked.stratum FROM ('
            '   SELECT unit_id, stratum,'
            '     ROW_NUMBER() OVER (PARTITION BY stratum ORDER BY digest, unit_id) AS rn'
            '   FROM units) ranked'
            ' JOIN quotas ON quotas.stratum = ranked.stratum'
            ' WHERE ranked.rn <= quotas.quota')
        db.execute("UPDATE meta SET value = 'selected' WHERE key = 'status'")


def process(frame, quotas, seed, state):
    """Run the checkpointed pipeline up to selection; return (lock fd, db)."""
    frame = Path(frame)
    if not frame.is_file():
        raise ValueError('frame must be a regular file: ' + str(frame))
    try:
        seed = text(seed)
    except ValueError as e:
        raise ValueError('seed: %s' % e) from None
    _check_quotas(quotas)
    lock_fd, db = _lock_and_connect(state)
    try:
        if _is_empty(db):
            _init(db, quotas, seed)
        meta = _meta(db)
        _check_bindings(meta, quotas, seed)
        status = meta['status']
        if status == 'running':
            _pass1(db, frame, quotas, seed)
            status = 'validated'
        else:
            _verify_frame_hash(db, frame)
        if status == 'validated':
            _validate_counts(db)
            _select(db)
        return lock_fd, db
    except BaseException:
        db.close()
        os.close(lock_fd)
        raise


def iter_sample(db):
    """Yield (unit_id, stratum) of the drawn sample sorted by unit_id."""
    cursor = db.execute('SELECT unit_id, stratum FROM sample ORDER BY unit_id')
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        for uid, sid in rows:
            yield uid, sid


def _write_json(fileobj, rows):
    """Stream the sample document, byte-identical to json.dump(indent=2)."""
    write = fileobj.write
    write('{\n  "sample": [')
    first = True
    for uid, sid in rows:
        write('\n' if first else ',\n')
        first = False
        write('    {\n      "stratum": ')
        write(json.dumps(sid, ensure_ascii=False))
        write(',\n      "unit_id": ')
        write(json.dumps(uid, ensure_ascii=False))
        write('\n    }')
    write(']\n}\n' if first else '\n  ]\n}\n')


def publish(output, rows):
    """Atomically replace output with the streamed sample document."""
    output = Path(output)
    if output.is_symlink():
        raise ValueError('output symlink is not allowed')
    output.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=output.parent, delete=False) as f:
            name = f.name
            _write_json(f, rows)
        os.replace(name, output)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _print(db):
    out = sys.stdout
    out.write('{"sample": [')
    first = True
    for uid, sid in iter_sample(db):
        out.write('' if first else ', ')
        first = False
        out.write('{"stratum": %s, "unit_id": %s}'
                  % (json.dumps(sid, ensure_ascii=False), json.dumps(uid, ensure_ascii=False)))
    out.write(']}\n')


def main():
    parser = argparse.ArgumentParser(
        description='Draw a stratified unit sample from a complete sampling frame')
    parser.add_argument('--frame', type=Path, required=True)
    parser.add_argument('--quotas', required=True)
    parser.add_argument('--seed', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        try:
            seed = text(args.seed)
        except ValueError as e:
            raise ValueError('seed: %s' % e) from None
        quotas = parse_quotas(args.quotas)
        if args.frame.resolve() == args.output.resolve():
            raise ValueError('frame and output must differ')
        if args.frame.resolve() == args.state.resolve():
            raise ValueError('frame and state must differ')
        if args.state.resolve() == args.output.resolve():
            raise ValueError('state and output must differ')
        lock_fd, db = process(args.frame, quotas, seed, args.state)
        try:
            publish(args.output, iter_sample(db))
            _print(db)
        finally:
            db.close()
            os.close(lock_fd)
    except (ValueError, OSError, KeyError, TypeError, OverflowError,
            RecursionError, sqlite3.Error) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
