"""Compare historical outbound billing and cost by waybill using bounded SQLite queries."""

import csv
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from daily_report import (FILE_ID, PKG_REL_NS, REL_NS, XML_NS, DailyReportError,
                          amount, csv_value, report_day, write_sheet)


VERSION = '历史数据验证 v1'
TITLE = '出港日报（历史数据验证）'
RULES = (
    '采用文档开头示例的收入侧方向，以客户账单「计费时间」作为 T 日，按运单号匹配成本「成功计费时间」T−5 至 T（含首尾）。文档正文第一节给出的反向口径需确认。',
    '客户账单金额取原列「运费」。重复运单的客户和金额一致时只计一次，冲突或字段缺失时保留记录并排除该运单的收入及匹配成本。',
    '发件成本金额取窗口内唯一匹配记录的原列「总金额」。同单号出现多条候选成本时全部待核对，金额不纳入合计。缺失金额不作零处理。',
    '该报表用于验证历史数据关联。正式结算日期、不含税收入、重算成本、分项成本、毛利和正式模板仍需确认。',
)
COLUMNS = ('结算对象', '账单运单数', '已匹配成本', '窗口内无成本', '账单冲突或缺字段', '成本有多条候选', '成本候选记录数',
           '缺金额的唯一成本记录数', '账单运费（元）', '发件成本原总金额（元）')
DETAIL_COLUMNS = ('运单号', '结算对象', '匹配状态', '账单运费（元）', '成本候选记录数', '缺金额的候选记录数',
                  '首条成本计费日', '末条成本计费日', '发件成本原总金额（元）')
SUMMARY_SQL = '''COUNT(*), SUM(cost_rows = 1), SUM(cost_rows IS NULL AND invalid = 0),
                 SUM(invalid), SUM(cost_rows > 1), SUM(cost_rows), SUM(cost_rows = 1 AND cost_amount_rows = 0),
                 SUM(CASE WHEN invalid = 0 THEN freight_cents END), SUM(CASE WHEN cost_rows = 1 THEN cost_cents END)'''
DETAIL_SHEET_LIMIT = 1_048_575


def _db_uri(path):
    return path.resolve().as_uri() + '?mode=ro'


def _extreme_day(db, table, *, latest=False, start=None, end=None):
    clauses, values = ['day IS NOT NULL'], []
    if start:
        clauses.append('day >= ?')
        values.append(start)
    if end:
        clauses.append('day <= ?')
        values.append(end)
    sql = f'SELECT day FROM {table} WHERE ' + ' AND '.join(clauses)
    row = db.execute(sql + (' ORDER BY day DESC LIMIT 1' if latest else ' ORDER BY day LIMIT 1'), values).fetchone()
    return row[0] if row else None


def _range(first, last):
    return f'{first} 至 {last}' if first and last else '无有效计费日期'


def _window(day):
    return (date.fromisoformat(day) - timedelta(days=5)).isoformat(), day


def _summary_row(label, values):
    return [label, *(item or 0 for item in values[:-2]),
            *(amount(item) if item is not None else None for item in values[-2:])]


def _display_row(row):
    return [item if isinstance(item, (str, int)) else '—' if item is None else f'{item:.2f}' for item in row]


def _detail_rows(db):
    for waybill, party, invalid, freight, n, money_n, first_day, last_day, cost in db.execute(
        'SELECT waybill, party, invalid, freight_cents, cost_rows, cost_amount_rows, cost_first, cost_last, cost_cents '
        'FROM outbound_detail ORDER BY party, waybill',
    ):
        if invalid:
            state = '账单冲突或缺字段'
        elif n is None:
            state = '窗口内无成本'
        else:
            state = '成本有多条候选' if n > 1 else '已匹配'
        yield (waybill, party or None, state, amount(freight) if not invalid else None, n, n - money_n if n else None,
               first_day, last_day, amount(cost) if n == 1 and cost is not None else None)


