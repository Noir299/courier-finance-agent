"""Local storage for document archives, task versions, runs, and business audit."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


MAX_DOCUMENT_BYTES = 30 * 1024 * 1024
DEFAULT_TASK_ID = 'daily'
ARCHIVE_TYPES = ('客户账单', '发件成本', '总部账单', '罚款单', '理赔结算单', '客诉证据', '客户回款凭证', '经营情况汇总', '制度与口径', '其他')
TYPE_WORDS = {
    '客户账单': ('账单', '月结单', '对账单'),
    '发件成本': ('发件成本', '成本明细'),
    '总部账单': ('总部账单', '总部月结单', '总部对账单'),
    '罚款单': ('罚款', '处罚单'),
    '理赔结算单': ('理赔结算', '理赔单'),
    '客诉证据': ('客诉', '投诉证据'),
    '客户回款凭证': ('回款', '收款凭证'),
    '经营情况汇总': ('经营情况汇总', '经营日报', '经营情况日报'),
    '制度与口径': ('制度', '口径说明', '费用办法', '结算规则'),
}
ALLOWED_EXTENSIONS = frozenset(('.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff',
                                '.xls', '.xlsx', '.csv', '.zip', '.docx', '.txt', '.md'))
PREVIEW_MIME = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
CHANNELS = frozenset(('站内', '企业微信', '邮件'))
MONTH_PATTERN = re.compile(r'(?<!\d)(20\d{2})\s*(?:[-_./年]\s*)?(0?[1-9]|1[0-2])(?:\s*月)?(?!\d)')


class OperationError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    customer TEXT NOT NULL DEFAULT '',
    month TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('ready','pending')),
    source TEXT NOT NULL CHECK(source IN ('上传','原始附件')),
    auto_identified INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (name_key, size)
);
CREATE INDEX IF NOT EXISTS documents_filter ON documents(status, customer, month, kind);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    ip TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    details TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_time ON audit(at);
CREATE INDEX IF NOT EXISTS audit_action ON audit(action, at);
CREATE TRIGGER IF NOT EXISTS audit_keep_history_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_keep_history_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    report_kind TEXT NOT NULL CHECK(report_kind IN ('daily','query')),
    query TEXT NOT NULL DEFAULT '',
    time TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL,
    paused_until TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    recipients TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_versions (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    version INTEGER NOT NULL,
    config TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    PRIMARY KEY(task_id, version)
);
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    version INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    scheduled_for TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    artifact TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(task_id, version) REFERENCES task_versions(task_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS run_one_active ON task_runs(task_id) WHERE status = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS run_unique_attempt ON task_runs(task_id, scheduled_for, attempt)
WHERE scheduled_for <> '';
CREATE INDEX IF NOT EXISTS runs_history ON task_runs(task_id, started_at DESC);
"""


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def calendar_date(value, field='日期'):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise OperationError(f'{field}须使用 YYYY-MM-DD 格式。')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise OperationError(f'{field}无效。') from error


def clean_text(value, field, maximum, *, required=False):
    if not isinstance(value, str):
        raise OperationError(f'{field}须为文本。')
    value = unicodedata.normalize('NFC', value.strip())
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise OperationError(f'{field}太长或含有无效字符。')
    if required and not value:
        raise OperationError(f'请填写{field}。')
    return value


def document_metadata(customer, month, kind, *, required=False):
    customer = clean_text(customer, '客户', 120, required=required)
    month = clean_text(month, '月份', 7, required=required)
    kind = clean_text(kind, '单据类型', 30, required=required)
    if month and not re.fullmatch(r'20\d{2}-(?:0[1-9]|1[0-2])', month):
        raise OperationError('月份须使用 YYYY-MM 格式。')
    if kind and kind not in ARCHIVE_TYPES:
        raise OperationError('单据类型不在可选范围内。')
    return customer, month, kind


