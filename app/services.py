"""Local task execution, offline scheduling and health measurements."""

import csv
import ctypes
import os
import shutil
import sqlite3
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


BUSINESS_ZONE = timezone(timedelta(hours=8))


def business_now():
    return datetime.now(BUSINESS_ZONE)


def normalized_day(value):
    if value in ('', None):
        return (business_now().date() - timedelta(days=1)).isoformat()
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('结算日期请用 YYYY-MM-DD。')
    return date.fromisoformat(value).isoformat()


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        try:
            float(value)
        except ValueError:
            return "'" + value
    return value


class TaskService:
    def __init__(self, operations, daily, data_dir, run_query):
        self.ops = operations
        self.daily = daily
        self.run_query = run_query
        self.files = data_dir / 'task-exports'
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.stopped = threading.Event()

    def query_artifact(self, outcome):
        report = outcome.get('result')
        if not report or not report.get('rows'):
            return None
        self.files.mkdir(parents=True, exist_ok=True)
        name = f'查询任务-{uuid4().hex}.csv'
        path = self.files / name
        try:
            with path.open('x', newline='', encoding='utf-8-sig') as stream:
                writer = csv.writer(stream)
                for row in ([report['title']], ['导出时间', business_now().isoformat(timespec='seconds')],
                            ['口径版本', '验证框架 v2'], ['查询结论', outcome.get('answer', '')],
                            ['数据来源', *report.get('sources', [])], ['计算口径', *report.get('notes', [])],
                            [], report['columns'], *report['rows']):
                    writer.writerow([csv_safe(value) for value in row])
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return name

    def file(self, name):
        path = self.files / name
        if not name.startswith('查询任务-') or Path(name).name != name or path.suffix != '.csv' or not path.is_file():
            raise ValueError('找不到任务结果文件。')
        return path

    def run(self, task_id, day=None, trigger='手动', *, attempt=1, scheduled_for=''):
        report_day = normalized_day(day)
        task = self.ops.task(task_id)
        if not self.lock.acquire(blocking=False):
            raise ValueError('另一个任务正在执行，请稍后重试。')
        try:
            record = self.ops.start_run(task_id, report_day, trigger, attempt=attempt, scheduled_for=scheduled_for)
            try:
                if task['report_kind'] == 'daily':
                    outcome = self.daily.generate(report_day)
                    for export in outcome.get('exports', []):
                        export.setdefault('url', '/api/local/reports/file?name=' + encoded(export['filename']))
                else:
                    outcome = self.run_query(task['query'])
                    filename = self.query_artifact(outcome)
                    if filename:
                        outcome['exports'] = [{'name': filename, 'filename': filename,
                                              'url': '/api/local/tasks/file?name=' + encoded(filename)}]
                exports = outcome.get('exports', [])
                success = bool(exports)
                outcome['run'] = self.ops.finish_run(record['id'], success, outcome.get('answer', ''),
                                                     exports[0]['filename'] if exports else '')
                outcome['task'] = self.ops.task(task_id)
                return outcome
            except (ValueError, OSError) as error:
                self.ops.finish_run(record['id'], False, str(error))
                return {'answer': str(error), 'issues': [str(error)], 'run': self.ops.runs(task_id, limit=1)[0]}
            except Exception:
                self.ops.finish_run(record['id'], False, '执行出错，未生成结果文件。')
                raise
        finally:
            self.lock.release()

    def tick(self, now=None):
        now = now or business_now()
        scheduled_for = now.date().isoformat()
        self.ops.resume_expired_pauses(scheduled_for)
        for task in self.ops.tasks():
            if (not task['enabled'] or not task['time'] or task['time'] > now.strftime('%H:%M') or
                    (task.get('paused_until') and task['paused_until'] > scheduled_for)):
                continue
            runs = self.ops.runs(task['id'], limit=4, scheduled_for=scheduled_for)
            if runs:
                last = runs[0]
                if last['status'] != 'failed' or last['attempt'] >= 4:
                    continue
                finished = datetime.fromisoformat(last['finished_at'])
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=BUSINESS_ZONE)
                if now - finished < timedelta(minutes=5):
                    continue
            attempt = runs[0]['attempt'] + 1 if runs else 1
            outcome = self.run(task['id'], (now.date() - timedelta(days=1)).isoformat(), '定时',
                               attempt=attempt, scheduled_for=scheduled_for)
            if outcome['run']['status'] == 'failed' and attempt == 4:
                self.ops.record('任务告警', task['name'], '首次执行及三次重试均未生成报表。', '失败')
                latest = {}
                for run in self.ops.runs(task['id'], limit=60):
                    if run['scheduled_for']:
                        latest.setdefault(run['scheduled_for'], run)
                recent = list(latest.values())[:3]
                if len(recent) == 3 and all(run['status'] == 'failed' and run['attempt'] >= 4 for run in recent):
                    self.ops.set_task_enabled(task['id'], False)
                    self.ops.record('自动停用任务', task['name'], '连续三个执行周期失败。', '成功')

    def start(self):
        if self.started.is_set():
            return
        self.started.set()
        self.ops.recover_interrupted_runs()

        def loop():
            while not self.stopped.wait(12):
                try:
                    self.tick()
                except (ValueError, OSError, sqlite3.Error) as error:
                    print(f'调度检查未完成：{error}', flush=True)

        threading.Thread(target=loop, daemon=True, name='daily-scheduler').start()


