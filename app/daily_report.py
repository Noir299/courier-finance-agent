"""Offline report calculations on explicitly normalized settlement data."""

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


REPORT_TIMEZONE = timezone(timedelta(hours=8))
VERSION = '技术验证口径 v1'
HEADERS = ('结算日期', '运单号', '客户', '组织类别', '组织', '重量', '收入不含税',
           '系统成本', '取件成本', '操作成本', '重量段', '流向', '来源文件')
REQUIRED = HEADERS[:-1]
SHEETS = ('经营情况汇总', '利润表-重量维度', '利润表-重量(按日)', '利润表-流向维度',
          '利润表-流向(按日)', '客户业务量情况--重量维度', '客户业务量情况--流向维度')
METRICS = ('票数', '重量', '均重', '收入（不含税）', '系统成本', '取件成本', '操作成本', '毛利', '毛利率', '单票毛利')
GROUPS = ('业务量', '业务量', '业务量', '收入', '成本', '成本', '成本', '毛利', '毛利', '毛利')
SOURCE_GAPS = ['需要按结算日期逐票归集的标准日报明细。',
               '需要确认收入不含税，以及系统、取件、操作三类成本。',
               '需要组织类别、组织、重量段及流向。']
ASSUMPTIONS = ['依据标准输入字段计算。按结算日和运单号去重，完全相同的重复明细只计算一次。',
               '月份视图汇总截至所选日报日期的记录。月度票数为每日去重票数之和。',
               '缺失日期或组织的单元格留空。零收入时毛利率留空。',
               '当前附件未提供正式《报表输出.xlsx》。本文件的 7 张工作表按需求说明构建，用于验证计算和导出。']
SQL_SCHEMA = '''
CREATE TABLE records (
    day TEXT NOT NULL, waybill TEXT NOT NULL, customer TEXT NOT NULL,
    org_kind TEXT NOT NULL, org TEXT NOT NULL, weight INTEGER NOT NULL,
    income INTEGER NOT NULL, system_cost INTEGER NOT NULL,
    pickup_cost INTEGER NOT NULL, operation_cost INTEGER NOT NULL,
    weight_band TEXT NOT NULL, flow TEXT NOT NULL, source TEXT,
    PRIMARY KEY (day, waybill)
) WITHOUT ROWID;
CREATE TABLE source_metadata (value TEXT NOT NULL);
CREATE TABLE daily_sources (
    day TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (day, source)
) WITHOUT ROWID;
'''
INSERT = 'INSERT OR IGNORE INTO records VALUES (' + ','.join(['?'] * len(HEADERS)) + ')'
TOTALS = 'COUNT(*), SUM(weight), SUM(income), SUM(system_cost), SUM(pickup_cost), SUM(operation_cost)'
PLAIN_NUMBER = re.compile(r'[+-]?\d+(?:\.\d+)?')
INVALID_XML = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')
FILE_ID = re.compile(r'\d{4}-\d{2}-\d{2}-[a-f0-9]{12}\.(csv|xlsx)')


class DailyReportError(ValueError):
    """An input or output issue that can be corrected by the operator."""


@dataclass(frozen=True)
class Percentage:
    value: Decimal

    def __str__(self):
        return f'{self.value:.2f}%'


def report_day(value=None):
    if value is None or value == '':
        return (datetime.now(REPORT_TIMEZONE).date() - timedelta(days=1)).isoformat()
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise DailyReportError('日报日期须为 YYYY-MM-DD。')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise DailyReportError('日报日期无效。') from error


def input_template():
    """A header-only, UTF-8 BOM input template suitable for Excel."""
    stream = io.StringIO(newline='')
    csv.writer(stream).writerow(HEADERS)
    return stream.getvalue().encode('utf-8-sig')


def clean(value, field, *, optional=False):
    text = (value or '').strip()
    if not text and not optional:
        raise DailyReportError(f'「{field}」缺失。')
    if len(text) > 32767 or INVALID_XML.search(text):
        raise DailyReportError(f'「{field}」包含无法写入报表的字符或过长。')
    return text


