"""Readable summaries and metric CSV exports (scale 0–1)."""
import csv
import io
from pathlib import Path
import sys


def _csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader(); writer.writerows(rows)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(stream.getvalue(), encoding="utf-8")
    temporary.replace(path)


def display(result, system_csv=None, case_csv=None):
    print("System\tCases\tCoverage\tBreadth\tDepth\tOverall (0–1)", file=sys.stderr)
    for row in result["systems"]:
        print(f'{row["system_id"]}\t{row["case_count"]}/{row["expected_case_count"]}\t'
              f'{"PARTIAL" if row["partial"] else "COMPLETE"}\t'
              f'{row["breadth"]:.6f}\t{row["depth"]:.6f}\t{row["overall"]:.6f}', file=sys.stderr)
    if system_csv:
        _csv(system_csv, result["systems"], ["system_id", "case_count", "expected_case_count",
                                           "partial", "breadth", "depth", "overall"])
    if case_csv:
        _csv(case_csv, result["cases"], ["system_id", "case_id", "breadth", "depth", "overall"])