def _workbook(path, sheets):
    ct_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    sheet_rel = f'{REL_NS}/worksheet'
    sheet_ct = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
    indices = range(1, len(sheets) + 1)
    styles = (f'<styleSheet xmlns="{XML_NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FF28456A"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
              '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    with ZipFile(path, 'w', ZIP_DEFLATED, compresslevel=3) as package:
        package.writestr('[Content_Types].xml', f'<Types xmlns="{ct_ns}">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                         + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{sheet_ct}"/>' for i in indices)
                         + '</Types>')
        package.writestr('_rels/.rels', f'<Relationships xmlns="{PKG_REL_NS}">'
                         f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
                         '</Relationships>')
        package.writestr('xl/workbook.xml', f'<workbook xmlns="{XML_NS}" xmlns:r="{REL_NS}">'
                         '<bookViews><workbookView/></bookViews><sheets>'
                         + ''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                                   for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        package.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="{PKG_REL_NS}">'
                         + ''.join(f'<Relationship Id="rId{i}" Type="{sheet_rel}" Target="worksheets/sheet{i}.xml"/>' for i in indices)
                         + f'<Relationship Id="rId{len(sheets) + 1}" Type="{REL_NS}/styles" Target="styles.xml"/>'
                         '</Relationships>')
        package.writestr('xl/styles.xml', styles)
        for index, (_, rows) in enumerate(sheets, 1):
            write_sheet(package, index, rows)