def integer_units(value, field, decimals):
    text = clean(value, field)
    if not PLAIN_NUMBER.fullmatch(text) or len(text) > 30:
        raise DailyReportError(f'「{field}」需要十进制数字。')
    whole, _, fraction = text.lstrip('+-').partition('.')
    if fraction[decimals:].strip('0'):
        raise DailyReportError(f'「{field}」最多支持 {decimals} 位小数。')
    numeric = int(whole) * 10 ** decimals + int(fraction[:decimals].ljust(decimals, '0'))
    if numeric > 2**63 - 1:
        raise DailyReportError(f'「{field}」超出数值范围。')
    return -numeric if text.startswith('-') else numeric


def normalized_row(row):
    org_kind = clean(row['组织类别'], '组织类别')
    if org_kind not in ('分部', '市场部'):
        raise DailyReportError('「组织类别」须为「分部」或「市场部」。')
    weight = integer_units(row['重量'], '重量', 6)
    if weight < 0:
        raise DailyReportError('「重量」须为非负千克数。')
    return (
        report_day(clean(row['结算日期'], '结算日期')),
        clean(row['运单号'], '运单号'), clean(row['客户'], '客户'),
        org_kind, clean(row['组织'], '组织'), weight,
        *(integer_units(row[field], field, 2) for field in ('收入不含税', '系统成本', '取件成本', '操作成本')),
        clean(row['重量段'], '重量段'), clean(row['流向'], '流向'),
        clean(row.get('来源文件'), '来源文件', optional=True) or None,
    )


def amount(value):
    return Decimal(value).scaleb(-2)


def rounded(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def metric_values(aggregates):
    tickets, weight_raw, income_raw, system_raw, pickup_raw, operation_raw = aggregates
    weight = Decimal(weight_raw).scaleb(-6)
    income, system, pickup, operation = map(amount, (income_raw, system_raw, pickup_raw, operation_raw))
    gross = income - system - pickup - operation
    return (tickets, rounded(weight), rounded(weight / tickets), income, system, pickup, operation,
            gross, rounded(gross * 100 / income) if income else None, rounded(gross / tickets))


def display(value, index):
    if value is None:
        return '—'
    return f'{value:.2f}%' if index == 8 else str(value)


def csv_value(value):
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return '' if value is None else value


def main_rows(db, start, day):
    yield ('类别', '性质', '费用项目', '项目', *(f'{number}日' for number in range(1, 32)))
    groups = {}
    for org_kind, org, settlement, *aggregates in db.execute(
        f'SELECT org_kind, org, day, {TOTALS} FROM records WHERE day BETWEEN ? AND ? '
        'GROUP BY org_kind, org, day ORDER BY org_kind, org, day', (start, day),
    ):
        groups.setdefault((org_kind, org), {})[int(settlement[-2:])] = metric_values(aggregates)
    for (org_kind, org), values in groups.items():
        for index, (group, label) in enumerate(zip(GROUPS, METRICS)):
            cells = [values[number][index] if number in values else None for number in range(1, 32)]
            if index == 8:
                cells = [Percentage(value) if value is not None else None for value in cells]
            yield (org_kind, org, group, label, *cells)


def dimension_rows(db, start, day, dimension, *, by_day=False, volume=False):
    label, field = ('重量段', 'weight_band') if dimension == 'weight' else ('流向', 'flow')
    dimensions = ('结算日期', '客户', label) if by_day else ('客户', label)
    yield (*dimensions, *(METRICS[:3] if volume else METRICS))
    group = f'day, customer, {field}' if by_day else f'customer, {field}'
    for row in db.execute(f'SELECT {group}, {TOTALS} FROM records WHERE day BETWEEN ? AND ? '
                          f'GROUP BY {group} ORDER BY {group}', (start, day)):
        n = len(dimensions)
        values = metric_values(row[n:])
        if volume:
            yield (*row[:n], *values[:3])
        else:
            percentage = Percentage(values[8]) if values[8] is not None else None
            yield (*row[:n], *values[:8], percentage, values[9])


XML_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'


def column_letter(index):
    letters = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord('A') + remainder) + letters
    return letters


