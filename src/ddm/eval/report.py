"""Build eval/report.md from the labelled sample alone (labels.csv and strata.json).

No database access: anyone with the two files gets the same report, byte for byte.
Labels are 'same', 'different' or 'unsure' (any case); blank means not labelled yet.
Rates leave out 'unsure' and blank pairs and say how many there were. Titles come from
untrusted feeds and the report is published: they are escaped, and links are rebuilt from
the numeric Podcast Index id rather than taken from the file.
"""

import csv
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ddm.eval.sample import FUZZY_BANDS, PI_URL, EvalInputError
from ddm.eval.stats import Estimate, Stratum, ratio_estimate, stratified_proportion, wilson
from ddm.ingest.fuzzy import TRGM_FLOOR

__all__ = ["EvalInputError", "build_report", "read_labels", "write_report"]

LABELS = frozenset({"same", "different", "unsure"})
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)
# A review queue in which fewer than half the pairs are real wastes the reviewer's time.
MIN_QUEUE_PRECISION = 0.5
STRONG_STRATA = ("strong_same_title", "strong_diff_title")
QUEUED_STRATA = ("strong_conflict", "unconfirmed_guid")
EXAMPLES_PER_CLASS = 3
REQUIRED_COLUMNS = frozenset({"pair_id", "stratum", "label"})
MAX_TITLE_CHARS = 120
MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~-])")


@dataclass(frozen=True)
class LabelledPair:
    line: int
    pair_id: str
    stratum: str
    right_class: str
    label: str | None
    left_title: str
    right_title: str
    left_pi_id: str
    right_pi_id: str


def _pair(line: int, row: Mapping[str, str | None], name: str) -> LabelledPair:
    label = (row.get("label") or "").strip().lower() or None
    if label is not None and label not in LABELS:
        raise EvalInputError(f"{name} line {line}: label must be same, different or unsure")
    return LabelledPair(
        line=line,
        pair_id=(row.get("pair_id") or "").strip(),
        stratum=(row.get("stratum") or "").strip(),
        right_class=row.get("right_class") or "",
        label=label,
        left_title=row.get("left_title") or "",
        right_title=row.get("right_title") or "",
        left_pi_id=(row.get("left_pi_id") or "").strip(),
        right_pi_id=(row.get("right_pi_id") or "").strip(),
    )


