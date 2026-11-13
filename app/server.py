import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from chat_history import ChatHistory
from daily_report import DailyReportService
import legacy_api
from operations import ARCHIVE_TYPES, MAX_DOCUMENT_BYTES, Operations
from outbound_report import OutboundReportService
from services import Measurements, TaskService, business_now, csv_safe, normalized_day
from tool_router import TOOLS, RoutingError, select_tool


APP = Path(__file__).resolve().parent
SAMPLE_BRIDGE_TAG = b'<script src="/sample_bridge.js"></script>'
DEFAULT_DATA = APP.parents[1] / 'work' / '快递财务智能体数据'
MODEL = os.environ.get('FINANCE_MODEL', 'qwen2.5:7b')
MODEL_CONTEXT = int(os.environ.get('FINANCE_MODEL_CONTEXT', '4096'))
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,149}', MODEL) or MODEL.endswith(':cloud') or not 2048 <= MODEL_CONTEXT <= 32768:
    raise ValueError('模型名称或上下文配置无效，请使用本地模型。')
OLLAMA = os.environ.get('FINANCE_OLLAMA_URL', 'http://127.0.0.1:11434')
if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', OLLAMA):
    raise ValueError('模型服务必须使用本机回环地址。')
KINDS = {'bill': '客户账单', 'cost': '发件成本'}
AMOUNTS = {'bill': '应收金额', 'cost': '总金额'}
GROUPS = {'total': None, 'day': 'day', 'month': 'substr(day,1,7)',
          'party': 'party', 'source': 'file_id'}
DATE_FIELDS = {'bill': '计费时间', 'cost': '成功计费时间'}
MAX_UPLOAD = 2 * 1024**3
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class InputError(Exception):
    pass


def ollama(path, payload=None, timeout=3):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(OLLAMA + path, data=body, headers={'Content-Type': 'application/json'})
    with HTTP.open(request, timeout=timeout) as response:
        return json.load(response)


def model_ready():
    try:
        tag = MODEL if ':' in MODEL.rsplit('/', 1)[-1] else MODEL + ':latest'
        return any(row.get('name') == tag for row in ollama('/api/tags').get('models', []))
    except (OSError, ValueError):
        return False


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


class Datasets:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.status = {}
        for kind in KINDS:
            metadata = self.metadata(kind)
            self.status[kind] = ({**metadata, 'state': 'ready'} if metadata else {'state': 'empty'})

    def path(self, kind):
        return self.directory / f'{kind}.sqlite3'

    def metadata(self, kind, path=None):
        path = path or self.path(kind)
        if not path.is_file():
            return None
        try:
            with connect(path) as db:
                return json.loads(db.execute('SELECT value FROM import_info').fetchone()[0])
        except (sqlite3.Error, ValueError, TypeError):
            return None
        finally:
            if 'db' in locals():
                db.close()

    def current(self):
        with self.lock:
            return {kind: dict(item) for kind, item in self.status.items()}

    def reserve(self, kind, label, state):
        with self.lock:
            if self.status[kind]['state'] in ('receiving', 'queued', 'importing'):
                raise InputError(f'{KINDS[kind]}已有导入任务。')
            self.status[kind] = {'state': state, 'name': label}

    def update(self, kind, **changes):
        with self.lock:
            self.status[kind].update(changes)

    def error(self, kind, message):
        prior = self.metadata(kind)
        if prior:
            self.update(kind, **{**prior, 'state': 'ready', 'error': f'{message}。当前使用上次成功导入的数据。'})
        else:
            self.update(kind, state='error', error=message)

    def queue(self, kind, path, label, remove_after=False):
        self.update(kind, state='queued', message='等待开始导入')
        thread = threading.Thread(target=self.import_file, args=(kind, path, label, remove_after), daemon=True)
        thread.start()

    def import_file(self, kind, path, label, remove_after):
        temporary = self.directory / f'{kind}.staging.sqlite3'
        freight = temporary.with_suffix('.freight.sqlite3') if kind == 'bill' else None
        self.update(kind, state='importing', message='正在读取压缩包和表格')
        try:
            for suffix in ('', '-journal', '-wal', '-shm'):
                Path(str(temporary) + suffix).unlink(missing_ok=True)
            if freight is not None:
                freight.unlink(missing_ok=True)
            command = [sys.executable, '-B', str(APP / 'server.py'), '--worker', kind,
                       str(path), str(temporary), label]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding='utf-8',
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as child:
                for line in child.stdout:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if 'error' in message:
                        self.update(kind, error=message['error'])
                    else:
                        changes = {key: value for key, value in message.items()
                                   if key in ('files', 'rows', 'skipped', 'months', 'message')}
                        if message.get('phase') == 'indexing':
                            changes['message'] = '表格已读取，正在建立查询索引'
                        elif message.get('source'):
                            changes['message'] = '正在读取 ' + message['source']
                        self.update(kind, **changes)
                if child.wait() != 0:
                    raise InputError(self.current()[kind].get('error') or '导入进程未能完成。')
            metadata = self.metadata(kind, temporary)
            if metadata is None:
                raise InputError('导入元数据无法读取。')
            with self.read_lock:
                temporary.replace(self.path(kind))
                if freight is not None and freight.is_file():
                    try:
                        freight.replace(self.directory / 'outbound_bill.sqlite3')
                    except OSError as error:
                        print(f'无法更新日报运费索引：{error}', file=sys.stderr, flush=True)
                self.update(kind, **{**metadata, 'state': 'ready', 'error': None, 'message': '导入完成'})
        except (InputError, OSError, sqlite3.Error) as error:
            self.error(kind, str(error))
        finally:
            for suffix in ('', '-journal', '-wal', '-shm'):
                Path(str(temporary) + suffix).unlink(missing_ok=True)
            if freight is not None:
                freight.unlink(missing_ok=True)
            if remove_after:
                path.unlink(missing_ok=True)


BILL_TERMS = r'客户账单|应收|账单金额|(?:单票|每票)\s*收入'
COST_TERMS = r'发件成本|成本金额'
SCOPE_REPLY = re.compile(r'(?:改(?:成|为)|换(?:成|为)|就看|查(?:一下)?|看(?:一下)?|用|按)?\s*'
                         r'(发件成本|成本金额|单票成本|客户账单|应收金额|应收|单票应收|单票收入)[。！!？?\s]*')
MONTH_COMPARISON = re.compile(r'环比|(?:和|与|跟)\s*上(?:个)?月\s*(?:相?比|对比)|'
                              r'(?:较|比|相比)\s*上(?:个)?月|上(?:个)?月\s*(?:相比|对比)')


def ledger_followup(text, context):
    reply = SCOPE_REPLY.fullmatch(text)
    if reply:
        for previous in reversed(context):
            if SCOPE_REPLY.fullmatch(previous):
                continue
            if (re.search(r'单票|每票|金额|票数|单量|应收|成本', previous)
                    and re.search(r'多少|查询|汇总|统计|各客户|环比|按[月日天客户]|上月比', previous)
                    and not re.search(r'日报|报表|怎么算|怎么计算|口径|找.*(?:单据|原件)|下载|归档', previous)):
                scope = 'cost' if '成本' in reply[1] else 'bill'
                return previous, scope
            break
    return text, None