def write_sheet(package, index, rows):
    with package.open(f'xl/worksheets/sheet{index}.xml', 'w', force_zip64=True) as stream:
        stream.write((f'<worksheet xmlns="{XML_NS}"><sheetViews><sheetView workbookViewId="0">'
                      '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                      '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="16"/>'
                      '<cols><col min="1" max="35" width="15" customWidth="1"/></cols>'
                      '<sheetData>').encode())
        for number, values in enumerate(rows, 1):
            if number > 1_048_576:
                raise DailyReportError('报表工作表超过 Excel 行数上限，请缩小输入范围。')
            cells = [f'<row r="{number}">']
            for column, value in enumerate(values, 1):
                if value is None:
                    continue
                ref = f'{column_letter(column)}{number}'
                style = ' s="1"' if number == 1 else ''
                if isinstance(value, Percentage):
                    cells.append(f'<c r="{ref}" s="3"><v>{value.value.scaleb(-2)}</v></c>')
                elif isinstance(value, Decimal):
                    cells.append(f'<c r="{ref}" s="2"><v>{value}</v></c>')
                elif isinstance(value, int):
                    cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
                                 f'{escape(str(value))}</t></is></c>')
            cells.append('</row>')
            stream.write(''.join(cells).encode('utf-8'))
        stream.write(b'</sheetData></worksheet>')


def write_workbook(path, db, start, day):
    ct_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    def rel(rel_id, kind, target):
        return f'<Relationship Id="{rel_id}" Type="{REL_NS}/{kind}" Target="{target}"/>'

    def types(part, kind):
        return f'<Override PartName="/{part}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml"/>'

    count = len(SHEETS)
    styles = (
        f'<styleSheet xmlns="{XML_NS}"><fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF28456A"/><bgColor indexed="64"/>'
        '</patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    def main_with_notes():
        yield from main_rows(db, start, day)
        yield ()
        yield (VERSION,)
        for note in ASSUMPTIONS:
            yield (note,)

    rows = (main_with_notes(), dimension_rows(db, start, day, 'weight'),
            dimension_rows(db, start, day, 'weight', by_day=True), dimension_rows(db, start, day, 'flow'),
            dimension_rows(db, start, day, 'flow', by_day=True),
            dimension_rows(db, start, day, 'weight', volume=True), dimension_rows(db, start, day, 'flow', volume=True))
    with ZipFile(path, 'w', ZIP_DEFLATED, compresslevel=5) as package:
        package.writestr('[Content_Types].xml', f'<Types xmlns="{ct_ns}">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         + types('xl/styles.xml', 'styles') + ''.join(types(f'xl/worksheets/sheet{i}.xml', 'worksheet')
                                                                   for i in range(1, count + 1)) + '</Types>')
        package.writestr('_rels/.rels', f'<Relationships xmlns="{PKG_REL_NS}">'
                         + rel('rId1', 'officeDocument', 'xl/workbook.xml') + '</Relationships>')
        package.writestr('xl/workbook.xml', f'<workbook xmlns="{XML_NS}" xmlns:r="{REL_NS}">'
                         '<bookViews><workbookView/></bookViews><sheets>'
                         + ''.join(f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
                                   for index, name in enumerate(SHEETS, 1)) + '</sheets></workbook>')
        package.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="{PKG_REL_NS}">'
                         + ''.join(rel(f'rId{i}', 'worksheet', f'worksheets/sheet{i}.xml')
                                   for i in range(1, count + 1)) + rel(f'rId{count + 1}', 'styles', 'styles.xml')
                         + '</Relationships>')
        package.writestr('xl/styles.xml', styles)
        for index, values in enumerate(rows, 1):
            write_sheet(package, index, values)


