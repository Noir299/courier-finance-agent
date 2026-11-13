"""Build the supplemental customer-bill freight index from the original export."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


APP = Path(__file__).resolve().parent / 'app'
if APP.is_dir():
    sys.path.insert(0, str(APP))

from data_engine import (BATCH, FIELDS, SUMMARY_LABEL, SUPPORTED, clean, csv_values,
                         find_tables, to_cents, to_day, worksheet_values)


INDEX_NAME = 'outbound_bill.sqlite3'
BILL_FREIGHT_FIELDS = (*FIELDS['bill'][:3], '运费')


class MissingFreightColumn(ValueError):
    """A customer bill accepted for ordinary accounting omits its freight field."""


def build_outbound_bill(source, data_dir, progress=None, *, display_name=None, bill_revision=None, destination=None):
    """Return import metadata after atomically publishing the new freight index."""
    source = Path(source)
    destination = Path(destination) if destination is not None else Path(data_dir) / INDEX_NAME
    name = display_name or source.name
    if source.suffix.lower() not in SUPPORTED or Path(name).suffix.lower() != source.suffix.lower():
        raise ValueError('请提供名称与文件类型一致的客户账单 ZIP、CSV 或 XLSX。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix='.outbound-bill-', suffix='.sqlite3', dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        stats = _populate(source, name, temporary, progress, bill_revision)
        temporary.replace(destination)
        return stats
    finally:
        temporary.unlink(missing_ok=True)


def _freight_values(rows, first, sheet, label):
    parse = worksheet_values if sheet else csv_values
    try:
        yield from parse(rows, first, BILL_FREIGHT_FIELDS, label)
    except ValueError as error:
        if str(error).endswith('表头缺少“运费”。请核对文件类型和表头。'):
            raise MissingFreightColumn(str(error)) from error
        raise


def _populate(source, name, temporary, progress, bill_revision):
    stats = dict(name=name, files=0, rows=0, skipped=0, summary_rows=0, missing_amounts=0, invalid_amounts=0,
                 rounded_amounts=0, missing_dates=0, missing_parties=0, missing_waybills=0,
                 date_min=None, date_max=None)
    if bill_revision is not None:
        stats['bill_revision'] = bill_revision
    months = set()
    with closing(sqlite3.connect(temporary)) as db, source.open('rb') as stream, db:
        db.execute('CREATE TABLE records(day TEXT, waybill TEXT, party TEXT, freight_cents INTEGER)')
        with closing(find_tables(stream, name, temporary.parent)) as tables:
            for member, sheet, rows, base in tables:
                label = f'{member} / {sheet}' if sheet else member
                with closing(rows):
                    first = next(rows, None)
                    if first is None:
                        continue
                    if progress:
                        progress({**stats, 'phase': 'reading', 'source': label})
                    with closing(_freight_values(rows, first, sheet, label)) as values:
                        _insert_rows(db, values, base, stats, months)
                stats['files'] += 1
        if not stats['rows']:
            raise ValueError('客户账单中没有可导入的运费明细。')
        stats['months'] = sorted(months)
        if progress:
            progress({**stats, 'phase': 'indexing'})
        db.execute('CREATE INDEX records_day ON records(day)')
        db.execute('CREATE INDEX records_waybill ON records(waybill)')
        db.execute('CREATE INDEX records_party ON records(party)')
        db.execute('CREATE TABLE import_info(value TEXT NOT NULL)')
        db.execute('INSERT INTO import_info VALUES (?)', (json.dumps(stats, ensure_ascii=False),))
    return stats


def _insert_rows(db, values, base, stats, months):
    batch = []
    for raw_values in values:
        raw_day, party, waybill, raw_freight = map(clean, raw_values)
        if not raw_day and not party and SUMMARY_LABEL.fullmatch(waybill):
            stats['summary_rows'] += 1
            stats['skipped'] += 1
            continue
        if not any((raw_day, party, waybill, raw_freight)):
            stats['skipped'] += 1
            continue
        day = to_day(raw_day, base)
        freight, rounded, invalid = to_cents(raw_freight)
        stats['missing_dates'] += day is None
        stats['missing_parties'] += not party
        stats['missing_waybills'] += not waybill
        stats['missing_amounts'] += freight is None
        stats['invalid_amounts'] += invalid
        stats['rounded_amounts'] += rounded
        if day:
            months.add(day[:7])
            if stats['date_min'] is None or day < stats['date_min']:
                stats['date_min'] = day
            if stats['date_max'] is None or day > stats['date_max']:
                stats['date_max'] = day
        batch.append((day, waybill or None, party or None, freight))
        if len(batch) == BATCH:
            db.executemany('INSERT INTO records VALUES (?, ?, ?, ?)', batch)
            stats['rows'] += len(batch)
            batch.clear()
    db.executemany('INSERT INTO records VALUES (?, ?, ?, ?)', batch)
    stats['rows'] += len(batch)


def main():
    parser = argparse.ArgumentParser(description='从客户账单导入外发报表所需的运费明细')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()

    def progress(event):
        if event['phase'] == 'reading':
            print(f'正在读取 {event["source"]}，已读取 {event["rows"]:,} 行', flush=True)
        else:
            print(f'已读取 {event["rows"]:,} 行，正在建立查询索引', flush=True)

    stats = build_outbound_bill(args.source, args.data_dir, progress)
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
