"""Translate the original sample's HTTP contract to the real local services."""

import csv
import ctypes
import io
import json
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from daily_report import REQUIRED
from operations import MAX_DOCUMENT_BYTES
from services import business_now


UPLOAD_LIMIT = 2 * 1024**3
SAMPLE_TYPES = {'客户账单': '客户月结单', '罚款单': '罚款通知单'}
ARCHIVE_TYPES = {display: stored for stored, display in SAMPLE_TYPES.items()}
INDEX_FILES = {'客户账单数据.zip': 'bill', '发件成本数据.zip': 'cost'}


def param(params, name):
    value = params.get(name, '')
    return value[0] if isinstance(value, list) and value else value


def doc_view(document):
    return {'id': str(document['id']), 'name': document['name'], 'size': document['size'],
            'customer': document['customer'], 'month': document['month'],
            'type': SAMPLE_TYPES.get(document['kind'], document['kind']),
            'confident': document['status'] == 'ready', 'previewable': document['previewable'],
            'uploadedBy': document['actor'], 'createdAt': document['created_at']}


def task_item(task):
    last = task.get('last_run')
    last_label = '尚未运行'
    if last:
        status = {'succeeded': '成功', 'failed': '失败', 'running': '执行中'}.get(last['status'], last['status'])
        last_label = f'{last["report_date"]} · {status}'
        if last.get('message'):
            last_label += ' · ' + last['message']
    resume = task['paused_until']
    scheduled = task['time']
    next_label = '—'
    if task['enabled'] and scheduled:
        now = business_now()
        hour, minute = map(int, scheduled.split(':'))
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        if resume and date.fromisoformat(resume) > next_run.date():
            next_run = datetime.combine(date.fromisoformat(resume), next_run.timetz())
        next_label = next_run.strftime('%Y-%m-%d %H:%M')
    recipients = task['recipients']
    return {'id': task['id'], 'name': task['name'], 'period': f'每日 {scheduled}' if scheduled else '—（仅手动执行）',
            'to': '、'.join(f'{item["name"]}（{item["channel"]}）' for item in recipients) or '—',
            'channel': ','.join(dict.fromkeys(item['channel'] for item in recipients)),
            'last': last_label, 'next': next_label, 'on': task['enabled'], 'ver': task['version'],
            'pausedUntil': resume, 'reportKind': task['report_kind'], 'source': task['query']}


def task_items(operations):
    return [task_item(task) for task in operations.tasks()]


def physical_memory():
    if hasattr(ctypes, 'windll'):
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32), ('sizes', ctypes.c_uint64 * 7)]
        info = Memory()
        info.length = ctypes.sizeof(info)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
            return int(info.sizes[0]), int(info.sizes[1])
        return 0, 0
    try:
        fields = {}
        with open('/proc/meminfo', encoding='ascii') as stream:
            for line in stream:
                name, value = line.split(':', 1)
                if name in ('MemTotal', 'MemAvailable'):
                    fields[name] = int(value.split()[0]) * 1024
        return fields.get('MemTotal', 0), fields.get('MemAvailable', 0)
    except (OSError, ValueError, IndexError):
        return 0, 0


def sample_metrics(server):
    stats = server.operations.archive_stats()
    measurement = server.measurements
    measured = measurement.metrics({'total': stats['uploaded'], 'recognized': stats['auto_count']})
    rows = {row['name']: row['value'] for row in measured['metrics']}
    with measurement.lock:
        sample_count = len(measurement.samples)
    disk = shutil.disk_usage(server.datasets.directory)
    mem_total, mem_free = physical_memory()
    average, last = rows.get('最近 10 次平均响应'), measured['last_seconds']
    return {'recognize': {'rate': rows.get('单据自动识别率'), 'ok': stats['auto_count'], 'total': stats['uploaded']},
            'latency': {'avg': None if average is None else round(average * 1000),
                        'last': None if last is None else round(last * 1000), 'n': sample_count},
            'concurrency': {'active': measured['active'], 'peak': measured['peak']},
            'cpu': {'usage': rows.get('CPU 使用率'), 'cores': measured['cores']},
            'mem': {'usedPct': rows.get('内存使用率'), 'total': mem_total, 'free': mem_free},
            'disk': {'usedPct': rows.get('磁盘使用率'), 'total': disk.total, 'free': disk.free},
            'gpu': {'available': False, 'note': '本服务未采集显存指标'},
            'uptime': measured['uptime_seconds'], 'procUptime': measured['uptime_seconds'],
            'alerts': [{'text': alert} for alert in measured['alerts']]}