def read_labels(path: Path) -> tuple[LabelledPair, ...]:
    """Spreadsheets add a byte-order mark; utf-8-sig drops it."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
            if missing:
                raise EvalInputError(
                    f"{path.name} lacks columns {sorted(missing)}; save it as comma-separated CSV"
                )
            pairs = tuple(_pair(line, row, path.name) for line, row in enumerate(reader, start=2))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise EvalInputError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    seen: set[str] = set()
    for p in pairs:
        if p.pair_id in seen:
            raise EvalInputError(f"{path.name} line {p.line}: duplicate pair_id {p.pair_id}")
        seen.add(p.pair_id)
    return pairs


def _read_strata(path: Path) -> tuple[dict[str, int], Mapping[str, Any]]:
    """Population of each stratum, as recorded when the sample was drawn."""
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        return {name: int(info["population"]) for name, info in meta["strata"].items()}, meta
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EvalInputError(f"cannot read {path.name}: {type(exc).__name__}") from exc


def _md(text: str) -> str:
    """One line of plain text: whitespace collapsed, Markdown syntax escaped, truncated."""
    flat = " ".join(text.split())[:MAX_TITLE_CHARS]
    return MARKDOWN_SPECIAL.sub(r"\\\1", flat) or "(untitled)"


def _feed_link(pi_id: str) -> str:
    if pi_id.isascii() and pi_id.isdigit():
        return f" ([feed]({PI_URL.format(pi_id)}))"
    return ""


def _stratum(pairs: Sequence[LabelledPair], name: str, population: int) -> Stratum:
    decided = [p for p in pairs if p.stratum == name and p.label in ("same", "different")]
    return Stratum(population, len(decided), sum(p.label == "same" for p in decided))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _interval(low: float, high: float) -> str:
    return f"{_pct(low)}–{_pct(high)}"


def _rate_row(name: str, s: Stratum) -> str:
    if s.sampled == 0:
        return f"| {name} | {s.population} | 0 | – | – |"
    return (
        f"| {name} | {s.population} | {s.sampled} | {_pct(s.positives / s.sampled)} | "
        f"{_interval(*wilson(s.positives, s.sampled))} |"
    )


def _estimate_cells(estimate: Estimate | None) -> tuple[str, str]:
    if estimate is None:
        return ("–", "–")
    return (_pct(estimate.point), _interval(estimate.low, estimate.high))


def _band_range(lower: float) -> str:
    upper = next((b for b in FUZZY_BANDS if b > lower), 1.0)
    return f"{lower}–{upper}"


def _strong_section(strata: Mapping[str, Stratum]) -> list[str]:
    weighted = stratified_proportion([strata[n] for n in STRONG_STRATA])
    sampled = sum(strata[n].sampled for n in STRONG_STRATA)
    population = sum(strata[n].population for n in STRONG_STRATA)
    point, interval = _estimate_cells(weighted)
    return [
        "## Automatic merges on strong identifiers",
        "",
        "Precision: the share of automatic merges that join two feeds of the same podcast.",
        "",
        "| Stratum | Population | Labelled | Precision | 95% interval |",
        "|---|---|---|---|---|",
        *(_rate_row(n, strata[n]) for n in STRONG_STRATA),
        f"| Weighted (both strata) | {population} | {sampled} | {point} | {interval} |",
        "",
    ]


def _conflict_section(strata: Mapping[str, Stratum]) -> list[str]:
    queued = [n for n in QUEUED_STRATA if strata[n].population > 0 or n == "strong_conflict"]
    return [
        "## Strong-identifier matches sent to review",
        "",
        "The share of queued pairs that are nonetheless the same podcast: strong identifiers "
        "that disagree (strong_conflict), and a podcast:guid shared under another title "
        "(unconfirmed_guid, rules v2).",
        "",
        "| Stratum | Population | Labelled | Same | 95% interval |",
        "|---|---|---|---|---|",
        *(_rate_row(n, strata[n]) for n in queued),
        "",
    ]


def _bands(strata: Mapping[str, Stratum]) -> list[tuple[float, Stratum]]:
    return [(b, strata[f"fuzzy_{b}"]) for b in FUZZY_BANDS if f"fuzzy_{b}" in strata]


def _band_row(lower: float, s: Stratum) -> str:
    cells = _rate_row(f"fuzzy_{lower}", s).split(" | ", 1)
    return f"{cells[0]} | {_band_range(lower)} | {cells[1]}"


def _threshold_rows(bands: Sequence[tuple[float, Stratum]]) -> list[tuple[float, Any, Any]]:
    rows = []
    for t in THRESHOLDS:
        above = [s for b, s in bands if b >= t]
        below = [s for b, s in bands if b < t]
        rows.append((t, stratified_proportion(above), ratio_estimate(above, below)))
    return rows


def _recommendation(
    rows: Sequence[tuple[float, Any, Any]], bands: Sequence[tuple[float, Stratum]]
) -> str:
    if any(s.population > 0 and s.sampled == 0 for _, s in bands):
        return (
            "Labelling is not complete: every score band needs labels before a threshold is chosen."
        )
    for t, precision, _ in rows:
        if precision is not None and precision.point >= MIN_QUEUE_PRECISION:
            return (
                f"**{t}**: the lowest threshold whose estimated queue precision is at least "
                f"{_pct(MIN_QUEUE_PRECISION)}, which keeps as much recall as possible while "
                "at least half of what reviewers see is a real duplicate."
            )
    return f"No threshold reaches {_pct(MIN_QUEUE_PRECISION)} estimated queue precision."


def _fuzzy_section(strata: Mapping[str, Stratum]) -> list[str]:
    bands = _bands(strata)
    rows = _threshold_rows(bands)
    table = []
    for t, precision, recall in rows:
        p_point, p_interval = _estimate_cells(precision)
        r_point, r_interval = _estimate_cells(recall)
        table.append(f"| {t} | {p_point} | {r_point} | {p_interval} | {r_interval} |")
    return [
        "## Fuzzy candidates by score band",
        "",
        "Each discovered record's best fuzzy neighbour; precision is the share that is the "
        "same podcast.",
        "",
        "| Stratum | Scores | Population | Labelled | Precision | 95% interval |",
        "|---|---|---|---|---|---|",
        *(_band_row(b, s) for b, s in bands),
        "",
        "## Threshold table",
        "",
        "Queue precision and recall if the review threshold were at each band edge. Recall is "
        "relative to true duplicates whose best neighbour's title is at least "
        f"{TRGM_FLOOR} similar.",
        "",
        "| Threshold | Precision | Recall | Precision 95% | Recall 95% |",
        "|---|---|---|---|---|",
        *table,
        "",
        "## Recommended threshold",
        "",
        _recommendation(rows, bands),
        "",
    ]


BLIND_SPOTS = [
    "## What this evaluation cannot see",
    "",
    f"- Duplicates whose titles share less than {TRGM_FLOOR} trigram similarity never "
    "become candidates, so they are missing from both the queue and this recall estimate.",
    "- Two new feeds of the same podcast that arrive in the same run are not compared by the "
    "fuzzy matcher (strong identifiers still join them).",
    "- 'rename_or_move' is checked here only as 'same podcast'; whether the old feed was "
    "retired or still runs as a mirror is not labelled.",
    "",
]

EXAMPLE_RULES: tuple[tuple[str, str, str], ...] = (
    # (heading, right_class, label that makes it a good example)
    ("rename_or_move", "rename_or_move", "same"),
    ("duplicate", "duplicate", "same"),
    ("uncertain (a real duplicate)", "uncertain", "same"),
    ("new (a look-alike, not a duplicate)", "new", "different"),
)


def _examples(pairs: Iterable[LabelledPair]) -> list[str]:
    pairs = list(pairs)
    lines = ["## Real examples", ""]
    for title, right_class, label in EXAMPLE_RULES:
        matching = (p for p in pairs if p.right_class == right_class and p.label == label)
        found = sorted(matching, key=lambda p: p.pair_id)
        lines.append(f"**{title}**")
        lines.append("")
        lines.extend(
            f"- {_md(p.right_title)}{_feed_link(p.right_pi_id)} ~ "
            f"{_md(p.left_title)}{_feed_link(p.left_pi_id)}"
            for p in found[:EXAMPLES_PER_CLASS]
        )
        if not found:
            lines.append("- none in the labelled sample")
        lines.append("")
    return lines


def build_report(labels_path: Path, strata_path: Path) -> str:
    pairs = read_labels(labels_path)
    populations, meta = _read_strata(strata_path)
    unknown = sorted({p.stratum for p in pairs} - populations.keys())
    if unknown:
        raise EvalInputError(f"strata missing from {strata_path.name}: {', '.join(unknown)}")
    strata = {name: _stratum(pairs, name, n) for name, n in populations.items()}
    for name in (*STRONG_STRATA, *QUEUED_STRATA):
        strata.setdefault(name, Stratum(0, 0, 0))
    unsure = sum(p.label == "unsure" for p in pairs)
    unlabelled = sum(p.label is None for p in pairs)
    lines = [
        "# Discovery accuracy evaluation",
        "",
        "Built by `uv run ddm eval-report` from `eval/labels.csv` and `eval/strata.json`; "
        "labelling rules in `eval/LABELING.md`.",
        "",
        f"Pairs: {len(pairs)}. Unsure: {unsure}. Not labelled yet: {unlabelled}. "
        f"Sampling seed: `{_md(str(meta.get('seed')))}`. Review threshold when sampled: "
        f"{_md(str(meta.get('threshold')))}.",
        "",
        *_strong_section(strata),
        *_conflict_section(strata),
        *_fuzzy_section(strata),
        *BLIND_SPOTS,
        *_examples(pairs),
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_report(labels_path: Path, strata_path: Path, out_path: Path) -> None:
    out_path.write_text(build_report(labels_path, strata_path), encoding="utf-8")
