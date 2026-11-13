"""Stream local shipment ledgers into a per-import SQLite database."""

import codecs
from contextlib import contextmanager
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sqlite3
import tempfile
import zipfile

from lxml import etree


FIELDS = {
    'bill': ('计费时间', '账单结算对象名称', '运单号码', '应收金额'),
    'cost': ('成功计费时间', '客户名称', '运单号', '总金额'),
}
XLSX = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
RELATIONSHIPS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
ROW, CELL = XLSX + 'row', XLSX + 'c'
VALUE, INLINE, TEXT = XLSX + 'v', XLSX + 'is', XLSX + 't'
COLUMN_REF = re.compile(r'^([A-Z]+)[0-9]+$')
POI_ROW = re.compile(rb'<row r="([0-9]+)"(?:\s|>)')
XML_WRAPPER = b'<projected xmlns="' + XLSX[1:-1].encode('ascii') + b'">'
XML_WRAPPER_END = b'</projected>'
PLAIN_AMOUNT = re.compile(r'^([+-]?)(\d+)(?:\.(\d{1,2}))?$')
COMMA_AMOUNT = re.compile(r'^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$')
DECIMAL_AMOUNT = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$')
SUMMARY_LABEL = re.compile(r'共计\s*[:：]\s*([0-9]+)\s*条')
SUPPORTED = {'.zip', '.xlsx', '.csv'}
BATCH = 10_000


@contextmanager
def archive_member(archive, item, temp_dir):
    with tempfile.TemporaryFile(dir=temp_dir) as local_file:
        with archive.open(item) as compressed:
            shutil.copyfileobj(compressed, local_file, 1024 * 1024)
        local_file.seek(0)
        yield local_file


def find_tables(stream, name, temp_dir, depth=0):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == '.csv':
        yield name, '', csv_rows(stream), None
        return
    if suffix == '.xlsx':
        yield from workbook_tables(stream, name)
        return
    if depth >= 8:
        raise ValueError('ZIP 嵌套超过 8 层。')
    with zipfile.ZipFile(stream) as archive:
        for item in archive.infolist():
            member = PurePosixPath(item.filename)
            if item.is_dir() or member.name.startswith(('._', '~$')) or '__MACOSX' in member.parts:
                continue
            ext = member.suffix.lower()
            if ext not in SUPPORTED:
                continue
            member_name = f'{name}/{item.filename}'
            if ext == '.csv':
                with archive.open(item) as entry:
                    yield from find_tables(entry, member_name, temp_dir, depth + 1)
            else:
                with archive_member(archive, item, temp_dir) as entry:
                    yield from find_tables(entry, member_name, temp_dir, depth + 1)


def csv_rows(stream):
    reader = io.BufferedReader(stream)
    prefix = reader.peek(8192)[:8192]
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    else:
        try:
            codecs.getincrementaldecoder('utf-8-sig')().decode(prefix, final=False)
            encoding = 'utf-8-sig'
        except UnicodeDecodeError:
            encoding = 'gb18030'
    wrapper = io.TextIOWrapper(reader, encoding=encoding, newline='')
    try:
        for values in csv.reader(wrapper):
            yield values
    finally:
        wrapper.detach()


def xml_document(archive, member):
    options = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    with archive.open(member) as stream:
        return etree.parse(stream, options).getroot()


def workbook_tables(stream, filename):
    with zipfile.ZipFile(stream) as workbook:
        manifest = xml_document(workbook, 'xl/workbook.xml')
        rels = xml_document(workbook, 'xl/_rels/workbook.xml.rels')
        targets = {
            item.get('Id'): item.get('Target') for item in rels
            if item.get('TargetMode') != 'External'
            and item.get('Type', '').endswith('/worksheet')
        }
        properties = manifest.find(XLSX + 'workbookPr')
        date_base = date(1904, 1, 1) if properties is not None and properties.get('date1904') in {'1', 'true'} else date(1899, 12, 30)
        strings = []
        is_poi = ('docProps/app.xml' in workbook.namelist()
                  and b'<Application>Apache POI</Application>' in workbook.read('docProps/app.xml'))
        if 'xl/sharedStrings.xml' in workbook.namelist():
            with workbook.open('xl/sharedStrings.xml') as table:
                for node in xml_elements(table, XLSX + 'si'):
                    strings.append(''.join(node.itertext(tag=TEXT, with_tail=False)))
        for sheet in manifest.iter(XLSX + 'sheet'):
            target = targets.get(sheet.get(RELATIONSHIPS + 'id'))
            if not target:
                continue
            location = posixpath.normpath(posixpath.join('xl', target)) if not target.startswith('/') else target.lstrip('/')
            with workbook.open(location) as table:
                yield filename, sheet.get('name', ''), sheet_rows(table, strings, is_poi), date_base


def xml_elements(stream, tag):
    # Exported workbooks often declare A1 as their used range. Stream actual rows.
    for _, node in etree.iterparse(stream, events=('end',), tag=tag, resolve_entities=False, load_dtd=False, no_network=True):
        yield node
        node.clear()
        parent = node.getparent()
        if parent is not None:
            while node.getprevious() is not None:
                del parent[0]