def normalized_ledger(text, arguments, scope=None):
    parsed = dict(arguments)
    if re.search(r'毛利|利润|净利|不?含税|税额|税率|系统成本|取件成本|操作成本|重量|计费省份|目的地|地区|省份|同比|预测|差额|相差|结算收入', text):
        raise InputError('这两份账单可查询应收金额和发件成本。结算收入、分项成本和毛利请在日报页核对结算数据与口径。')
    if re.search(r'查(?:询)?(?:指定)?运单|运单号[：:]', text) and not parsed.get('waybill'):
        raise InputError('请提供要查询的完整运单号。')
    bill = bool(re.search(BILL_TERMS, text))
    cost = bool(re.search(COST_TERMS, text))
    if scope:
        parsed['scope'] = scope
    elif bill or cost:
        parsed['scope'] = 'both' if bill and cost else ('bill' if bill else 'cost')
    for pattern, group in [(r'按月|每月|月度', 'month'), (r'按天|按日|每日|每天|逐日', 'day'),
                           (r'按客户|各客户|按结算对象|客户排名|每个客户', 'party'), (r'按文件|按来源', 'source')]:
        if re.search(pattern, text):
            parsed['group'] = group
            break
    if re.search(r'各客户|每个客户|所有客户|全部客户', text):
        parsed.pop('party', None)
    if re.search(r'单票|每票', text):
        if not (scope or bill or cost):
            raise InputError('请明确是单票应收金额还是单票发件成本。')
        parsed['metric'] = 'per_ticket'
    elif re.search(r'票数|票量|多少票|单量|多少单|运单数|运单量', text):
        parsed['metric'] = 'tickets'
    return parsed


def period_filter(text, months):
    matches = list(re.finditer(
        r'(?<!\d)(?:(\d{4})\s*[-/.年]\s*)?(\d{1,2})\s*月(?:\s*(\d{1,2})[日号]?(?!\d))?'
        r'|(?<!\d)(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?(?![\d./-])', text))
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if not matches:
        if '本月' in text or '这个月' in text:
            return today.replace(day=1).isoformat(), today.isoformat()
        if '上月' in text or '上个月' in text:
            last = today.replace(day=1) - timedelta(days=1)
            return last.replace(day=1).isoformat(), last.isoformat()
        for word, delta in [('今天', 0), ('昨日', 1), ('昨天', 1), ('前天', 2)]:
            if word in text:
                day = (today - timedelta(days=delta)).isoformat()
                return day, day
        if re.search(r'去年|今年|最近|上周|本周|近\d|季度|\d{4}年', text):
            raise InputError('请用具体月份或起止日期，例如 2026年10月、2026-10-01 至 2026-10-15。')
        return None, None
    if len(matches) > 2 or (len(matches) == 2 and not re.search(r'至|到|—|~|～|\s-\s', text[matches[0].end():matches[1].start()])):
        raise InputError('一次查询支持一个月份、一个日期或一段连续日期。')
    if re.search(r'截至|之前|之后|以来', text):
        raise InputError('请给出明确的起止日期，例如 2026-10-01 至 2026-10-15。')
    ranges = []
    previous_year = None
    for match in matches:
        year, month, day, iso_year, iso_month, iso_day = match.groups()
        year, month, day = (iso_year, iso_month, iso_day) if iso_year else (year, month, day)
        month = int(month)
        if not year:
            years = {value[:4] for value in months if value[5:] == f'{month:02}'}
            if not years:
                years = {value[:4] for value in months}
            year = previous_year or (years.pop() if len(years) == 1 else None)
            if year is None:
                raise InputError('请注明年份，例如 2026年10月。')
        previous_year = year
        try:
            start = date(int(year), month, int(day or 1))
            if day:
                end = start
            else:
                next_month = date(start.year + (month == 12), month % 12 + 1, 1)
                end = next_month - timedelta(days=1)
        except ValueError as error:
            raise InputError('日期无效，请检查年、月、日。') from error
        ranges.append((start.isoformat(), end.isoformat()))
    start, end = ranges[0][0], ranges[-1][1]
    if len(matches) == 1:
        short_end = re.match(r'\s*(?:至|到|—|~|～|-)\s*(\d{1,2})[日号](?!\d)', text[matches[0].end():])
        if short_end and start == end:
            try:
                end = date.fromisoformat(start).replace(day=int(short_end[1])).isoformat()
            except ValueError as error:
                raise InputError('日期无效，请检查年、月、日。') from error
        elif short_end or re.match(r'\s*(?:至|到|—|~|～|-)\s*\d', text[matches[0].end():]):
            raise InputError('请为日期范围注明明确的起止年月日。')
    if start > end:
        raise InputError('结束日期须晚于或等于起始日期。')
    return start, end


def money(cents):
    if cents is None:
        return '无有效金额'
    sign = '-' if cents < 0 else ''
    whole, fraction = divmod(abs(cents), 100)
    return f'{sign}{whole}.{fraction:02}'


def source_label(source):
    label = source['name']
    if source['sheet']:
        label += f' · {source["sheet"]}'
    return f'{label} · {source["amount_field"]} / {source["date_field"]}'


def source_report(datasets):
    rows, sources = [], []
    for kind, status in datasets.current().items():
        if status['state'] != 'ready':
            continue
        rows.append([KINDS[kind], status['files'], status['rows'], '、'.join(status.get('months', [])) or '日期未知'])
        db = connect(datasets.path(kind))
        try:
            sources.extend(source_label(source) for source in db.execute('SELECT * FROM sources ORDER BY id'))
        finally:
            db.close()
    if not rows:
        return {'answer': '数据尚未导入完成，可在数据中心查看进度。'}
    return {'answer': '以下月份来自表内的计费日期。', 'result': {
        'title': '已导入的数据与期间', 'columns': ['数据', '表格数', '记录数', '可用月份'],
        'rows': rows, 'notes': [], 'sources': sources}}


def indexed_span(db, start=None, end=None):
    where, values = ('day BETWEEN ? AND ?', (start, end)) if start else ('day IS NOT NULL', ())
    boundaries = [db.execute(f'SELECT day FROM records WHERE {where} ORDER BY day {order} LIMIT 1', values).fetchone()
                  for order in ('ASC', 'DESC')]
    return tuple(row[0] for row in boundaries) if boundaries[0] else None


def span_label(span):
    return span[0] if span[0] == span[1] else f'{span[0]} 至 {span[1]}'