def get(handler, route, params):
    server = handler.server
    operations = server.operations
    if route == '/api/config':
        handler.reply(200, {**server.finance_config(), 'authorized': True, 'needCode': False, 'keyHint': ''})
    elif route == '/api/docs':
        filters = {key: param(params, key) for key in ('status', 'customer', 'month', 'kind', 'search', 'limit', 'offset')
                   if key in params}
        filters.setdefault('limit', 200)
        if 'kind' in filters:
            filters['kind'] = ARCHIVE_TYPES.get(filters['kind'], filters['kind'])
        documents = operations.documents(**filters)
        handler.reply(200, {'docs': [doc_view(item) for item in documents], 'customers': operations.customers(),
                            'stats': operations.archive_stats(), 'datasets': server.datasets.current(),
                            'report': server.daily.status()})
    elif route == '/api/file':
        path, document = operations.document_file(param(params, 'id'))
        inline = param(params, 'inline') == '1' and document['previewable']
        handler.record('预览单据' if inline else '下载单据', document['name'])
        handler.send_file(path, document['name'], document['mime'] if inline else 'application/octet-stream', inline)
    elif route == '/api/tasks':
        handler.reply(200, {'tasks': task_items(operations)})
    elif route == '/api/sys':
        handler.reply(200, sample_metrics(server))
    elif route == '/api/audit':
        filters = {key: param(params, key) for key in ('action', 'since', 'until', 'limit', 'offset', 'actor')
                   if key in params}
        handler.reply(200, {'entries': operations.audit_entries(**filters)})
    elif route == '/api/audit/export':
        handler.get_route('/api/local/audit/export', {key: param(params, key) for key in params})
    elif route == '/api/rules':
        handler.reply(200, {'rules': [], 'supported': False})
    elif route == '/api/tsrc':
        handler.reply(200, {'tsrc': {}, 'supported': False})
    else:
        return False
    return True


def csv_header_matches(prefix):
    try:
        line = prefix.splitlines()[0].decode('utf-8-sig')
        fields = next(csv.reader(io.StringIO(line)))
        return set(REQUIRED).issubset(field.strip() for field in fields)
    except (UnicodeError, csv.Error, StopIteration, IndexError):
        return False