class OutboundReportService:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.bill = self.root / 'outbound_bill.sqlite3'
        self.current_bill = self.root / 'bill.sqlite3'
        self.cost = self.root / 'cost.sqlite3'
        self.exports = self.root / 'daily_report' / 'outbound_exports'

    @staticmethod
    def _revision(path):
        with closing(sqlite3.connect(_db_uri(path), uri=True)) as db:
            row = db.execute('SELECT value FROM import_info').fetchone()
            if row is None:
                raise ValueError('账单导入记录为空。')
            return json.loads(row[0]).get('bill_revision')

    def available(self):
        if not all(path.is_file() for path in (self.bill, self.current_bill, self.cost)):
            return False
        try:
            return self._revision(self.bill) == self._revision(self.current_bill)
        except (OSError, ValueError, AttributeError, sqlite3.Error):
            return False

    def _connect(self):
        connection = sqlite3.connect(_db_uri(self.bill), uri=True)
        try:
            connection.execute('ATTACH DATABASE ? AS cost', (_db_uri(self.cost),))
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    @staticmethod
    def _coverage(db):
        bill_first = _extreme_day(db, 'main.records')
        bill_last = _extreme_day(db, 'main.records', latest=True)
        cost_first = _extreme_day(db, 'cost.records')
        cost_last = _extreme_day(db, 'cost.records', latest=True)
        first = last = None
        if bill_first and cost_first:
            cost_end = (date.fromisoformat(cost_last) + timedelta(days=5)).isoformat()
            first = _extreme_day(db, 'main.records', start=cost_first, end=cost_end)
            last = _extreme_day(db, 'main.records', latest=True, start=cost_first, end=cost_end)
        rows = db.execute('SELECT COUNT(*) FROM main.records WHERE day BETWEEN ? AND ?', (first, last)).fetchone()[0]
        return {'first_day': first, 'last_day': last, 'rows': rows, 'bill_first_day': bill_first,
                'bill_last_day': bill_last, 'cost_first_day': cost_first, 'cost_last_day': cost_last}

    def coverage(self):
        if not self.available():
            return None
        with closing(self._connect()) as db:
            coverage = self._coverage(db)
            days = db.execute('SELECT DISTINCT day FROM main.records WHERE day BETWEEN ? AND ? ORDER BY day DESC',
                              (coverage['first_day'], coverage['last_day']))
            coverage['last_matched_day'] = next((day for (day,) in days if db.execute(
                'SELECT 1 FROM main.records AS b CROSS JOIN cost.records AS c '
                'ON c.waybill=b.waybill AND c.day BETWEEN ? AND ? WHERE b.day=? LIMIT 1',
                (*_window(day), day)).fetchone()), None)
        return coverage if coverage['rows'] else None

    @staticmethod
    def _prepare(db, day, start):
        db.execute('''CREATE TEMP TABLE outbound_day AS
                      SELECT waybill, CASE WHEN MIN(COALESCE(party, '')) = MAX(COALESCE(party, '')) THEN MIN(party) END AS party,
                             MIN(freight_cents) AS freight_cents,
                             (MIN(COALESCE(party, '')) <> MAX(COALESCE(party, '')) OR MIN(COALESCE(party, '')) = ''
                              OR COUNT(freight_cents) <> COUNT(*) OR MIN(freight_cents) <> MAX(freight_cents)) AS invalid,
                             COUNT(*) AS bill_rows
                      FROM main.records WHERE day=? AND waybill IS NOT NULL AND waybill<>'' GROUP BY waybill''', (day,))
        db.execute('CREATE UNIQUE INDEX temp.outbound_waybill ON outbound_day(waybill)')
        db.execute('''CREATE TEMP TABLE outbound_cost AS
                      SELECT d.waybill, COUNT(*) AS cost_rows, COUNT(c.amount_cents) AS cost_amount_rows,
                             CASE WHEN COUNT(*) = 1 THEN MIN(c.amount_cents) END AS cost_cents,
                             MIN(c.day) AS cost_first, MAX(c.day) AS cost_last
                      FROM temp.outbound_day AS d CROSS JOIN cost.records AS c
                      ON c.waybill=d.waybill AND c.day BETWEEN ? AND ?
                      WHERE d.invalid=0 GROUP BY d.waybill''', (start, day))
        db.execute('CREATE UNIQUE INDEX temp.outbound_cost_waybill ON outbound_cost(waybill)')
        db.execute('''CREATE TEMP TABLE outbound_detail AS
                      SELECT d.waybill, d.party, d.freight_cents, d.invalid, c.cost_rows, c.cost_amount_rows,
                             c.cost_cents, c.cost_first, c.cost_last
                      FROM temp.outbound_day AS d LEFT JOIN temp.outbound_cost AS c ON d.waybill=c.waybill''')

    @staticmethod
    def _outcome(day, state, missing, coverage=None):
        status = {'day': day, 'state': state, 'ready': state == 'ready', 'missing': missing,
                  'version': VERSION, 'report_kind': 'outbound_history', 'official_template_ready': False}
        if coverage is not None:
            status['coverage'] = coverage
        return {'day': day, 'state': state, 'ready': status['ready'], 'report_kind': 'outbound_history',
                'status': status, 'answer': ' '.join(missing), 'missing': missing, 'exports': []}

    def generate(self, day=None):
        return self._report(day, export=True)

    def preview(self, day=None):
        return self._report(day, export=False)

    def _report(self, day, *, export):
        day = report_day(day)
        if not self.available():
            return self._outcome(day, 'missing_source', ['缺少与当前账单版本对应的客户账单运费索引，或缺少发件成本索引。'])
        start, end = _window(day)
        with closing(self._connect()) as db:
            coverage = self._coverage(db)
            day_rows = db.execute('SELECT COUNT(*) FROM main.records WHERE day=?', (day,)).fetchone()[0]
            if not day_rows:
                return self._outcome(day, 'missing_day', [f'客户账单没有 {day} 的计费记录。'], coverage)
            cost_window_days = {row[0] for row in db.execute('SELECT DISTINCT day FROM cost.records WHERE day BETWEEN ? AND ?',
                                                              (start, end))}
            if not cost_window_days:
                return self._outcome(day, 'missing_day', [f'发件成本在 {start} 至 {end} 没有已导入记录。'], coverage)
            self._prepare(db, day, start)
            total = _summary_row('合计', db.execute(f'SELECT {SUMMARY_SQL} FROM temp.outbound_detail').fetchone())
            unmatched_blank = db.execute("SELECT COUNT(*) FROM main.records WHERE day=? AND (waybill IS NULL OR waybill='')",
                                         (day,)).fetchone()[0]
            repeated = db.execute('SELECT COALESCE(SUM(bill_rows-1),0) FROM temp.outbound_day').fetchone()[0]
            group_rows = [_summary_row(label, values) for label, *values in db.execute(
                f'SELECT COALESCE(party, ?), {SUMMARY_SQL} FROM temp.outbound_detail '
                'GROUP BY COALESCE(party, ?) ORDER BY 1', ('客户不明', '客户不明'))]
            absent = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(6)]
            absent = [item for item in absent if item not in cost_window_days]
            notes = [f'账单 T 日为 {day}。成本查询区间为 {start} 至 {end}。',
                     f'已导入账单覆盖 {_range(coverage["bill_first_day"], coverage["bill_last_day"])}，'
                     f'已导入成本覆盖 {_range(coverage["cost_first_day"], coverage["cost_last_day"])}。',
                     f'账单 T 日有 {day_rows} 条原始记录，其中 {repeated} 条与已有运单号重复，'
                     f'{unmatched_blank} 条没有运单号。', *RULES]
            if absent:
                notes.insert(2, '成本窗口内以下日期无已导入记录：' + '、'.join(absent) + '。')
            if not total[2]:
                notes.insert(3, '本日没有匹配的有效成本记录，成本金额留空。')
            if total[-3]:
                notes.append('缺金额的唯一成本记录已计入匹配记录数，未计入金额合计。')
            sources = self._sources(db, start, day)
            outcome = self._outcome(day, 'ready', [], coverage)
            outcome['status']['day_rows'] = day_rows
            outcome['answer'] = f'{day} 出港日报历史验证' + ('已生成。' if export else '预览。')
            if len(group_rows) > 100:
                notes.append('网页仅展示前 100 个结算对象，文件包含全部汇总。')
            summary = [total, *group_rows]
            outcome['result'] = {'title': TITLE, 'columns': list(COLUMNS),
                                 'rows': [_display_row(row) for row in summary[:101]],
                                 'notes': notes, 'sources': sources, 'basis_version': VERSION}
            if export:
                outcome['exports'] = self._write_outputs(db, day, summary, notes, sources)
        return outcome

    @staticmethod
    def _sources(db, start, day):
        bill = ['客户账单 · 运费 / 计费时间']
        meta = db.execute('SELECT value FROM main.import_info').fetchone()
        if meta:
            info = json.loads(meta[0])
            source = info.get('name') or info.get('source')
            if isinstance(source, str) and source.strip():
                bill = [f'{source} · 运费 / 计费时间']
        cost = [f'{name} · {amount_field} / {date_field}' for name, amount_field, date_field in db.execute(
            '''SELECT s.name, s.amount_field, s.date_field FROM cost.sources AS s JOIN
               (SELECT DISTINCT c.file_id FROM cost.records AS c WHERE c.day BETWEEN ? AND ?) AS w
               ON s.id=w.file_id ORDER BY s.id''', (start, day))]
        return [*bill, *cost]

    def _write_outputs(self, db, day, summary, notes, sources):
        basename = f'{day}-{uuid.uuid4().hex[:12]}'
        csv_name, xlsx_name = f'{basename}.csv', f'{basename}.xlsx'
        self.exports.mkdir(parents=True, exist_ok=True)
        exports = [{'name': f'出港日报_历史数据验证_{day}.{extension}', 'filename': filename,
                    'format': extension, 'url': f'/api/local/reports/outbound-file?name={filename}'}
                   for extension, filename in [('csv', csv_name), ('xlsx', xlsx_name)]]
        note_rows = [('类别', '内容'), ('口径版本', VERSION), ('报表日期', day),
                     *(('说明', item) for item in notes), *(('来源', item) for item in sources)]

        def details():
            yield from _detail_rows(db)

        def details_part(iterator):
            yield DETAIL_COLUMNS
            for _ in range(DETAIL_SHEET_LIMIT):
                row = next(iterator, None)
                if row is None:
                    break
                yield row

        iterator = details()
        part_count = max(1, (summary[0][1] + DETAIL_SHEET_LIMIT - 1) // DETAIL_SHEET_LIMIT)
        sheets = [('出港汇总', [COLUMNS, *summary]),
                  *((f'运单对照{i}' if part_count > 1 else '运单对照', details_part(iterator))
                    for i in range(1, part_count + 1)), ('口径与来源', note_rows)]
        with tempfile.TemporaryDirectory(dir=self.exports, prefix='pending-') as scratch:
            staging = Path(scratch)
            with (staging / csv_name).open('w', newline='', encoding='utf-8-sig') as stream:
                writer = csv.writer(stream)
                writer.writerow([TITLE, day])
                writer.writerow(COLUMNS)
                writer.writerows(tuple(csv_value(cell) for cell in row) for row in summary)
                writer.writerow([])
                writer.writerows(tuple(csv_value(cell) for cell in row) for row in note_rows)
            _workbook(staging / xlsx_name, sheets)
            published = []
            try:
                for filename in (csv_name, xlsx_name):
                    path = self.exports / filename
                    os.replace(staging / filename, path)
                    published.append(path)
            except OSError:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
        return exports

    def download_path(self, filename):
        if not isinstance(filename, str) or not FILE_ID.fullmatch(filename):
            raise DailyReportError('无效的出港日报文件。')
        path = self.exports / filename
        if not path.is_file() or path.resolve().parent != self.exports.resolve():
            raise DailyReportError('未找到出港日报文件。')
        return path