def encoded(value):
    from urllib.parse import quote
    return quote(value, safe='')


class Measurements:
    def __init__(self, directory):
        self.directory = directory
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.samples = deque(maxlen=10)
        self.cpu_at = self.cpu = None

    def begin(self):
        with self.lock:
            if self.active >= 3:
                return False
            self.active += 1
            self.peak = max(self.active, self.peak)
            return True

    def end(self, start):
        with self.lock:
            self.active -= 1
            self.samples.append(time.monotonic() - start)

    def metrics(self, counts):
        with self.lock:
            busy, peak = self.active, self.peak
            timings = list(self.samples)
            sample = cpu_times()
            if sample and self.cpu_at:
                total = sample[0] - self.cpu_at[0]
                if total > 0:
                    self.cpu = round((sample[1] - self.cpu_at[1]) / total * 100, 1)
            self.cpu_at = sample
            cpu = self.cpu
        disk = shutil.disk_usage(self.directory)
        automatic = counts.get('recognized', 0)
        total = counts.get('total', 0)
        metrics = [
            metric('单据自动识别率', round(100 * automatic / total, 1) if total else None, '%', 90, minimum=True),
            metric('最近 10 次平均响应', round(sum(timings) / len(timings), 2) if timings else None, '秒', 5),
            metric('当前会话数', busy, '个', 3),
            metric('CPU 使用率', cpu, '%', 85),
            metric('内存使用率', memory_percent(), '%', 85),
            metric('磁盘使用率', round(100 * disk.used / disk.total, 1), '%', 90),
        ]
        return {'active': busy, 'limit': 3, 'peak': peak, 'uptime_seconds': round(time.monotonic() - self.started),
                'last_seconds': round(timings[-1], 2) if timings else None,
                'disk_free_gib': round(disk.free / 1024**3, 1), 'cores': os.cpu_count(), 'metrics': metrics,
                'alerts': [item['name'] + '超过设定范围' for item in metrics if item['status'] == 'alert']}


def metric(name, value, unit, threshold, minimum=False):
    outside = value is not None and (value < threshold if minimum else value > threshold)
    return {'name': name, 'value': value, 'unit': unit, 'threshold': threshold,
            'comparison': '≥' if minimum else '≤', 'status': 'alert' if outside else ('empty' if value is None else 'ok')}


def cpu_times():
    try:
        if os.name == 'nt':
            idle, kernel, user = (ctypes.c_uint64() for _ in range(3))
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                total = kernel.value + user.value
                return total, total - idle.value
            return None
        with open('/proc/stat', encoding='ascii') as stream:
            times = [int(value) for value in stream.readline().split()[1:9]]
        total = sum(times)
        return total, total - times[3] - times[4]
    except (OSError, ValueError, IndexError):
        return None


def memory_percent():
    try:
        if os.name == 'nt':
            class Memory(ctypes.Structure):
                _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32), ('memory', ctypes.c_uint64 * 7)]
            info = Memory()
            info.length = ctypes.sizeof(info)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
                return info.load
            return None
        with open('/proc/meminfo', encoding='ascii') as stream:
            values = {line.split(':', 1)[0]: line.split()[1] for line in stream if ':' in line}
        return round(100 * (1 - int(values['MemAvailable']) / int(values['MemTotal'])), 1)
    except (OSError, ValueError, KeyError):
        return None