def fetch_report(db, kind, intent, start, end, distinct, limit, comparison=None, comparable=True):
    group = GROUPS[intent['group']]
    chosen = f'{group} AS bucket' if group else 'NULL AS bucket'
    count = "COUNT(DISTINCT NULLIF(waybill,''))" if distinct else 'COUNT(*)'
    grouped = ' GROUP BY 1' if group else ''
    rate = f'1.0 * SUM(amount_cents) / NULLIF({count},0)'
    sort = rate if intent.get('metric') == 'per_ticket' else ('shown_count' if intent.get('metric') == 'tickets' else 'SUM(amount_cents)')
    order = (' ORDER BY bucket IS NULL, bucket' if intent['group'] in ('day', 'month', 'source')
             else f' ORDER BY {sort} IS NULL, {sort} DESC, bucket')

    def aggregate(first, last, bucket_filter=None):
        where, params = [], []
        if first:
            where.append('day BETWEEN ? AND ?')
            params.extend((first, last))
        for field in ('party', 'waybill'):
            if intent.get(field):
                where.append(f'{field} = ?')
                params.append(intent[field])
        if group and bucket_filter is not None:
            present = [bucket for bucket in bucket_filter if bucket is not None]
            conditions = []
            if present:
                conditions.append(f'{group} IN (' + ','.join('?' for _ in present) + ')')
                params.extend(present)
            if None in bucket_filter:
                conditions.append(f'{group} IS NULL')
            where.append('(' + ' OR '.join(conditions) + ')')
        predicate = ' WHERE ' + ' AND '.join(where) if where else ''
        sql = (f'SELECT {chosen}, COUNT(*) AS n, {count} AS shown_count, COUNT(amount_cents) AS amount_count, '
               f'SUM(amount_cents) AS amount, GROUP_CONCAT(DISTINCT file_id) AS files '
               f'FROM records{predicate}{grouped}{order} LIMIT ?')
        return db.execute(sql, [*params, limit + 1]).fetchall()

    def metric_value(row):
        if not row or not row['n']:
            return None
        if intent.get('metric') == 'tickets':
            return Decimal(row['shown_count'])
        if row['n'] != row['amount_count'] or row['amount'] is None:
            return None
        if intent.get('metric') == 'per_ticket':
            return Decimal(row['amount']) / row['shown_count'] if row['shown_count'] else None
        return Decimal(row['amount'])

    values = aggregate(start, end)
    if not values or not values[0]['n']:
        return [], [], [], False, []
    previous = {}
    if comparison and comparable:
        previous = {row['bucket']: row for row in aggregate(*comparison, [row['bucket'] for row in values[:limit]])}
    inputs = {row['id']: row for row in db.execute('SELECT * FROM sources ORDER BY id')}
    shown, line_sources, source_ids = [], [], set()
    for row in values[:limit]:
        bucket = row['bucket']
        prior = previous.get(bucket)
        prior_value = metric_value(prior)
        current_value = metric_value(row)
        ids = {int(number) for number in (row['files'] or '').split(',') if number}
        if prior:
            ids.update(int(number) for number in (prior['files'] or '').split(',') if number)
        line_sources.append([source_label(inputs[key]) for key in sorted(ids)])
        source_ids.update(ids)
        if intent['group'] == 'source' and bucket in inputs:
            bucket = inputs[bucket]['name'] + (f' · {inputs[bucket]["sheet"]}' if inputs[bucket]['sheet'] else '')
        cells = [KINDS[kind], '合计' if not group else (bucket or '未填写'), row['shown_count'],
                 money(row['amount']), row['n'] - row['amount_count']]
        if intent.get('metric') == 'per_ticket':
            cells.append('无可计算数据' if current_value is None else money(int(current_value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))))
        if comparison:
            if prior_value is None:
                previous_label = '无可比较数据'
            elif intent.get('metric') == 'tickets':
                previous_label = int(prior_value)
            else:
                previous_label = money(int(prior_value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
            change = ((current_value - prior_value) / abs(prior_value) * 100
                      if current_value is not None and prior_value not in (None, 0) else None)
            cells.extend([previous_label, '—' if change is None else f'{change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)}%'])
        shown.append(cells)
    notes = [f'{KINDS[kind]}按「{DATE_FIELDS[kind]}」筛选日期，金额取「{AMOUNTS[kind]}」。']
    if intent['group'] == 'party' or intent.get('party'):
        fields = '、'.join(sorted({inputs[key]['party_field'] for key in source_ids if inputs[key]['party_field']}))
        if fields:
            notes.append(f'{KINDS[kind]}对象字段为「{fields}」。')
    return shown, notes, [source_label(inputs[key]) for key in sorted(source_ids)], len(values) > limit, line_sources


def match_report(datasets, intent, start, end):
    if intent.get('party'):
        raise InputError('按运单匹配支持日期范围筛选。要筛选结算对象或客户，请分别查询两类数据。')
    period = ' AND day BETWEEN ? AND ?' if start else ''
    params = (start, end) if start else ()
    identifiers = f"SELECT DISTINCT waybill FROM {{schema}}.records WHERE waybill IS NOT NULL AND waybill <> ''{period}"
    db = connect(datasets.path('bill'))
    try:
        db.execute('ATTACH DATABASE ? AS costs', (datasets.path('cost').as_uri() + '?mode=ro',))
        bill_ids = identifiers.format(schema='main')
        cost_ids = identifiers.format(schema='costs')
        bills = db.execute(f'SELECT COUNT(*) FROM ({bill_ids})', params).fetchone()[0]
        costs = db.execute(f'SELECT COUNT(*) FROM ({cost_ids})', params).fetchone()[0]
        if not bills and not costs:
            return {'answer': '所选范围内无可匹配的运单号。'}
        matched = db.execute(f'SELECT COUNT(*) FROM ({bill_ids}) AS b WHERE EXISTS ('
                             f'SELECT 1 FROM costs.records WHERE waybill = b.waybill{period})', params * 2).fetchone()[0]
        sources = [source_label(source) for schema in ('main', 'costs')
                   for source in db.execute(f'SELECT * FROM {schema}.sources ORDER BY id')]
    finally:
        db.close()
    filters = f'{start} 至 {end}' if start else '全部已导入记录'
    return {'answer': f'按运单号逐一匹配，查询范围为 {filters}。', 'result': {
        'title': '两类数据的运单匹配', 'columns': ['匹配情况', '去重运单数'],
        'rows': [['两类都有', matched], ['仅客户账单', bills - matched], ['仅发件成本', costs - matched]],
        'notes': ['按两侧非空运单号去重后精确匹配。两份数据的计费日期和覆盖范围可能不同，单边出现的运单需结合覆盖范围核对。'],
        'sources': sources}}


def query(datasets, text, intent):
    scope = intent['scope']
    with datasets.read_lock:
        if scope == 'sources':
            return source_report(datasets)
        selected = list(KINDS) if scope in ('both', 'match') else [scope]
        statuses = datasets.current()
        missing = [KINDS[kind] for kind in selected if statuses[kind]['state'] != 'ready']
        if missing:
            return {'answer': '这些数据尚未导入完成：' + '、'.join(missing) + '。', 'issues': missing}
        months = {month for kind in selected for month in statuses[kind].get('months', [])}
        date_text = text
        for field in ('party', 'waybill'):
            if intent.get(field):
                date_text = date_text.replace(intent[field], ' ')
        compare = bool(MONTH_COMPARISON.search(date_text))
        if compare:
            date_text = MONTH_COMPARISON.sub('', date_text)
        start, end = period_filter(date_text, months)
        if scope == 'match':
            return match_report(datasets, intent, start, end)
        comparison = None
        if compare:
            if not start or not end or not start.endswith('-01') or start[:7] != end[:7] or intent['group'] in ('month', 'day'):
                raise InputError('环比请指定一个完整月份，按整体、客户或文件查询。')
            last = date.fromisoformat(end)
            if (last + timedelta(days=1)).day != 1:
                raise InputError('环比请使用完整月份。')
            previous_end = date.fromisoformat(start) - timedelta(days=1)
            comparison = (previous_end.replace(day=1).isoformat(), previous_end.isoformat())
        if not start and intent['group'] not in ('month', 'source') and not intent.get('waybill') and '全部' not in text:
            today = business_now().date()
            start, end = today.replace(day=1).isoformat(), today.isoformat()
        distinct = intent.get('metric') in ('tickets', 'per_ticket')
        limit_match = re.search(r'前\s*(\d{1,3})', text)
        limit = max(1, min(int(limit_match[1]), 100)) if limit_match else 100
        rows, sources, notes, empty, row_sources, coverage = [], [], [], [], [], []
        for kind in selected:
            db = connect(datasets.path(kind))
            try:
                comparable = True
                if start:
                    observed = indexed_span(db, start, end)
                    if not observed:
                        available = indexed_span(db)
                        label = f'已导入记录的计费日期为 {span_label(available)}。' if available else ''
                        coverage.append(f'{KINDS[kind]}在所选期间没有计费记录。{label}')
                    elif observed != (start, end):
                        coverage.append(f'{KINDS[kind]}在所选期间的实际计费记录日期为 {span_label(observed)}。')
                    if comparison:
                        prior = indexed_span(db, *comparison)
                        comparable = observed == (start, end) and prior == comparison
                        if not prior:
                            coverage.append(f'{KINDS[kind]}缺少 {comparison[0][:7]} 的记录，环比留空。')
                        elif not comparable:
                            coverage.append(f'{KINDS[kind]}上月实际计费记录日期为 {span_label(prior)}，因期间覆盖不足，环比留空。')
                items, rules, provenance, capped, rows_provenance = fetch_report(
                    db, kind, intent, start, end, distinct, limit, comparison, comparable)
            finally:
                db.close()
            if not items:
                empty.append(KINDS[kind])
                continue
            rows.extend(items)
            row_sources.extend(rows_provenance)
            sources.extend(provenance)
            notes.extend(rules)
            quality = statuses[kind]
            if quality.get('summary_rows'):
                notes.append(f'{KINDS[kind]}已排除 {quality["summary_rows"]} 条合计行。')
            if start and quality.get('missing_dates'):
                notes.append(f'{KINDS[kind]}整份数据有 {quality["missing_dates"]} 条缺失或无法识别日期的记录，日期筛选会排除这些记录。')
            if quality.get('rounded_amounts'):
                notes.append(f'{KINDS[kind]}整份数据有 {quality["rounded_amounts"]} 条金额超过两位小数，已按每行四舍五入到分。')
            if capped:
                notes.append(f'{KINDS[kind]}仅显示前 {limit} 组，下载范围与当前表格一致。')
    filters = f'{start} 至 {end}' if start else '全部已导入记录'
    if intent.get('party'):
        filters += f'，对象 {intent["party"]}'
    if intent.get('waybill'):
        filters += f'，运单号 {intent["waybill"]}'
    answer = f'查询范围为 {filters}。' + ''.join(coverage)
    if not rows:
        if scope == 'bill' and re.search(r'(?:单票|每票)\s*收入', text):
            answer += '客户账单按应收金额统计。'
        return {'answer': '无符合条件的数据。' + answer}
    if empty:
        notes.append('无符合条件的数据：' + '、'.join(empty) + '。')
    if scope == 'both':
        notes.append('两类数据按各自计费日期和字段独立统计。')
    if distinct:
        notes.append('票数按组内非空运单号去重。')
    if intent.get('metric') == 'per_ticket':
        notes.append('单票金额为同组金额 ÷ 非空运单号去重票数。缺金额或分母为零时留空。')
    columns = ['数据', {'total': '范围', 'day': '日期', 'month': '月份', 'party': '对象', 'source': '文件'}[intent['group']],
               '去重票数' if distinct else '记录数', '金额（元）', '缺金额记录数']
    if intent.get('metric') == 'per_ticket':
        columns.append('单票金额（元/票）')
    if comparison:
        columns.extend(['上月同口径', '环比'])
        notes.append(f'环比比较 {comparison[0]} 至 {comparison[1]}，计费记录日期未覆盖整月或上月值为零时留空。')
    return {'answer': answer, 'result': {
        'title': '运单查询' if intent.get('waybill') else '账单与成本查询',
        'columns': columns, 'rows': rows, 'notes': notes, 'sources': sources, 'row_sources': row_sources,
        'basis_version': '验证框架 v2'}}


ARCHIVE_KIND = {'customer_bill': '客户账单', 'cost': '发件成本', 'hq_bill': '总部账单',
                'penalty': '罚款单', 'claim': '理赔结算单', 'complaint': '客诉证据',
                'report': '经营情况汇总', 'other': '其他'}

POLICIES = {
    'daily': ('日报按结算日期统计，默认取昨日。需要已确认的不含税收入、系统成本、取件成本、操作成本、重量与运单等字段。'
              '缺少当日记录时会等待重试，完整的正式版还需要客户现用《报表输出.xlsx》。', '需求说明书 §3.4、§5'),
    'query': ('客户账单查「应收金额」和「计费时间」，发件成本查「总金额」和「成功计费时间」。'
              '票数按非空运单号去重，单票金额按同组金额除以票数。', '本地账单字段映射 · 验证框架 v2'),
    'archive': ('归档要核对客户、月份和单据类型。文件名不能唯一确定这三项时，文件保留在待确认清单，由人工指定。', '需求说明书 §3.5–3.6'),
    'offline': ('模型在本机选择工具。大文件由程序读取，计算走本机数据库，结果随附来源和口径。', '需求说明书 §3.7、§4.1'),
}


def known_months(server):
    months = {month for item in server.datasets.current().values() for month in item.get('months', [])}
    coverage = server.daily.status().get('coverage')
    if coverage:
        day = date.fromisoformat(coverage['first_day']).replace(day=1)
        end = date.fromisoformat(coverage['last_day'])
        while day <= end and len(months) < 600:
            months.add(day.isoformat()[:7])
            day = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return months


def archive_results(server, text, arguments):
    month = ''
    search_text = text
    for key in ('customer', 'keyword'):
        if arguments.get(key):
            search_text = search_text.replace(arguments[key], ' ')
    start, end = period_filter(search_text, known_months(server))
    if start:
        if start[:7] != end[:7]:
            raise InputError('归档检索一次支持一个月份。')
        month = start[:7]
    if arguments.get('month') and arguments['month'] != month:
        raise InputError('请明确归档月份，例如 2026年10月。')
    docs = server.operations.documents(status={'archived': 'ready', 'pending': 'pending'}.get(arguments.get('status'), ''),
                                       customer=arguments.get('customer', ''), month=month,
                                       kind=ARCHIVE_KIND.get(arguments.get('kind'), ''),
                                       search=arguments.get('keyword', ''), limit=30)
    server.operations.record('检索单据', '对话检索', {'count': len(docs), 'month': month})
    if not docs:
        return {'answer': '档案库没有符合筛选条件的单据。', 'actions': [{'label': '打开数据中心', 'view': 'docs'}]}
    return {'answer': f'显示 {len(docs)} 份符合条件的本机单据。', 'result': {
        'title': '归档单据检索', 'columns': ['文件', '客户', '月份', '类型', '状态'],
        'rows': [[doc['name'], doc['customer'], doc['month'], doc['kind'], '待确认' if doc['status'] == 'pending' else '已归档'] for doc in docs],
        'sources': [doc['name'] for doc in docs], 'notes': ['每次最多显示 30 份。']},
        'actions': [{'label': '下载 ' + doc['name'], 'url': f'/api/local/archive/file?id={doc["id"]}'} for doc in docs[:8]]}


def task_change_plan(server, text, arguments):
    version = server.operations.task('daily')['version']
    request = {'id': 'daily', 'version': version, 'action': arguments['action']}
    if request['action'] == 'resume':
        description = '恢复「我的日报」的定时计划'
    elif re.search(r'停用|永久停发', text):
        request.update(action='enable', enabled=False)
        description = '停用「我的日报」的定时计划'
    else:
        days = re.search(r'(\d{1,3}|[一二两三四五六七八九十])\s*天', text)
        if days:
            value = days[1]
            count = int(value) if value.isdigit() else '一二三四五六七八九十'.index('二' if value == '两' else value) + 1
            if not 1 <= count <= 365:
                raise InputError('暂停天数请填写 1 至 365 天。')
            until = (business_now().date() + timedelta(days=count)).isoformat()
        else:
            start, end = period_filter(text, known_months(server))
            if not start or start != end:
                raise InputError('请说明暂停几天，或给出恢复日期。')
            until = start
        if until <= business_now().date().isoformat():
            raise InputError('恢复日期须晚于今天。')
        request['until'] = until
        description = f'暂停「我的日报」，于 {until} 恢复'
    return {'answer': '待确认操作：' + description + '。', 'actions': [
        {'kind': 'confirm', 'label': description, 'request': request}]}


DAILY_REPORT_NAME = re.compile(r'日报|每日经营|日经营报表')
DAILY_REPORT_RUN = re.compile(r'^\s*(?:(?:请|帮我|给我|麻烦(?:你)?|能否|能不能|可以|可否|重新|再)\s*){0,3}'
                              r'(?:生成|执行|运行|重跑|跑|出|做)')
DAILY_REPORT_LATEST = re.compile(r'(?:最近|最新)\s*(?:(?:有(?:结算)?数据|可用)\s*)?(?:的|那|\s)*(?:一|1)\s*[日天]'
                                 r'|(?:最近|最新)(?:可用的?)?(?:结算(?:明细)?数据|结算日(?:期)?|的?日报)')


def daily_chat_action(text, context):
    report_named = DAILY_REPORT_NAME.search(text)
    asks_policy = re.search(r'怎么|如何|为什么|为何|口径|规则|需要什么|说明', text)
    stops_report = re.search(r'暂停|恢复|停用|停发|不(?:要|用|必)?(?:再)?(?:生成|跑|发)', text)
    if report_named and DAILY_REPORT_RUN.match(text) and not asks_policy and not stops_report:
        return 'latest' if DAILY_REPORT_LATEST.search(text) else 'run'
    if is_coverage_question(text):
        if report_named:
            return 'coverage'
        for previous in reversed(context):
            if DAILY_REPORT_NAME.search(previous):
                return 'coverage'
            if not is_coverage_question(previous):
                break
    return None


def is_coverage_question(text):
    return re.search(r'最[近新晚]|截止|截至|到哪', text) and re.search(r'日期|哪[日天]|到哪|截止|截至|范围', text)


def requested_daily_outcome(server, day, trigger='手动'):
    if not day or server.daily.status(day)['ready']:
        return server.tasks.run('daily', day, trigger)
    outbound = getattr(server, 'outbound', None)
    if outbound is not None:
        with server.datasets.read_lock:
            if outbound.available():
                return outbound_outcome(outbound.generate(day))
    outcome = server.tasks.run('daily', day, trigger)
    issue = server.datasets.current().get('bill', {}).get('outbound_issue')
    if issue:
        outcome['missing'] = [*outcome.get('missing', []), issue]
    return outcome


def outbound_outcome(outcome):
    if outcome.get('state') != 'ready':
        return outcome
    outcome['actions'] = [{**item, 'label': f'下载{item["format"].upper()}'} for item in outcome['exports']]
    return outcome


def latest_daily_outcome(server, generate):
    status = server.daily.status()
    coverage = status.get('coverage')
    if not coverage:
        outbound = getattr(server, 'outbound', None)
        if outbound is not None:
            with server.datasets.read_lock:
                preview_coverage = outbound.coverage()
                matched_day = preview_coverage and preview_coverage['last_matched_day']
                if matched_day and generate:
                    return outbound_outcome(outbound.generate(matched_day))
            if preview_coverage:
                if matched_day:
                    message = f'原始账单最晚在 {matched_day} 有符合回查窗口的同号运单，可以生成该日的历史出港验证报表。'
                else:
                    message = '当前两类原始账单的日期范围有重叠，但在回查窗口内没有找到同号运单。'
                return {'answer': message + '正式日报仍需结算明细。', 'coverage': preview_coverage}
        return {'answer': '尚未导入标准日报结算明细，暂时无法确定最晚可生成的日报日期。'
                          '请下载结算明细模板，按结算日期补齐逐票明细后从数据中心导入。',
                'status': status, 'state': status['state'], 'ready': False, 'missing': status['missing'], 'exports': []}
    first, last = coverage['first_day'], coverage['last_day']
    if generate:
        return server.tasks.run('daily', last, '对话')
    span = first if first == last else f'{first} 至 {last}'
    return {'answer': f'已导入的标准日报结算明细覆盖 {span}，最晚可生成 {last} 的日报。'
                      '区间内能否生成日报，以当日是否有结算记录为准。',
            'coverage': coverage, 'sources': [status['source']]}


def select_conversation_tool(text, context, ledger_text, scope):
    routing_text = f'{ledger_text}（本次按{KINDS[scope]}统计）' if scope else text
    clear_ledger = scope
    if (not clear_ledger and re.search(r'(?:单票|每票)\s*收入', text)
            and re.search(r'多少|汇总|查询|统计|对比|环比|各客户', text)
            and not re.search(r'日报|报表|口径|规则|怎么|如何|为什么', text)):
        clear_ledger = 'bill'
    try:
        selected = select_tool(ollama, MODEL, MODEL_CONTEXT, routing_text, context if not scope else ())
    except RoutingError:
        if not clear_ledger:
            raise
        selected = None
    if clear_ledger and (not selected or selected[0] != 'query_ledgers'):
        return 'query_ledgers', {'scope': clear_ledger, 'group': 'total'}, '明确查询口径'
    return selected


def dispatch(server, text, context=(), allow_tasks=True):
    # The model receives only the bounded question and schema, never ZIP/XLSX rows or database access.
    ledger_text, scope = ledger_followup(text, context)
    daily_action = daily_chat_action(text, context)
    if daily_action:
        name = 'daily_coverage' if daily_action == 'coverage' else 'generate_daily'
        arguments, mode = {}, '本地规则'
    else:
        name, arguments, mode = select_conversation_tool(text, context, ledger_text, scope)
    if name == 'query_ledgers':
        outcome = query(server.datasets, ledger_text, normalized_ledger(ledger_text, arguments, scope))
    elif name in ('match_waybills', 'list_sources'):
        outcome = query(server.datasets, text, {'scope': 'match' if name == 'match_waybills' else 'sources', 'group': 'total'})
    elif name == 'daily_coverage':
        outcome = latest_daily_outcome(server, False)
        outcome['issues'] = outcome.get('missing', [])
    elif name == 'generate_daily' and allow_tasks:
        if DAILY_REPORT_LATEST.search(text):
            if re.search(r'\d+\s*月|\d{4}[-/.年]|本月|上个月|上月|以前|之前|以后|之后|截止|截至', text):
                raise InputError('请指定一个结算日期，或查询已导入明细中的最近一个结算日。')
            outcome = latest_daily_outcome(server, True)
        else:
            start, end = period_filter(text, known_months(server))
            if start != end:
                raise InputError('日报一次只生成一个结算日。')
            outcome = requested_daily_outcome(server, start, '对话')
        outcome.setdefault('issues', outcome.get('missing') or outcome.get('status', {}).get('missing', []))
        if not outcome.get('exports') and outcome.get('report_kind') != 'outbound_history':
            outcome['actions'] = [{'label': '查看日报数据并补传', 'view': 'tasks'}]
            if outcome.get('day') and outcome.get('missing'):
                outcome['answer'] = f'{outcome["day"]} 日报数据未就绪。' + ''.join(outcome['missing'])
    elif name == 'search_documents':
        outcome = archive_results(server, text, arguments)
    elif name == 'explain_policy':
        answer, source = POLICIES[arguments['topic']]
        outcome = {'answer': answer, 'sources': [source]}
    elif name == 'plan_daily_pause' and allow_tasks:
        outcome = task_change_plan(server, text, arguments)
    else:
        outcome = {'answer': '可试试“按月汇总客户账单”“按运单号匹配两类数据”“查找罚款单”或“生成日报”。'}
    if name in ('generate_daily', 'daily_coverage') and outcome.get('status', {}).get('state') == 'missing_source':
        outcome.setdefault('actions', []).insert(0, {'label': '下载日报结算明细模板',
                                                     'name': '日报结算明细_导入模板.csv',
                                                     'url': '/api/local/reports/template'})
    if name == 'generate_daily' and outcome.get('report_kind') == 'outbound_history':
        name, label = 'generate_outbound_daily', '生成历史出港验证报表'
    else:
        label = '查看日报结算日期' if name == 'daily_coverage' else TOOLS[name][0]
    outcome['tool'] = {'name': name, 'label': label, 'mode': mode}
    server.operations.record('工具调用', label, {'mode': mode, 'rows': len(outcome.get('result', {}).get('rows', []))},
                             '待补数据' if outcome.get('issues') else '成功')
    return outcome


def task_view(task):
    view = dict(task)
    run = task.get('last_run')
    if run and run.get('artifact'):
        name = run['artifact']
        route = 'reports' if task['report_kind'] == 'daily' else 'tasks'
        view['last_run'] = {**run, 'artifact': {'name': name, 'url': f'/api/local/{route}/file?name=' + quote(name)}}
    if task.get('enabled') and task.get('time'):
        now = business_now()
        hour, minute = map(int, task['time'].split(':'))
        planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if planned <= now:
            planned += timedelta(days=1)
        if task.get('paused_until'):
            resume = date.fromisoformat(task['paused_until'])
            if planned.date() < resume:
                planned = datetime.combine(resume, planned.timetz())
        view['next_run'] = planned.isoformat()
    return view


class Handler(BaseHTTPRequestHandler):
    timeout = 180

    def log_message(self, *_):
        pass

    def reply(self, status, content, mime='application/json; charset=utf-8', attachment=None):
        data = json.dumps(content, ensure_ascii=False).encode('utf-8') if mime.startswith('application/json') else content
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        if attachment:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(attachment, safe=''))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, filename, mime='application/octet-stream', inline=False):
        with path.open('rb') as stream:
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
            self.send_header('Content-Disposition', ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(filename, safe=''))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile, 1024**2)

    def permitted(self):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        origin = self.headers.get('Origin')
        return self.headers.get('Host') in hosts and (not origin or origin in {f'http://{host}' for host in hosts})

    def do_GET(self):
        if not self.permitted():
            self.record('访问拒绝', '本地服务', result='拒绝')
            return self.reply(403, {'error': '请从本地地址访问。'})
        route = urlsplit(self.path)
        params = {name: items[0] for name, items in parse_qs(route.query).items()}
        try:
            return self.get_route(route.path, params)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (InputError, ValueError) as error:
            self.reply(400, {'error': str(error)})
        except (sqlite3.Error, OSError) as error:
            print(f'本地读取失败：{error}', file=sys.stderr, flush=True)
            self.reply(500, {'error': '本地读取失败，请检查数据目录。'})

    def get_route(self, route, params):
        ops = self.server.operations
        daily = self.server.daily
        if route in ('/', '/index.html'):
            sample = (APP / 'index.html').read_bytes()
            if not (APP / 'sample_bridge.js').is_file():
                raise OSError('页面接口适配脚本缺失。')
            if sample.count(b'</body>') != 1:
                raise InputError('页面样板缺少唯一的结束标记。')
            return self.reply(200, sample.replace(b'</body>', SAMPLE_BRIDGE_TAG + b'</body>'), 'text/html; charset=utf-8')
        if route == '/sample_bridge.js':
            return self.reply(200, (APP / 'sample_bridge.js').read_bytes(), 'text/javascript; charset=utf-8')
        if route == '/api/local/chat/sessions':
            return self.reply(200, self.server.chat_history.sessions())
        if route == '/api/local/chat/messages':
            return self.reply(200, self.server.chat_history.messages(params.get('session_id'), params.get('before')))
        if route == '/api/local/status':
            return self.reply(200, {'model': MODEL, 'ollama': model_ready(), 'datasets': self.server.datasets.current(),
                                    'version': '2.0 验证框架'})
        if route == '/api/local/reports':
            reports = daily.status(params.get('date'))
            reports['history'] = daily.history()
            reports['runs'] = ops.runs('daily', limit=12)
            return self.reply(200, reports)
        if route == '/api/local/reports/template':
            return self.reply(200, daily.input_template(), 'text/csv; charset=utf-8', '日报结算明细_导入模板.csv')
        if route == '/api/local/reports/preview':
            day = params.get('date')
            outcome = daily.preview(day)
            if day and not outcome['ready']:
                with self.server.datasets.read_lock:
                    if self.server.outbound.available():
                        outcome = self.server.outbound.preview(day)
            return self.reply(200, outcome)
        if route == '/api/local/reports/outbound-file':
            path = self.server.outbound.download_path(params.get('name', ''))
            mime = ('text/csv; charset=utf-8' if path.suffix == '.csv' else
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.record('下载出港验证报表', path.name)
            return self.send_file(path, f'历史出港验证报表_{path.name[:10]}{path.suffix}', mime)
        if route in ('/api/local/reports/file', '/api/local/tasks/file'):
            name = params.get('name', '')
            is_daily = route.endswith('/reports/file')
            path = daily.download_path(name) if is_daily else self.server.tasks.file(name)
            self.record('下载报表', path.name)
            mime = ('text/csv; charset=utf-8' if path.suffix == '.csv' else
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            filename = path.name
            if is_daily:
                filename = (f'技术验证版_每日经营情况_{path.name[:7]}.csv' if path.suffix == '.csv'
                            else f'技术验证版_经营日报_{path.name[:10]}.xlsx')
            return self.send_file(path, filename, mime)
        if route == '/api/local/archive':
            filters = {key: params[key] for key in ('status', 'customer', 'month', 'kind', 'search', 'limit', 'offset') if key in params}
            documents = ops.documents(**filters)
            self.record('检索单据', '档案库', {'count': len(documents)})
            return self.reply(200, {'documents': documents, 'types': ARCHIVE_TYPES, 'stats': ops.archive_stats()})
        if route == '/api/local/archive/file':
            path, document = ops.document_file(params.get('id'))
            preview = params.get('preview') == '1'
            if preview and not document['previewable']:
                raise InputError('该格式可下载原件。')
            self.record('预览单据' if preview else '下载单据', document['name'])
            return self.send_file(path, document['name'], document['mime'] if preview else 'application/octet-stream', preview)
        if route == '/api/local/tasks':
            return self.reply(200, {'tasks': [task_view(task) for task in ops.tasks()], 'runs': ops.runs()})
        if route == '/api/local/tasks/versions':
            return self.reply(200, {'versions': ops.task_versions(params.get('id'))})
        if route in ('/api/local/audit', '/api/local/audit/export'):
            filters = {key: params[key] for key in ('action', 'limit', 'offset', 'since', 'until', 'actor') if key in params}
            entries = ops.audit_entries(**filters)
            if route.endswith('/export'):
                output = io.StringIO(newline='')
                writer = csv.writer(output)
                writer.writerow(['时间', '操作者', 'IP', '操作', '对象', '结果', '详情'])
                for row in entries:
                    values = [row.get(key, '') for key in ('at', 'actor', 'ip', 'action', 'target', 'result', 'details')]
                    writer.writerow([csv_safe(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value) for value in values])
                self.record('导出审计', '审计日志', {'count': len(entries)})
                return self.reply(200, output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', '审计日志.csv')
            return self.reply(200, {'entries': entries})
        if route == '/api/local/monitor':
            stats = ops.archive_stats()
            metrics = self.server.measurements.metrics({'total': stats['uploaded'], 'recognized': stats['auto_count']})
            recent_alerts = ops.audit_entries(action='任务告警', limit=3)
            metrics['alerts'].extend(row['target'] + '有尚未生成的日报，请查看任务记录。' for row in recent_alerts)
            return self.reply(200, {**metrics, 'model': MODEL, 'ollama': model_ready(),
                                    'datasets': self.server.datasets.current(),
                                    'tools': [{'name': name, 'label': definition[0], 'description': definition[1]}
                                              for name, definition in TOOLS.items() if name != 'unsupported']})
        if not legacy_api.get(self, route, params):
            return self.reply(404, {'error': '页面或接口不存在。'})

    def do_POST(self):
        if not self.permitted():
            self.record('访问拒绝', '本地服务', result='拒绝')
            return self.reply(403, {'error': '请从本地地址访问。'})
        route = urlsplit(self.path)
        try:
            if route.path == '/api/local/import':
                return self.upload(parse_qs(route.query))
            if route.path == '/api/local/chat/sessions':
                session = self.server.chat_history.select(self.body().get('id'))
                return self.reply(200, {'session': session})
            if route.path == '/api/local/query':
                payload = self.body()
                text = payload.get('text')
                if not isinstance(text, str) or not text.strip() or len(text) > 500:
                    raise InputError('请输入 1 至 500 字的查询。')
                context = payload.get('context', [])
                if not isinstance(context, list) or len(context) > 8 or any(not isinstance(item, str) or len(item) > 500 for item in context):
                    raise InputError('查询上下文过长。')
                text = text.strip()
                return self.chat_reply(payload, text, 'chat', context, lambda: self.run_query(text, context))
            if route.path == '/api/local/reports/run':
                return self.reply(200, requested_daily_outcome(self.server, self.body().get('date')))
            if route.path in ('/api/local/reports/input', '/api/local/archive'):
                return self.receive_document(route.path, parse_qs(route.query))
            if route.path == '/api/local/archive/classify':
                payload = self.body()
                document = self.server.operations.classify_document(payload.get('id'), payload.get('customer', ''),
                    payload.get('month', ''), payload.get('kind', ''), ip=self.client_address[0])
                return self.reply(200, {'document': document})
            if route.path == '/api/local/tasks':
                return self.change_task(self.body())
            if not legacy_api.post(self, route.path, parse_qs(route.query)):
                self.reply(404, {'error': '页面或接口不存在。'})
        except (InputError, ValueError) as error:
            self.reply(400, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (sqlite3.Error, OSError) as error:
            print(f'请求失败 {type(error).__name__}: {error}', file=sys.stderr, flush=True)
            self.reply(500, {'error': '本地处理失败，请重试并检查磁盘与导入状态。'})

    def run_query(self, text, context):
        if not self.server.measurements.begin():
            return 429, {'error': '已有三个查询在运行，请稍后再试。'}
        started = time.monotonic()
        try:
            return 200, dispatch(self.server, text, context)
        finally:
            self.server.measurements.end(started)

    def chat_reply(self, payload, text, mode, request_values, run):
        if 'session_id' not in payload and 'request_id' not in payload:
            return self.reply(*run())
        request_id = payload.get('request_id')
        history = self.server.chat_history
        replay = history.begin(payload.get('session_id'), request_id, text, mode, request_values)
        if replay is not None:
            return self.reply(*replay)
        try:
            status, response = run()
        except (InputError, ValueError) as error:
            status, response = 400, {'error': str(error)}
        except Exception as error:
            print(f'处理会话请求失败：{type(error).__name__}', file=sys.stderr, flush=True)
            status, response = 500, {'error': '本地处理失败，请重试并检查磁盘与导入状态。'}
        history.finish(request_id, status, response)
        return self.reply(status, response)

    def record(self, action, target, details='', result='成功'):
        self.server.operations.record(action, target, details, result, ip=self.client_address[0])

    def body(self):
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            raise InputError('请求格式应为 JSON。')
        value = json.loads(self.rfile.read(self.length(24000)))
        if not isinstance(value, dict):
            raise InputError('请发送 JSON 对象。')
        return value

    def change_task(self, payload):
        ops = self.server.operations
        action = payload.get('action')
        task_id = payload.get('id')
        if action == 'run':
            day = payload.get('date')

            def run():
                if task_id == 'daily':
                    return requested_daily_outcome(self.server, day)
                return self.server.tasks.run(task_id, day)

            if 'session_id' not in payload and 'request_id' not in payload:
                return self.reply(200, run())
            text = payload.get('chat_text', '运行任务')
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise InputError('任务会话说明须为 1 至 500 字。')
            return self.chat_reply(payload, text.strip(), 'task', [task_id, day],
                                   lambda: (200, run()))
        if payload.get('confirmed') is not True:
            raise InputError('请确认修改任务。')
        ip = self.client_address[0]
        if action == 'save':
            task = ops.save_task(payload, ip=ip)
        elif action == 'rollback':
            task = ops.rollback_task(task_id, payload.get('restore_version'), payload.get('version'), ip=ip)
        elif action in ('enable', 'pause', 'resume'):
            before = ops.task(task_id)
            if payload.get('version') is not None and payload['version'] != before['version']:
                raise InputError('任务已更新，请刷新并重新确认。')
            if action == 'enable':
                task = ops.set_task_enabled(task_id, payload.get('enabled'), ip=ip)
            else:
                until = payload.get('until') if action == 'pause' else ''
                if action == 'pause' and (not until or normalized_day(until) <= business_now().date().isoformat()):
                    raise InputError('恢复日期须晚于今天。')
                task = ops.set_task_pause(task_id, until, ip=ip)
        else:
            raise InputError('未知任务操作。')
        return self.reply(200, {'task': task_view(task)})

    def receive_document(self, route, params):
        label = params.get('name', [''])[0].replace('\\', '/').rsplit('/', 1)[-1]
        is_daily = route.endswith('/input')
        if not label or len(label) > 230 or (is_daily and Path(label).suffix.lower() != '.csv'):
            raise InputError('请提供有效文件名，日报结算数据使用 CSV。')
        remaining = self.length(MAX_UPLOAD if is_daily else MAX_DOCUMENT_BYTES)
        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix='input-', dir=self.server.datasets.directory, delete=False) as stream:
                path = Path(stream.name)
                while remaining:
                    piece = self.rfile.read(min(remaining, 1024**2))
                    if not piece:
                        raise InputError('文件接收未完成。')
                    stream.write(piece)
                    remaining -= len(piece)
            if is_daily:
                result = self.server.daily.import_source(path, label)
                self.record('导入日报明细', label, {'rows': result.get('rows')})
                return self.reply(200, result)
            document = self.server.operations.import_document(path, label, ip=self.client_address[0])
            return self.reply(200, {'document': document})
        finally:
            if path:
                path.unlink(missing_ok=True)

    def length(self, limit):
        raw = self.headers.get('Content-Length', '')
        if not raw.isascii() or not raw.isdigit() or len(raw) > 12 or not 0 < int(raw) <= limit:
            raise InputError('文件或请求为空，或超出允许大小。')
        return int(raw)

    def upload(self, params):
        kind = params.get('kind', [''])[0]
        label = params.get('name', [''])[0].replace('\\', '/').rsplit('/', 1)[-1]
        suffix = Path(label).suffix.lower()
        if kind not in KINDS or suffix not in ('.zip', '.xlsx', '.csv') or len(label) > 230:
            raise InputError('请选择对应数据类型的 ZIP、XLSX 或 CSV 文件。')
        remaining = self.length(MAX_UPLOAD)
        total = remaining
        datasets = self.server.datasets
        datasets.reserve(kind, label, 'receiving')
        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f'{kind}-upload-', suffix=suffix, dir=datasets.directory, delete=False) as file:
                path = Path(file.name)
                next_update = total - 32 * 1024**2
                while remaining:
                    piece = self.rfile.read(min(remaining, 1024**2))
                    if not piece:
                        raise InputError('接收未完成，请重新选择文件。')
                    file.write(piece)
                    remaining -= len(piece)
                    if remaining < next_update:
                        datasets.update(kind, message=f'已接收 {(total - remaining) * 100 // total}%')
                        next_update = remaining - 32 * 1024**2
            datasets.queue(kind, path, label, remove_after=True)
            self.record('导入账单索引', label, {'kind': kind, 'bytes': total}, '已接收')
        except (InputError, OSError):
            if path:
                path.unlink(missing_ok=True)
            datasets.error(kind, '接收文件失败')
            raise
        self.reply(202, {'ok': True})


def worker(kind, source, destination, label):
    from data_engine import import_dataset

    sys.stdout.reconfigure(encoding='utf-8')
    try:
        metadata = import_dataset(kind, Path(source), Path(destination),
                                  lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
                                  display_name=label)
        metadata['name'] = label
        if kind == 'bill':
            from build_outbound_bill import build_outbound_bill

            metadata['bill_revision'] = uuid.uuid4().hex
            try:
                build_outbound_bill(source, Path(destination).parent, display_name=label,
                                    bill_revision=metadata['bill_revision'],
                                    destination=Path(destination).with_suffix('.freight.sqlite3'),
                                    progress=lambda event: print(json.dumps({
                                        'message': '正在读取日报所需的运费列' if event['phase'] == 'reading'
                                        else '正在建立日报运费索引'}, ensure_ascii=False), flush=True))
            except (OSError, ValueError, sqlite3.Error) as error:
                metadata['outbound_issue'] = '日报运费索引不可用：' + str(error)
        with sqlite3.connect(destination) as db:
            db.execute('CREATE TABLE import_info (value TEXT NOT NULL)')
            db.execute('INSERT INTO import_info VALUES (?)', (json.dumps(metadata, ensure_ascii=False),))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), flush=True)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description='快递财务智能体 本地服务')
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--bill', type=Path)
    parser.add_argument('--cost', type=Path)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--originals', type=Path, default=APP.parent / '原始附件')
    parser.add_argument('--no-scheduler', action='store_true', help='测试时仅手动运行任务')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    url = f'http://127.0.0.1:{args.port}/'
    if args.open:
        try:
            with HTTP.open(url + 'api/local/status', timeout=1) as response:
                existing = json.load(response)
            if existing.get('model') == MODEL and set(existing.get('datasets', {})) == set(KINDS):
                webbrowser.open(url)
                return
        except (OSError, ValueError):
            pass
    datasets = Datasets(args.data_dir.resolve())
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.datasets = datasets
        server.daily = DailyReportService(datasets.directory)
        server.outbound = OutboundReportService(datasets.directory)
        server.operations = Operations(datasets.directory, args.originals.resolve())
        server.chat_history = ChatHistory(datasets.directory)
        server.measurements = Measurements(datasets.directory)
        server.tasks = TaskService(server.operations, server.daily, datasets.directory,
                                   lambda text: dispatch(server, text, allow_tasks=False))
        server.finance_dispatch = lambda text, context=(): dispatch(server, text, context)
        server.finance_config = lambda: {'model': MODEL, 'baseUrl': OLLAMA, 'hasKey': model_ready()}
        if not args.no_scheduler:
            server.tasks.start()
        for kind in KINDS:
            path = getattr(args, kind)
            if path is not None and datasets.current()[kind]['state'] != 'ready':
                datasets.reserve(kind, path.name, 'queued')
                datasets.queue(kind, path.resolve(), path.name)
        print(f'快递财务智能体 {url}', flush=True)
        if args.open:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.tasks.stopped.set()


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--worker':
        sys.exit(worker(*sys.argv[2:]))
    main()
