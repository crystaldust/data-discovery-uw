"""Read, label and save eval/labels.csv for the labelling page (ddm label).

Changes return a new LabelFile; nothing is edited in place. Saving is atomic and keeps
the file's own column order, so a spreadsheet and the page can take turns on one file.
Notes go into a published file, so they get the same cleaning as every other cell.
"""

import csv
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ddm.eval.sample import EvalInputError, safe_cell, write_atomically

LABELS = frozenset({"same", "different", "unsure", ""})
REQUIRED_COLUMNS = frozenset({"pair_id", "stratum", "label", "note"})


@dataclass(frozen=True)
class LabelFile:
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]


def load_pairs(path: Path) -> LabelFile:
    """Spreadsheets add a byte-order mark; utf-8-sig drops it."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            missing = REQUIRED_COLUMNS - set(columns)
            if missing:
                raise EvalInputError(
                    f"{path.name} lacks columns {sorted(missing)}; save it as comma-separated CSV"
                )
            rows = tuple(MappingProxyType({c: row.get(c) or "" for c in columns}) for row in reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise EvalInputError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    return LabelFile(columns, rows)


def with_label(labels: LabelFile, pair_id: str, label: str, note: str) -> LabelFile:
    label = label.strip().lower()
    if label not in LABELS:
        raise EvalInputError("label must be same, different, unsure or empty")
    if not any(row["pair_id"] == pair_id for row in labels.rows):
        raise EvalInputError(f"unknown pair {safe_cell(pair_id)}")
    clean_note = safe_cell(note)
    rows = tuple(
        MappingProxyType({**row, "label": label, "note": clean_note})
        if row["pair_id"] == pair_id
        else row
        for row in labels.rows
    )
    return LabelFile(labels.columns, rows)


def save_pairs(path: Path, labels: LabelFile) -> None:
    def write(handle) -> None:
        writer = csv.DictWriter(handle, fieldnames=labels.columns, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(dict(row) for row in labels.rows)

    write_atomically(path, write)