class DailyReportService:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve() / 'daily_report'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'normalized.sqlite3'
        self.exports = self.root / 'exports'
        self.lock = threading.RLock()
        self.import_lock = threading.Lock()

    input_template = staticmethod(input_template)

    def import_source(self, path, source_name):
        label = clean(Path(str(source_name).replace('\\', '/')).name, '来源文件')
        if Path(label).suffix.lower() != '.csv':
            raise DailyReportError('请上传按标准日报字段整理的 CSV。')
        if not self.import_lock.acquire(blocking=False):
            raise DailyReportError('已有日报明细正在导入。')
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='pending-', suffix='.sqlite3', delete=False) as pending:
                tmp = Path(pending.name)
            with closing(sqlite3.connect(tmp)) as db, db:
                db.executescript(SQL_SCHEMA)
                stats = self._load_csv(path, db)
                if not stats['rows']:
                    raise DailyReportError('没有可导入的结算明细。')
                first_day, last_day = db.execute('SELECT MIN(day), MAX(day) FROM records').fetchone()
                meta = {**stats, 'first_day': first_day, 'last_day': last_day, 'source': label}
                db.execute('INSERT INTO source_metadata VALUES (?)', (json.dumps(meta, ensure_ascii=False),))
            with self.lock:
                os.replace(tmp, self.path)
            return {'ok': True, **meta}
        except (UnicodeError, csv.Error) as error:
            raise DailyReportError('无法读取 CSV。请使用 UTF-8 编码并检查表格格式。') from error
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
            self.import_lock.release()

    @staticmethod
    def _load_csv(path, db):
        rows = duplicates = 0
        with Path(path).open('r', newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream)
            headers = [name.strip() for name in (reader.fieldnames or [])]
            missing = [field for field in REQUIRED if field not in headers]
            if missing:
                raise DailyReportError('日报明细缺少字段：' + '、'.join(missing) + '。')
            if len(set(headers)) != len(headers):
                raise DailyReportError('日报明细包含重复列名。')
            reader.fieldnames = headers
            for row in reader:
                if not any(row.values()):
                    continue
                try:
                    if None in row:
                        raise DailyReportError('数据列数超出表头。')
                    values = normalized_row(row)
                    if db.execute(INSERT, values).rowcount:
                        rows += 1
                    else:
                        existing = db.execute('SELECT * FROM records WHERE day=? AND waybill=?', values[:2]).fetchone()
                        if existing[:-1] != values[:-1]:
                            raise DailyReportError('同一结算日期及运单号的明细存在冲突。请在输入表中核对。')
                        duplicates += 1
                    if values[-1]:
                        db.execute('INSERT OR IGNORE INTO daily_sources VALUES (?, ?)', (values[0], values[-1]))
                except DailyReportError as error:
                    raise DailyReportError(f'CSV 第 {reader.line_num} 行：{error}') from error
        return {'rows': rows, 'duplicates': duplicates}

    def _metadata(self, db, day):
        meta = json.loads(db.execute('SELECT value FROM source_metadata').fetchone()[0])
        hit = db.execute('SELECT COUNT(*) FROM records WHERE day = ?', (day,)).fetchone()[0]
        return {'day': day, 'state': 'ready' if hit else 'missing_day', 'ready': bool(hit),
                'missing': [] if hit else [f'标准日报明细中没有 {day} 的结算记录。'], 'day_rows': hit,
                'coverage': {key: meta[key] for key in ('first_day', 'last_day', 'rows', 'duplicates')},
                'source': meta['source'], 'version': VERSION, 'official_template_ready': False}

    def status(self, day=None):
        day = report_day(day)
        with self.lock:
            if not self.path.is_file():
                return {'day': day, 'state': 'missing_source', 'ready': False, 'missing': SOURCE_GAPS.copy(),
                        'required_fields': list(REQUIRED), 'version': VERSION, 'official_template_ready': False,
                        'available_formats': ['csv', 'xlsx']}
            with closing(sqlite3.connect(self.path)) as db:
                return self._metadata(db, day)

    def _preview(self, day, db):
        status = self._metadata(db, day)
        if not status['ready']:
            return self._blocked(status)
        totals = db.execute(f'SELECT {TOTALS} FROM records WHERE day = ?', (day,)).fetchone()
        groups = [('合计', '全部组织', *totals)]
        groups.extend(db.execute(f'SELECT org_kind, org, {TOTALS} FROM records WHERE day=? '
                                 'GROUP BY org_kind, org ORDER BY org_kind, org', (day,)))
        rows = [[kind, org, *(display(value, index) for index, value in enumerate(metric_values(values)))]
                for kind, org, *values in groups]
        sources = [status['source'], *(row[0] for row in db.execute('SELECT source FROM daily_sources '
                                                                 'WHERE day=? '
                                                                 'ORDER BY source LIMIT 100', (day,)))]
        return {'state': 'ready', 'ready': True, 'day': day, 'status': status, 'answer': f'{day} 结算日报技术验证。',
                'result': {'title': '经营情况汇总（技术验证版）', 'columns': ['组织类别', '组织', *METRICS],
                           'rows': rows, 'notes': ASSUMPTIONS.copy(), 'sources': list(dict.fromkeys(sources))}}

    @staticmethod
    def _blocked(status):
        return {'state': status['state'], 'ready': False, 'day': status['day'], 'status': status,
                'missing': status['missing'], 'answer': '日报数据未就绪。' + ''.join(status['missing']),
                'exports': []}

    def preview(self, day=None):
        day = report_day(day)
        with self.lock:
            if not self.path.is_file():
                return self._blocked(self.status(day))
            with closing(sqlite3.connect(self.path)) as db:
                return self._preview(day, db)

    def generate(self, day=None):
        day = report_day(day)
        with self.lock:
            if not self.path.is_file():
                return self._blocked(self.status(day))
            with closing(sqlite3.connect(self.path)) as db:
                report = self._preview(day, db)
                if report['ready']:
                    report['exports'] = self._write_outputs(db, day)
        return report

    def _write_outputs(self, db, day):
        start = day[:7] + '-01'
        base = f'{day}-{uuid.uuid4().hex[:12]}'
        csv_name, xlsx_name = f'{base}.csv', f'{base}.xlsx'
        self.exports.mkdir(parents=True, exist_ok=True)
        created = datetime.now(REPORT_TIMEZONE).isoformat(timespec='seconds')
        files = [{'name': f'技术验证版_每日经营情况_{day[:7]}.csv', 'filename': csv_name, 'format': 'csv'},
                 {'name': f'技术验证版_经营日报_{day}.xlsx', 'filename': xlsx_name, 'format': 'xlsx'}]
        with tempfile.TemporaryDirectory(dir=self.exports, prefix='pending-') as staging:
            tmp = Path(staging)
            with (tmp / csv_name).open('w', newline='', encoding='utf-8-sig') as stream:
                writer = csv.writer(stream)
                writer.writerows(tuple(csv_value(value) for value in row) for row in main_rows(db, start, day))
            write_workbook(tmp / xlsx_name, db, start, day)
            meta_name = f'{base}.json'
            (tmp / meta_name).write_text(json.dumps({'day': day, 'created': created, 'exports': files}, ensure_ascii=False),
                                         encoding='utf-8')
            published = []
            try:
                for filename in (csv_name, xlsx_name, meta_name):
                    path = self.exports / filename
                    (tmp / filename).replace(path)
                    published.append(path)
            except OSError:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
        return files

    def history(self, limit=20):
        if not self.exports.is_dir():
            return []
        records = sorted(self.exports.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        return [json.loads(path.read_text(encoding='utf-8')) for path in records[:limit]]

    def download_path(self, filename):
        if not isinstance(filename, str) or not FILE_ID.fullmatch(filename):
            raise DailyReportError('无效的日报文件。')
        path = self.exports / filename
        if not path.is_file() or path.resolve().parent != self.exports.resolve():
            raise DailyReportError('未找到日报文件。')
        return path