def cell_value(cell, strings):
    if cell is None:
        return None
    cell_type = cell.get('t')
    if cell_type == 'inlineStr':
        value = cell.find(INLINE)
        return ''.join(value.itertext(tag=TEXT, with_tail=False)) if value is not None else None
    value = cell.findtext(VALUE)
    if cell_type == 's' and value:
        return strings[int(value)]
    return None if cell_type == 'e' else value


def sheet_rows(table, strings, is_poi):
    # Apache POI's large inline-string exports can be projected without
    # materializing the dozens of unused cells in each row.
    if is_poi:
        first_block = table.read(256 * 1024)
        marker = first_block.find(b'<sheetData>')
        first_end = first_block.find(b'</row>', marker) if marker >= 0 else -1
        if b'<dimension ref="A1"/>' in first_block[:marker] and first_end >= 0:
            first_start = marker + len(b'<sheetData>')
            first_row = first_block[first_start:first_end + len(b'</row>')].strip()
            if POI_ROW.match(first_row):
                yield projected_node(first_row), strings
                yield from poi_rows(table, first_block[first_end + len(b'</row>'):], strings)
                return
        table.seek(0)
    for row in xml_elements(table, ROW):
        yield row, strings


def projected_node(raw_xml):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    return etree.fromstring(XML_WRAPPER + raw_xml + XML_WRAPPER_END, parser)[0]


def poi_rows(table, remainder, strings):
    previous_number = 0
    while True:
        pieces = remainder.split(b'</row>')
        remainder = pieces.pop()
        for piece in pieces:
            row = piece.lstrip()
            match = POI_ROW.match(row)
            if match is None or int(match[1]) <= previous_number:
                raise ValueError('XLSX 中发现无法识别的工作表行。')
            previous_number = int(match[1])
            yield (row, match[1]), strings
        chunk = table.read(4 * 1024 * 1024)
        if not chunk:
            if not remainder.lstrip().startswith(b'</sheetData>') or b'</worksheet>' not in remainder:
                raise ValueError('XLSX 工作表未完整读取。')
            return
        remainder += chunk


def poi_value(row, reference, strings):
    start = row.find(reference)
    if start < 0:
        return None
    attributes_end = row.find(b'>', start + len(reference))
    if attributes_end < 0:
        raise ValueError('XLSX 单元格标签不完整。')
    if row[attributes_end - 1:attributes_end] == b'/':
        return None
    close = row.find(b'</c>', attributes_end)
    if close < 0:
        raise ValueError('XLSX 单元格未闭合。')
    content = row[attributes_end + 1:close]
    if content.startswith(b'<v>') and content.endswith(b'</v>'):
        value = content[3:-4]
        attributes = row[start:attributes_end]
        if (b'<' not in value and b'&' not in value
                and (b' t="' not in attributes or b' t="n"' in attributes)):
            return value.decode('utf-8')
    if content.startswith(b'<is><t') and content.endswith(b'</t></is>'):
        value = content[content.find(b'>', 5) + 1:-9]
        if b'<' not in value and b'&' not in value:
            return value.decode('utf-8')
    return cell_value(projected_node(row[start:close + len(b'</c>')]), strings)


def clean(value):
    return str(value).strip().lstrip('\ufeff').strip() if value is not None else ''


def find_positions(heading, fields, source):
    positions = []
    for field in fields:
        hits = [i for i, column in enumerate(heading) if clean(column) == field]
        if len(hits) != 1:
            problem = '缺少' if not hits else '重复出现'
            raise ValueError(f'{source} 表头{problem}“{field}”。请核对文件类型和表头。')
        positions.append(hits[0])
    return positions


def csv_values(rows, first, fields, source):
    positions = find_positions(first, fields, source)
    for values in rows:
        yield tuple(values[i] if i < len(values) else None for i in positions)


def worksheet_values(rows, first, fields, source):
    head, strings = first
    cells = head.findall(CELL)
    indexes = find_positions([cell_value(c, strings) for c in cells], fields, source)
    columns = [COLUMN_REF.fullmatch(cells[i].get('r', '')).group(1) for i in indexes]
    poi_refs = [b'<c r="' + column.encode('ascii') for column in columns]
    # Verify the prior cell position against its reference. Sparse rows fall back
    # to their explicit column references and update those position hints.
    for row, _ in rows:
        if isinstance(row, tuple):
            raw_row, number = row
            yield tuple(poi_value(raw_row, reference + number + b'"', strings) for reference in poi_refs)
            continue
        number = row.get('r', '')
        refs = [column + number for column in columns]
        values = []
        remaining = []
        cell_count = len(row)
        for i, (position, ref) in enumerate(zip(indexes, refs)):
            cell = row[position] if position < cell_count else None
            if cell is not None and cell.get('r') == ref:
                values.append(cell_value(cell, strings))
            else:
                values.append(None)
                remaining.append(i)
        if remaining:
            lookup = {refs[i]: i for i in remaining}
            for position, cell in enumerate(row.iterchildren(CELL)):
                match = lookup.pop(cell.get('r'), None)
                if match is not None:
                    indexes[match] = position
                    values[match] = cell_value(cell, strings)
                    if not lookup:
                        break
        yield values