def upload(handler, params):
    label = param(params, 'name').replace('\\', '/').rsplit('/', 1)[-1]
    suffix = Path(label).suffix.lower()
    forced = param(params, 'kind')
    if not label or len(label) > 230 or any(ord(character) < 32 for character in label):
        raise ValueError('请提供有效文件名。')
    if forced not in ('', 'archive', 'daily', 'bill', 'cost'):
        raise ValueError('文件类别无效。')
    server = handler.server
    datasets = server.datasets
    size = handler.length(UPLOAD_LIMIT)
    prefix = handler.rfile.read(min(size, 32 * 1024))
    if len(prefix) != min(size, 32 * 1024):
        raise ValueError('文件接收未完成。')
    kind = forced or INDEX_FILES.get(label) or ('daily' if suffix == '.csv' and csv_header_matches(prefix) else 'archive')
    if kind == 'archive' and size > MAX_DOCUMENT_BYTES:
        raise ValueError('普通归档文件上限为 30 MB。大文件请选择账单或成本索引导入。')
    if kind in ('bill', 'cost') and suffix not in ('.zip', '.xlsx', '.csv'):
        raise ValueError('账单索引支持 ZIP、XLSX、CSV。')
    if kind == 'daily' and suffix != '.csv':
        raise ValueError('日报结算数据请使用标准 CSV。')
    path = None
    retained = False
    reserved = False
    try:
        if kind in ('bill', 'cost'):
            datasets.reserve(kind, label, 'receiving')
            reserved = True
        with tempfile.NamedTemporaryFile(prefix='sample-upload-', suffix=suffix, dir=datasets.directory, delete=False) as stream:
            path = Path(stream.name)
            stream.write(prefix)
            remaining = size - len(prefix)
            while remaining:
                chunk = handler.rfile.read(min(remaining, 1024**2))
                if not chunk:
                    raise ValueError('文件接收未完成。')
                stream.write(chunk)
                remaining -= len(chunk)
        if kind in ('bill', 'cost'):
            datasets.queue(kind, path, label, remove_after=True)
            retained = True
            handler.record('导入账单索引', label, {'kind': kind, 'bytes': size}, '已接收')
            handler.reply(202, {'ok': True, 'kind': kind, 'state': 'queued', 'doc': {'name': label, 'size': size}})
        elif kind == 'daily':
            imported = server.daily.import_source(path, label)
            handler.record('导入日报明细', label, {'rows': imported.get('rows')})
            handler.reply(200, {'ok': True, 'kind': kind, 'doc': {'name': label, 'size': size}, 'report': imported})
        else:
            document = server.operations.import_document(path, label, ip=handler.client_address[0])
            handler.reply(200, {'ok': True, 'kind': kind, 'doc': doc_view(document)})
    except (OSError, ValueError):
        if reserved and not retained:
            datasets.error(kind, '接收失败')
        raise
    finally:
        if path and not retained:
            path.unlink(missing_ok=True)


def period_time(period):
    if not isinstance(period, str):
        raise ValueError('执行周期须为文本。')
    if not period or '手动' in period or period == '—':
        return ''
    matched = re.fullmatch(r'(?:每日|每天)\s+((?:[01]\d|2[0-3]):[0-5]\d)', period)
    if matched:
        return matched[1]
    raise ValueError('当前任务支持每日定时或仅手动执行。')


def recipients(options):
    to = options.get('to', '')
    channel = options.get('channel', '') or '站内'
    if not isinstance(to, str) or not isinstance(channel, str):
        raise ValueError('推送对象和渠道须为文本。')
    if not to.strip() or to == '—':
        return []
    name = re.sub(r'（(?:站内|企业微信|邮件)）$', '', to.strip())
    return [{'name': name, 'channel': item.strip()} for item in channel.split(',') if item.strip()]


def update_tasks(handler, payload):
    operations = handler.server.operations
    ip = handler.client_address[0]
    task_id = payload.get('id')
    if payload.get('action') == 'run':
        outcome = handler.server.tasks.run(task_id, payload.get('date'))
        handler.reply(200, {'ok': True, **outcome, 'tasks': task_items(operations)})
        return
    create = payload.get('create')
    if isinstance(create, dict):
        question = create.get('query', '')
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError('请从已有的查询结果保存任务。')
        scheduled = period_time(create.get('period', ''))
        task = operations.save_task({'name': create.get('name'), 'report_kind': 'query', 'query': question,
                                     'time': scheduled, 'enabled': create.get('on', True),
                                     'recipients': recipients(create)}, ip=ip)
        handler.reply(200, {'ok': True, 'created': task_item(task), 'tasks': task_items(operations)})
        return
    patch = payload.get('patch')
    if not isinstance(patch, dict) or not task_id:
        raise ValueError('任务请求缺少编号或参数。')
    task = operations.task(task_id)
    if 'pausedUntil' in patch and patch['pausedUntil']:
        operations.set_task_pause(task_id, patch['pausedUntil'], ip=ip)
    elif 'on' in patch or 'pausedUntil' in patch:
        if 'pausedUntil' in patch:
            operations.set_task_pause(task_id, '', ip=ip)
        if 'on' in patch:
            operations.set_task_enabled(task_id, patch['on'], ip=ip)
    elif 'period' in patch or 'to' in patch:
        update = {'id': task_id, 'version': task['version']}
        if 'period' in patch:
            update['time'] = period_time(patch['period'])
        if 'to' in patch:
            item = task_item(task)
            if patch['to'] != item['to']:
                update['recipients'] = recipients({'to': patch['to'], 'channel': item['channel']})
        operations.save_task(update, ip=ip)
    else:
        raise ValueError('任务执行状态由实际运行记录产生。')
    handler.reply(200, {'ok': True, 'tasks': task_items(operations)})


