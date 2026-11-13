"""Durable local conversation history for the sample web client."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


PAGE_SIZE = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    active_id TEXT NOT NULL REFERENCES chat_sessions(id)
);
CREATE TABLE IF NOT EXISTS chat_turns (
    seq INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    question TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('chat','task')),
    signature TEXT NOT NULL,
    status INTEGER,
    preview TEXT NOT NULL DEFAULT '',
    response TEXT
);
CREATE INDEX IF NOT EXISTS chat_turns_session ON chat_turns(session_id, seq DESC);
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError(f'{label}无效。')
    return value


def compact_text(value, limit):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


class ChatHistory:
    def __init__(self, root):
        self.path = Path(root) / 'chat_history.sqlite3'
        with closing(self._connect()) as db, db:
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript(SCHEMA)
            message = '上一次处理因服务关闭而中断，问题已保留。'
            db.execute('UPDATE chat_turns SET status=503, preview=?, response=? WHERE status IS NULL',
                       (message, json.dumps({'error': message}, ensure_ascii=False)))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _create(db, session_id, now):
        db.execute("INSERT INTO chat_sessions VALUES (?,'新对话',?,?) ON CONFLICT(id) DO NOTHING",
                   (session_id, now, now))

    def sessions(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            active = db.execute('SELECT active_id FROM chat_state WHERE singleton=1').fetchone()
            items = db.execute('''
                SELECT s.*, coalesce(nullif(t.preview,''), substr(t.question,1,120),'') AS preview
                FROM chat_sessions s LEFT JOIN chat_turns t ON t.seq=(
                    SELECT seq FROM chat_turns WHERE session_id=s.id ORDER BY seq DESC LIMIT 1
                ) ORDER BY s.updated_at DESC, s.id
            ''')
            return {'active_id': active['active_id'] if active else None, 'sessions': [dict(row) for row in items]}

    def select(self, session_id):
        session_id = identifier(session_id, '会话编号')
        with self._write() as db:
            self._create(db, session_id, timestamp())
            db.execute('INSERT INTO chat_state VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET active_id=excluded.active_id',
                       (session_id,))
            item = dict(db.execute('SELECT * FROM chat_sessions WHERE id=?', (session_id,)).fetchone())
            last = db.execute('SELECT preview, question FROM chat_turns WHERE session_id=? ORDER BY seq DESC LIMIT 1',
                              (session_id,)).fetchone()
            item['preview'] = (last['preview'] or compact_text(last['question'], 120)) if last else ''
            return item

    def messages(self, session_id, before=None):
        session_id = identifier(session_id, '会话编号')
        cursor = 2**63 - 1
        if before is not None:
            if not re.fullmatch(r'[0-9]{1,19}', before) or not 0 < int(before) <= cursor:
                raise ValueError('会话分页位置无效。')
            cursor = int(before)
        with closing(self._connect()) as db:
            rows = db.execute('SELECT * FROM chat_turns WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT ?',
                              (session_id, cursor, PAGE_SIZE + 1)).fetchall()
        result = []
        for row in reversed(rows[:PAGE_SIZE]):
            request_id, question = row['request_id'], row['question']
            result.append({'role': 'user', 'content': question, 'request_id': request_id, 'mode': row['mode']})
            assistant = {'role': 'assistant', 'query': question, 'mode': row['mode'], 'request_id': request_id}
            if row['status'] is None:
                assistant.update(state='pending', content='正在处理…')
            else:
                response = json.loads(row['response'])
                if row['status'] >= 400:
                    assistant.update(state='error', content=response.get('error', '处理失败。'))
                else:
                    assistant.update(state='complete', content=response.get('answer') or response.get('message') or '',
                                     response=response)
            result.append(assistant)
        return {'messages': result, 'next_before': rows[PAGE_SIZE - 1]['seq'] if len(rows) > PAGE_SIZE else None}

    def begin(self, session_id, request_id, question, mode, request_values):
        session_id = identifier(session_id, '会话编号')
        request_id = identifier(request_id, '请求编号')
        payload = json.dumps([mode, question, request_values], ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        with self._write() as db:
            existing = db.execute('SELECT session_id, signature, status, response FROM chat_turns WHERE request_id=?',
                                  (request_id,)).fetchone()
            if existing:
                if (existing['session_id'], existing['signature']) != (session_id, signature):
                    raise ValueError('请求编号已被另一条消息使用。')
                if existing['status'] is None:
                    return 409, {'error': '这条消息正在处理，请稍后查看。'}
                return existing['status'], json.loads(existing['response'])
            now = timestamp()
            self._create(db, session_id, now)
            db.execute('INSERT OR IGNORE INTO chat_state VALUES (1,?)', (session_id,))
            db.execute('INSERT INTO chat_turns(request_id,session_id,question,mode,signature) VALUES (?,?,?,?,?)',
                       (request_id, session_id, question, mode, signature))
            title = compact_text(question, 18) or '新对话'
            db.execute("UPDATE chat_sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END, updated_at=? WHERE id=?",
                       (title, now, session_id))

    def finish(self, request_id, status, response):
        preview = compact_text(response.get('answer') or response.get('message') or response.get('error'), 120)
        body = json.dumps(response, ensure_ascii=False)
        with self._write() as db:
            db.execute('UPDATE chat_turns SET status=?, preview=?, response=? WHERE request_id=? AND status IS NULL',
                       (status, preview, body, request_id))