def to_day(value, base):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10].replace('/', '-')).isoformat()
    except ValueError:
        if base is None or not re.fullmatch(r'\d{4,6}(?:\.\d+)?', value):
            return None
        days = int(value.partition('.')[0])
        return (base + timedelta(days=days)).isoformat() if days < 110_000 else None


def to_cents(value):
    if not value:
        return None, False, False
    if ',' in value:
        if not COMMA_AMOUNT.fullmatch(value):
            return None, False, True
        value = value.replace(',', '')
    match = PLAIN_AMOUNT.fullmatch(value)
    if match and len(match[2]) <= 15:
        fraction = (match[3] or '').ljust(2, '0')
        cents = int(match[2]) * 100 + int(fraction)
        return (-cents if match[1] == '-' else cents), False, False
    if not DECIMAL_AMOUNT.fullmatch(value):
        return None, False, True
    try:
        numeric = Decimal(value)
        if not numeric.is_finite() or abs(numeric) >= Decimal('1e15'):
            return None, False, True
        raw_cents = numeric * 100
        cents = raw_cents.to_integral_value(rounding=ROUND_HALF_UP)
        return int(cents), raw_cents != cents, False
    except InvalidOperation:
        return None, False, True


SCHEMA = '''
CREATE TABLE records(day TEXT, party TEXT, waybill TEXT, amount_cents INTEGER, file_id INTEGER NOT NULL);
CREATE TABLE sources(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, sheet TEXT NOT NULL,
    amount_field TEXT NOT NULL, date_field TEXT NOT NULL, party_field TEXT NOT NULL,
    rows INTEGER NOT NULL
);
'''


def import_dataset(kind, path, db_path, progress, *, display_name=None):
    """Import a ZIP, CSV, or XLSX. The caller owns publishing this staging DB."""
    fields = FIELDS[kind]
    path, db_path = Path(path), Path(db_path)
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError('请选择 ZIP、CSV 或 XLSX 文件。')
    label = PurePosixPath(str(display_name).replace('\\', '/')).name if display_name else path.name
    if PurePosixPath(label).suffix.lower() != path.suffix.lower():
        label = path.name
    stats = dict(files=0, rows=0, skipped=0, missing_amounts=0, invalid_amounts=0,
                 missing_dates=0, missing_parties=0, missing_waybills=0, rounded_amounts=0,
                 summary_rows=0, reconciled_summary_rows=0, summary_mismatches=0)
    months = set()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with path.open('rb') as root, conn:
            for name, sheet, rows, base in find_tables(root, label, db_path.parent):
                source = f'{name} / {sheet}' if sheet else name
                progress({**stats, 'phase': 'reading', 'source': source})
                first = next(rows, None)
                if first is None:
                    continue
                values = worksheet_values(rows, first, fields, source) if sheet else csv_values(rows, first, fields, source)
                file_id = stats['files'] + 1
                file_rows, file_cents, batch, summary_rows = 0, 0, [], []
                for record in values:
                    raw_day, party, waybill, amount = map(clean, record)
                    if not (raw_day or party or waybill or amount):
                        stats['skipped'] += 1
                        continue
                    day = to_day(raw_day, base)
                    cents, rounded, invalid = to_cents(amount)
                    footer = SUMMARY_LABEL.fullmatch(waybill) if not raw_day and not party else None
                    if footer:
                        summary_rows.append((int(footer[1]), cents))
                        stats['summary_rows'] += 1
                        stats['skipped'] += 1
                        continue
                    stats['missing_dates'] += day is None
                    stats['missing_parties'] += not party
                    stats['missing_waybills'] += not waybill
                    stats['missing_amounts'] += cents is None
                    stats['invalid_amounts'] += invalid
                    stats['rounded_amounts'] += rounded
                    if day:
                        months.add(day[:7])
                    if cents is not None:
                        file_cents += cents
                    batch.append((day, party or None, waybill or None, cents, file_id))
                    if len(batch) >= BATCH:
                        conn.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?)', batch)
                        stats['rows'] += len(batch)
                        file_rows += len(batch)
                        batch.clear()
                        progress({**stats, 'phase': 'reading', 'source': source})
                conn.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?)', batch)
                stats['rows'] += len(batch)
                file_rows += len(batch)
                for reported_rows, reported_cents in summary_rows:
                    if (reported_rows, reported_cents) == (file_rows, file_cents):
                        stats['reconciled_summary_rows'] += 1
                    else:
                        stats['summary_mismatches'] += 1
                conn.execute('INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (file_id, name, sheet, fields[3], fields[0], fields[1], file_rows))
                stats['files'] += 1
        if not stats['rows']:
            raise ValueError('文件中没有可导入的记录。')
        progress({**stats, 'phase': 'indexing'})
        with conn:
            conn.execute('CREATE INDEX records_day ON records(day)')
            conn.execute('CREATE INDEX records_party ON records(party)')
            conn.execute('CREATE INDEX records_waybill ON records(waybill)')
        return {**stats, 'months': sorted(months)}
    finally:
        conn.close()