def chat(handler, payload):
    messages = payload.get('messages')
    if not isinstance(messages, list) or len(messages) > 200:
        raise ValueError('会话消息无效。')
    user_text = [item['content'].strip() for item in messages if isinstance(item, dict) and item.get('role') == 'user'
                 and isinstance(item.get('content'), str) and item['content'].strip()]
    if not user_text or len(user_text[-1]) > 500:
        raise ValueError('请输入 1 至 500 字的查询。')
    measured = handler.server.measurements
    if not measured.begin():
        handler.reply(429, {'error': '已有三个查询在运行。', 'message': '已有三个查询在运行。'})
        return
    started = time.monotonic()
    try:
        outcome = handler.server.finance_dispatch(user_text[-1], [text[:500] for text in user_text[-9:-1]])
    finally:
        measured.end(started)
    text = outcome.get('answer', '').replace('```', '｀｀｀')
    report = outcome.get('result')
    if report:
        card = {'title': report['title'], 'rows': [report['columns'], *report['rows']],
                'source': '、'.join(report.get('sources', [])),
                'basis': report.get('notes', []) or ['按本次实际查询口径显示。'],
                'basis_version': report.get('basis_version'), 'exports': outcome.get('exports', []),
                'query': user_text[-1]}
        encoded = json.dumps(card, ensure_ascii=False).replace('```', r'\u0060\u0060\u0060')
        text += '\n\n```result\n' + encoded + '\n```'
    elif outcome.get('issues'):
        text += '\n' + '\n'.join(f'- {item}' for item in outcome['issues']).replace('```', '｀｀｀')
    data = {'choices': [{'delta': {'content': text}}]}
    sse = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\ndata: [DONE]\n\n'
    handler.reply(200, sse.encode('utf-8'), 'text/event-stream; charset=utf-8')


def post(handler, route, params):
    if route not in ('/api/upload', '/api/docs', '/api/tasks', '/api/chat', '/api/rules', '/api/tsrc'):
        return False
    try:
        if route == '/api/upload':
            upload(handler, params)
        elif route == '/api/docs':
            payload = handler.body()
            patch = payload.get('patch')
            if not isinstance(patch, dict):
                raise ValueError('请选择归档客户、月份和单据类型。')
            category = patch.get('type', '')
            document = handler.server.operations.classify_document(payload.get('id'), patch.get('customer', ''),
                         patch.get('month', ''), ARCHIVE_TYPES.get(category, category), ip=handler.client_address[0])
            handler.reply(200, {'ok': True, 'doc': doc_view(document)})
        elif route == '/api/tasks':
            update_tasks(handler, handler.body())
        elif route == '/api/chat':
            chat(handler, handler.body())
        else:
            handler.body()
            raise ValueError('此入口尚未接入。正式计算仍使用已验证的数据源和口径。')
    except ValueError as error:
        handler.reply(400, {'ok': False, 'error': str(error), 'message': str(error)})
    return True