def bounded_int(value, field, minimum, maximum):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value):
        number = int(value)
    else:
        raise OperationError(f'{field}须为整数。')
    if not minimum <= number <= maximum:
        raise OperationError(f'{field}超出范围。')
    return number


def true_false(value, field):
    if type(value) is not bool:
        raise OperationError(f'{field}须为布尔值。')
    return value


def json_value(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def document_dict(row):
    item = dict(row)
    del item['name_key'], item['relative_path']
    item['missing'] = [key for key in ('customer', 'month', 'kind') if not item[key]]
    item['previewable'] = Path(item['name']).suffix.lower() in PREVIEW_MIME
    item['auto_identified'] = bool(item['auto_identified'])
    return item


def preview_mime(path):
    mime = PREVIEW_MIME.get(path.suffix.lower())
    if mime is None:
        return None
    with path.open('rb') as stream:
        header = stream.read(16)
    signatures = {'application/pdf': b'%PDF-', 'image/png': b'\x89PNG\r\n\x1a\n', 'image/jpeg': b'\xff\xd8\xff',
                  'image/gif': b'GIF8', 'image/bmp': b'BM'}
    if mime == 'image/webp':
        return mime if header.startswith(b'RIFF') and header[8:12] == b'WEBP' else None
    return mime if header.startswith(signatures[mime]) else None


def task_dict(row):
    item = dict(row)
    item['enabled'] = bool(item['enabled'])
    item['recipients'] = json.loads(item['recipients'])
    return item


def normalize_recipients(value):
    if not isinstance(value, list) or len(value) > 20:
        raise OperationError('收件人须为最多 20 项的列表。')
    items = []
    for recipient in value:
        if isinstance(recipient, str):
            recipient = {'name': recipient, 'channel': '站内'}
        if not isinstance(recipient, dict):
            raise OperationError('收件人须包含姓名和渠道。')
        name = clean_text(recipient.get('name'), '收件人', 120, required=True)
        channel = clean_text(recipient.get('channel', '站内'), '推送渠道', 20, required=True)
        if channel not in CHANNELS:
            raise OperationError('推送渠道须为站内、企业微信或邮件。')
        normalized = {'name': name, 'channel': channel}
        if normalized not in items:
            items.append(normalized)
    return items


class Operations:
    def __init__(self, root, originals=None):
        self.root = Path(root).resolve()
        self.originals = Path(originals).resolve() if originals else None
        self.files = self.root / 'files'
        self.files.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'operations.sqlite3'
        with closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript(SCHEMA)
        self._seed()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _audit(db, action, target='', details='', result='成功', *, actor='本机用户', ip='127.0.0.1'):
        if not isinstance(details, str):
            details = json_value(details)
        db.execute('INSERT INTO audit(at,actor,ip,action,target,details,result) VALUES(?,?,?,?,?,?,?)',
                   (timestamp(), str(actor)[:120], str(ip)[:80], str(action)[:60], str(target)[:240],
                    details[:24000], str(result)[:60]))

    @staticmethod
    def _save_version(db, task, actor):
        config = {key: task[key] for key in ('name', 'report_kind', 'query', 'time', 'recipients')}
        db.execute('INSERT INTO task_versions(task_id,version,config,at,actor) VALUES(?,?,?,?,?)',
                   (task['id'], task['version'], json_value(config), timestamp(), actor))

    def _seed(self):
        with self._write() as db:
            now = timestamp()
            cursor = db.execute('INSERT OR IGNORE INTO tasks(id,name,report_kind,time,enabled,version,recipients,created_at,updated_at) '
                                'VALUES(?,?,?,?,?,?,?,?,?)',
                                (DEFAULT_TASK_ID, '我的日报', 'daily', '08:30', 1, 1, '[]', now, now))
            if cursor.rowcount:
                self._save_version(db, self._task(db, DEFAULT_TASK_ID), '系统')
            # Remove untouched archive entries created by older versions for the bundled attachments.
            db.execute("DELETE FROM documents WHERE source='原始附件' AND actor='系统' AND status='pending' "
                       "AND customer='' AND month='' AND sha256='' AND auto_identified=0")

    def customers(self, limit=500):
        limit = bounded_int(limit, '客户数量', 1, 10000)
        with closing(self._connect()) as db:
            customers = {row[0] for row in db.execute("SELECT DISTINCT customer FROM documents WHERE customer <> '' LIMIT ?", (limit,))}
        for filename in ('bill.sqlite3', 'cost.sqlite3'):
            path = self.root / filename
            if not path.is_file():
                continue
            try:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    # Seek to the next party in records_party instead of scanning every ledger row.
                    indexed = db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='records_party'").fetchone()
                    if indexed:
                        rows = db.execute('''
                            WITH RECURSIVE names(party,n) AS (
                                SELECT (SELECT min(party) FROM records WHERE party > ''), 1
                                UNION ALL
                                SELECT (SELECT min(party) FROM records WHERE party > names.party), n+1
                                FROM names WHERE party IS NOT NULL AND n < ?
                            ) SELECT party FROM names WHERE party IS NOT NULL
                        ''', (limit,))
                    else:
                        rows = db.execute("SELECT DISTINCT party FROM records WHERE party > '' LIMIT ?", (limit,))
                    customers.update(row[0] for row in rows)
            except sqlite3.Error:
                continue
        return sorted(customers)[:limit]

    def detect_metadata(self, filename):
        stem = Path(filename).stem
        normalized = unicodedata.normalize('NFC', stem).casefold()
        matches = [customer for customer in self.customers(10000) if len(customer) >= 2 and customer.casefold() in normalized]
        months = {f'{year}-{int(month):02d}' for year, month in MONTH_PATTERN.findall(normalized)}
        remaining = normalized
        for word in TYPE_WORDS['总部账单']:
            remaining = remaining.replace(word, '')
        kinds = [kind for kind, words in TYPE_WORDS.items()
                 if any(word in (remaining if kind == '客户账单' else normalized) for word in words)]
        return (matches[0] if len(matches) == 1 else '', next(iter(months)) if len(months) == 1 else '',
                kinds[0] if len(kinds) == 1 else '')

    def archive_stats(self):
        with closing(self._connect()) as db:
            item = dict(db.execute("SELECT count(*) total, coalesce(sum(status='ready'),0) ready, "
                                   "coalesce(sum(status='pending'),0) pending, coalesce(sum(source='上传'),0) uploaded, "
                                   "coalesce(sum(auto_identified),0) auto_count FROM documents").fetchone())
        return item

    def documents(self, status='', customer='', month='', kind='', search='', limit=100, offset=0):
        customer, month, kind = document_metadata(customer, month, kind)
        if status not in ('', 'ready', 'pending'):
            raise OperationError('归档状态无效。')
        search = clean_text(search, '搜索词', 200)
        limit = bounded_int(limit, '每页条数', 1, 200)
        offset = bounded_int(offset, '偏移量', 0, 1000000)
        clauses, values = [], []
        for field, value in (('status', status), ('customer', customer), ('month', month), ('kind', kind)):
            if value:
                clauses.append(field + ' = ?')
                values.append(value)
        if search:
            clauses.append('(instr(name_key, ?) > 0 OR instr(lower(customer), ?) > 0 OR instr(lower(kind), ?) > 0)')
            values.extend([search.casefold()] * 3)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with closing(self._connect()) as db:
            return [document_dict(row) for row in db.execute('SELECT * FROM documents' + where +
                                                            ' ORDER BY id DESC LIMIT ? OFFSET ?', (*values, limit, offset))]

    def import_document(self, temp_path, filename, *, customer='', month='', kind='', actor='本机用户', ip='127.0.0.1'):
        filename = clean_text(filename, '文件名', 240, required=True)
        if '/' in filename or '\\' in filename or filename in ('.', '..'):
            raise OperationError('文件名无效。')
        if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise OperationError('文件类型不受支持。')
        customer, month, kind = document_metadata(customer, month, kind)
        supplied = any((customer, month, kind))
        if not all((customer, month, kind)):
            detected = self.detect_metadata(filename)
            customer, month, kind = (customer or detected[0], month or detected[1], kind or detected[2])
        status = 'ready' if customer and month and kind else 'pending'
        source = Path(temp_path)
        if not source.is_file():
            raise OperationError('上传文件不存在。')
        size = source.stat().st_size
        if not 0 < size <= MAX_DOCUMENT_BYTES:
            raise OperationError('单据须大于 0 字节且不超过 30 MiB。')
        name_key = filename.casefold()
        with closing(self._connect()) as db:
            if db.execute('SELECT 1 FROM documents WHERE name_key=? AND size=?', (name_key, size)).fetchone():
                raise OperationError('同名同大小的单据已存在。')

        relative = uuid.uuid4().hex + Path(filename).suffix.lower()
        final_path = self.files / relative
        descriptor, staging_name = tempfile.mkstemp(dir=self.files, prefix='.upload-', suffix='.tmp')
        staging = Path(staging_name)
        digest = hashlib.sha256()
        copied = 0
        moved = False
        try:
            with os.fdopen(descriptor, 'wb') as target, source.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    copied += len(block)
                    if copied > size:
                        raise OperationError('上传文件在归档时发生变化。')
                    digest.update(block)
                    target.write(block)
                if copied != size:
                    raise OperationError('上传文件在归档时发生变化。')
                target.flush()
                os.fsync(target.fileno())
            now = timestamp()
            with self._write() as db:
                cursor = db.execute('INSERT INTO documents(name,name_key,relative_path,size,sha256,customer,month,kind,status,'
                                    'source,auto_identified,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (filename, name_key, relative, size, digest.hexdigest(), customer, month, kind, status,
                                     '上传', int(status == 'ready' and not supplied), actor, now, now))
                os.replace(staging, final_path)
                moved = True
                item = document_dict(self._document(db, cursor.lastrowid))
                self._audit(db, '上传归档', filename, {key: item[key] for key in ('id', 'customer', 'month', 'kind', 'status')},
                            actor=actor, ip=ip)
            return item
        except sqlite3.IntegrityError as error:
            if moved:
                final_path.unlink(missing_ok=True)
            raise OperationError('同名同大小的单据已存在。') from error
        except BaseException:
            if moved:
                final_path.unlink(missing_ok=True)
            raise
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _document(db, document_id):
        key = bounded_int(document_id, '单据编号', 1, 10**12 - 1)
        row = db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone()
        if not row:
            raise OperationError('单据不存在。')
        return row

    def classify_document(self, document_id, customer, month, kind, *, actor='本机用户', ip='127.0.0.1'):
        customer, month, kind = document_metadata(customer, month, kind, required=True)
        with self._write() as db:
            previous = self._document(db, document_id)
            db.execute("UPDATE documents SET customer=?, month=?, kind=?, status='ready', updated_at=? WHERE id=?",
                       (customer, month, kind, timestamp(), previous['id']))
            self._audit(db, '指定归位', previous['name'], {'id': previous['id'], 'customer': customer, 'month': month, 'kind': kind},
                        actor=actor, ip=ip)
            return document_dict(self._document(db, previous['id']))

    def document_file(self, document_id):
        with closing(self._connect()) as db:
            row = self._document(db, document_id)
        base = self.originals if row['source'] == '原始附件' else self.files
        if base is None:
            raise OperationError('原件已丢失，请重新上传。')
        path = (base / row['relative_path']).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            raise OperationError('原件已丢失，请重新上传。')
        item = document_dict(row)
        mime = preview_mime(path)
        item['previewable'] = mime is not None
        item['mime'] = mime or 'application/octet-stream'
        return path, item

    def record(self, action, target='', details='', result='成功', *, actor='本机用户', ip='127.0.0.1'):
        with self._write() as db:
            self._audit(db, action, target, details, result, actor=actor, ip=ip)

    def audit_entries(self, action='', limit=100, offset=0, *, since=None, until=None, actor=''):
        limit = bounded_int(limit, '每页条数', 1, 10000)
        offset = bounded_int(offset, '偏移量', 0, 1000000)
        since = (date.today() - timedelta(days=29)).isoformat() if since is None else since
        clauses, values = [], []
        for field, value in (('action', action), ('actor', actor)):
            if value:
                clauses.append(field + ' = ?')
                values.append(clean_text(value, '审计筛选词', 120))
        if since:
            clauses.append('at >= ?')
            values.append(calendar_date(since, '起始日期'))
        if until:
            clauses.append('at < ?')
            try:
                following_day = date.fromisoformat(calendar_date(until, '结束日期')) + timedelta(days=1)
            except OverflowError as error:
                raise OperationError('结束日期无效。') from error
            values.append(following_day.isoformat())
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM audit' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                                                  (*values, limit, offset))]

    @staticmethod
    def _task(db, task_id):
        task_id = clean_text(task_id, '任务编号', 80, required=True)
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise OperationError('任务不存在。')
        item = task_dict(row)
        run = db.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
        item['last_run'] = dict(run) if run else None
        return item

    def task(self, task_id):
        with closing(self._connect()) as db:
            return self._task(db, task_id)

    def tasks(self):
        with closing(self._connect()) as db:
            rows = db.execute('SELECT id FROM tasks ORDER BY created_at,id LIMIT 200').fetchall()
            return [self._task(db, row['id']) for row in rows]

    def save_task(self, payload, *, actor='本机用户', ip='127.0.0.1'):
        if not isinstance(payload, dict):
            raise OperationError('任务参数须为对象。')
        task_id = clean_text(payload.get('id', ''), '任务编号', 80)
        with self._write() as db:
            before = self._task(db, task_id) if task_id else None
            if before and 'version' in payload and bounded_int(payload['version'], '任务版本', 1, 10**9) != before['version']:
                raise OperationError('任务已更新，请刷新后再修改。')
            existing = before or {}
            name = clean_text(payload.get('name', existing.get('name', '')), '任务名', 100, required=True)
            report_kind = payload.get('report_kind', existing.get('report_kind', 'daily'))
            if report_kind not in ('daily', 'query'):
                raise OperationError('任务类型须为日报或查询。')
            query = clean_text(payload.get('query', existing.get('query', '')), '查询', 1000, required=report_kind == 'query')
            if before and (before['report_kind'], before['query']) != (report_kind, query):
                raise OperationError('任务的工具及查询口径不可更改。请另存为新任务。')
            if report_kind == 'daily' and query:
                raise OperationError('日报任务使用固定报表口径。')
            requested_time = payload.get('time', existing.get('time', ''))
            time = clean_text('' if requested_time is None else requested_time, '执行时间', 5)
            if time and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time):
                raise OperationError('执行时间须使用 HH:MM 格式，留空为手动执行。')
            recipients = normalize_recipients(payload.get('recipients', existing.get('recipients', [])))
            enabled = true_false(payload.get('enabled', existing.get('enabled', True)), '启用状态')
            config = {'name': name, 'report_kind': report_kind, 'query': query, 'time': time, 'recipients': recipients}
            now = timestamp()
            if before:
                changes = {key: {'before': before[key], 'after': value} for key, value in config.items() if before[key] != value}
                if not changes and enabled == before['enabled']:
                    return before
                version = before['version'] + bool(changes)
                db.execute('UPDATE tasks SET name=?, time=?, enabled=?, version=?, recipients=?, updated_at=? WHERE id=?',
                           (name, time, int(enabled), version, json_value(recipients), now, task_id))
                if changes:
                    self._save_version(db, self._task(db, task_id), actor)
                if enabled != before['enabled']:
                    changes['enabled'] = {'before': before['enabled'], 'after': enabled}
                self._audit(db, '修改任务', name, {'task_id': task_id, 'version': version, 'changes': changes}, actor=actor, ip=ip)
            else:
                if db.execute('SELECT count(*) FROM tasks').fetchone()[0] >= 200:
                    raise OperationError('本地任务已达 200 个。')
                task_id = uuid.uuid4().hex
                db.execute('INSERT INTO tasks(id,name,report_kind,query,time,enabled,version,recipients,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (task_id, name, report_kind, query, time, int(enabled), 1, json_value(recipients), now, now))
                self._save_version(db, self._task(db, task_id), actor)
                self._audit(db, '保存任务', name, {'task_id': task_id, 'version': 1, **config}, actor=actor, ip=ip)
            return self._task(db, task_id)

    def set_task_enabled(self, task_id, enabled, *, actor='本机用户', ip='127.0.0.1'):
        enabled = true_false(enabled, '启用状态')
        with self._write() as db:
            task = self._task(db, task_id)
            if enabled != task['enabled']:
                db.execute('UPDATE tasks SET enabled=?,updated_at=? WHERE id=?', (int(enabled), timestamp(), task_id))
                self._audit(db, '启用任务' if enabled else '停用任务', task['name'], {'task_id': task_id}, actor=actor, ip=ip)
            return self._task(db, task_id)

    def set_task_pause(self, task_id, until='', *, actor='本机用户', ip='127.0.0.1'):
        until = calendar_date(until, '暂停日期') if until else ''
        if until and until <= date.today().isoformat():
            raise OperationError('恢复日期须晚于今天。')
        with self._write() as db:
            task = self._task(db, task_id)
            if task['paused_until'] != until:
                db.execute('UPDATE tasks SET paused_until=?,updated_at=? WHERE id=?', (until, timestamp(), task_id))
                self._audit(db, '暂停任务' if until else '恢复任务', task['name'], {'task_id': task_id, 'paused_until': until},
                            actor=actor, ip=ip)
            return self._task(db, task_id)

    def resume_expired_pauses(self, today=None):
        today = calendar_date(today, '恢复日期') if today else date.today().isoformat()
        with self._write() as db:
            tasks = db.execute("SELECT id,name,paused_until FROM tasks WHERE paused_until <> '' AND paused_until <= ?", (today,)).fetchall()
            for task in tasks:
                db.execute("UPDATE tasks SET paused_until='',updated_at=? WHERE id=?", (timestamp(), task['id']))
                self._audit(db, '自动恢复任务', task['name'], {'task_id': task['id'], 'paused_until': task['paused_until']}, actor='系统')
        return len(tasks)

    def task_versions(self, task_id, limit=100):
        limit = bounded_int(limit, '版本条数', 1, 200)
        with closing(self._connect()) as db:
            self._task(db, task_id)
            return [{**dict(row), 'config': json.loads(row['config'])} for row in db.execute(
                'SELECT * FROM task_versions WHERE task_id=? ORDER BY version DESC LIMIT ?', (task_id, limit))]

    def rollback_task(self, task_id, version, expected_version=None, *, actor='本机用户', ip='127.0.0.1'):
        version = bounded_int(version, '目标版本', 1, 10**9)
        with self._write() as db:
            current = self._task(db, task_id)
            if expected_version is not None and bounded_int(expected_version, '当前版本', 1, 10**9) != current['version']:
                raise OperationError('任务已更新，请刷新后再回滚。')
            target = db.execute('SELECT config FROM task_versions WHERE task_id=? AND version=?', (task_id, version)).fetchone()
            if not target:
                raise OperationError('目标版本不存在。')
            if version == current['version']:
                return current
            config = json.loads(target['config'])
            new_version = current['version'] + 1
            db.execute('UPDATE tasks SET name=?,time=?,recipients=?,version=?,updated_at=? WHERE id=?',
                       (config['name'], config['time'], json_value(config['recipients']), new_version, timestamp(), task_id))
            restored = self._task(db, task_id)
            self._save_version(db, restored, actor)
            self._audit(db, '回滚任务', restored['name'], {'task_id': task_id, 'restored_version': version, 'version': new_version},
                        actor=actor, ip=ip)
            return restored

    @staticmethod
    def _run(db, run_id):
        key = clean_text(run_id, '运行编号', 80, required=True)
        row = db.execute('SELECT * FROM task_runs WHERE id=?', (key,)).fetchone()
        if not row:
            raise OperationError('运行记录不存在。')
        return dict(row)

    def start_run(self, task_id, report_date, trigger='手动', *, attempt=1, scheduled_for='', actor='本机用户', ip='127.0.0.1'):
        report_date = calendar_date(report_date, '报表日期')
        scheduled_for = calendar_date(scheduled_for, '计划日期') if scheduled_for else ''
        attempt = bounded_int(attempt, '执行次数', 1, 4)
        trigger = clean_text(trigger, '触发方式', 30, required=True)
        with self._write() as db:
            task = self._task(db, task_id)
            if scheduled_for and (not task['enabled'] or task['paused_until'] > scheduled_for):
                raise OperationError('任务已停用或暂停。')
            run_id = uuid.uuid4().hex
            try:
                db.execute('INSERT INTO task_runs(id,task_id,version,report_date,scheduled_for,trigger,attempt,status,started_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?)', (run_id, task_id, task['version'], report_date, scheduled_for,
                                                         trigger, attempt, 'running', timestamp()))
            except sqlite3.IntegrityError as error:
                raise OperationError('任务正在运行，或该计划尝试已执行。') from error
            self._audit(db, '启动任务', task['name'], {'task_id': task_id, 'run_id': run_id, 'version': task['version'],
                                                      'report_date': report_date, 'attempt': attempt, 'trigger': trigger},
                        actor=actor, ip=ip)
            return self._run(db, run_id)

    def finish_run(self, run_id, success, message='', artifact='', *, actor='本机用户', ip='127.0.0.1'):
        success = true_false(success, '执行结果')
        if not isinstance(message, str) or not isinstance(artifact, str):
            raise OperationError('运行提示与附件须为文本。')
        status = 'succeeded' if success else 'failed'
        with self._write() as db:
            previous = self._run(db, run_id)
            if previous['status'] != 'running':
                return previous
            db.execute('UPDATE task_runs SET status=?, finished_at=?, message=?, artifact=? WHERE id=?',
                       (status, timestamp(), message[:4000], artifact[:500] if success else '', run_id))
            task = self._task(db, previous['task_id'])
            self._audit(db, '任务执行', task['name'], {'run_id': run_id, 'report_date': previous['report_date'], 'message': message[:4000]},
                        '成功' if success else '失败', actor=actor, ip=ip)
            return self._run(db, run_id)

    def runs(self, task_id='', limit=30, scheduled_for=''):
        limit = bounded_int(limit, '运行条数', 1, 200)
        conditions, parameters = [], []
        if task_id:
            conditions.append('task_id=?')
            parameters.append(clean_text(task_id, '任务编号', 80))
        if scheduled_for:
            conditions.append('scheduled_for=?')
            parameters.append(calendar_date(scheduled_for, '计划日期'))
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_runs' + where + ' ORDER BY rowid DESC LIMIT ?',
                                                   (*parameters, limit))]

    def recover_interrupted_runs(self):
        with self._write() as db:
            rows = db.execute("SELECT id,task_id FROM task_runs WHERE status='running'").fetchall()
            for row in rows:
                db.execute("UPDATE task_runs SET status='failed', finished_at=?, message='服务重启，执行中断。' WHERE id=?",
                           (timestamp(), row['id']))
                self._audit(db, '任务执行中断', row['task_id'], {'run_id': row['id']}, '失败', actor='系统')
        return len(rows)
