"""Phase 59 band coverage gate (D-01, D-01a, D-02).

The engine loads the coverage report JSON by path, validates it as a
``BandCoverageReport`` and refuses to run when the report is missing,
unreadable, inconsistent, too short for the run window or ``phase62_blocked``.

``phase62_blocked`` false is permission to run, not evidence that coverage is
sufficient: every ``unavailable_bands`` entry is handed to the fill model as a
``BandUnavailable`` and surfaces as missing evidence in the report.

``report_sha256`` is recomputed here exactly the way 59 ``build_band_coverage``
derives it: canonical sha256 over the report fields, the target universe hash
(``TargetUniverseResult.target_sha256``, which the caller supplies from the 59
store because the file does not carry it) and the caveat codes. The file name
must follow 59 ``write_coverage_report`` (period dates plus the first 12 hex of
the hash) and agree with the report. The registration's recorded report hash and
raw file hash are checked as well when supplied.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from pydantic import BaseModel

from pilot_data.core import canonical_sha256
from pilot_data.price_bands import NONBLOCKING_ROW_DEFECTS, BandCoverageReport, UnavailableBand

from .errors import GateRefused

_NAME_RE = re.compile(r"^band-coverage-(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})-([0-9a-f]{12})\.json$")


@dataclass(frozen=True)
class GateResult:
    report: BandCoverageReport
    report_sha256: str
    file_sha256: str
    unavailable: tuple[UnavailableBand, ...]

    def reason_for(self, isin: str, session: date) -> str | None:
        for item in self.unavailable:
            if item.isin == isin and item.session == session:
                return item.reason
        return None

    def index(self) -> dict[tuple[str, date], str]:
        return {(item.isin, item.session): item.reason for item in self.unavailable}


_DIGEST_FIELDS = (
    "period_start", "period_end", "sessions", "sessions_by_status", "unsupported_sessions", "targets_checked",
    "target_unknown_counts", "fixed_count", "no_band_count", "unknown_count", "unknown_by_reason", "convention",
    "archive_depth", "phase62_blocked", "blocked_reasons", "row_conflict_sessions", "nonblocking_reasons",
    "unavailable_bands",
)


def _jsonable(value):
    """Same conversion 59 uses before hashing."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def recompute_report_sha256(report: BandCoverageReport, targets_sha256: str) -> str:
    """The 59 derivation of ``report_sha256`` (``build_band_coverage``), reproduced from the report and the targets hash."""
    fields = {name: _jsonable(getattr(report, name)) for name in _DIGEST_FIELDS}
    return canonical_sha256({"fields": fields, "targets": targets_sha256, "caveats": [c.code for c in report.caveats]})


def load_coverage_report(
    path: Path,
    *,
    run_start: date,
    run_end: date,
    targets_sha256: str,
    expected_report_sha256: str | None = None,
    expected_file_sha256: str | None = None,
) -> GateResult:
    """Load, validate and gate. Raises ``GateRefused`` (CLI exit 3) on every failure."""
    if run_end < run_start:
        raise GateRefused("run window ends before it starts", code="run_window_invalid")
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateRefused("band coverage report is missing or unreadable", code="report_missing") from exc
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        report = BandCoverageReport.model_validate(json.loads(raw.decode("utf-8")))
    except (ValueError, ValidationError) as exc:
        raise GateRefused("band coverage report is not a valid BandCoverageReport", code="report_invalid") from exc

    if expected_file_sha256 is not None and file_sha != expected_file_sha256:
        raise GateRefused("band coverage report file hash differs from the registered hash", code="report_tampered")
    if expected_report_sha256 is not None and report.report_sha256 != expected_report_sha256:
        raise GateRefused("band coverage report_sha256 differs from the registered hash", code="report_tampered")
    named = _NAME_RE.match(path.name)
    if named is None:
        raise GateRefused("band coverage report file name does not follow band-coverage-<start>-<end>-<sha12>.json",
                          code="report_tampered")
    if (named.group(1), named.group(2)) != (report.period_start.isoformat(), report.period_end.isoformat()) \
            or not report.report_sha256.startswith(named.group(3)):
        raise GateRefused("band coverage report differs from its file name", code="report_tampered")
    if recompute_report_sha256(report, targets_sha256) != report.report_sha256:
        raise GateRefused("band coverage report_sha256 does not match its content and the target universe",
                          code="report_tampered")
    _check_consistency(report)

    if report.phase62_blocked:
        raise GateRefused(
            "Phase 59 band coverage report has phase62_blocked true: "
            + "; ".join(report.blocked_reasons[:3]),
            code="phase62_blocked",
        )
    if report.period_start > run_start or report.period_end < run_end:
        raise GateRefused(
            f"band coverage report covers {report.period_start.isoformat()}..{report.period_end.isoformat()} "
            f"but the run needs {run_start.isoformat()}..{run_end.isoformat()}",
            code="report_period_short",
        )
    return GateResult(report, report.report_sha256, file_sha, report.unavailable_bands)


def _check_consistency(report: BandCoverageReport) -> None:
    if report.phase62_blocked != bool(report.blocked_reasons):
        raise GateRefused("phase62_blocked disagrees with blocked_reasons", code="report_tampered")
    nonblocking = sum(count for reason, count in report.unknown_by_reason.items() if reason in NONBLOCKING_ROW_DEFECTS)
    if len(report.unavailable_bands) != nonblocking:
        raise GateRefused("unavailable_bands disagrees with unknown_by_reason", code="report_tampered")
    for item in report.unavailable_bands:
        if item.reason not in NONBLOCKING_ROW_DEFECTS:
            raise GateRefused("unavailable_bands holds a blocking reason", code="report_tampered")
        if not report.period_start <= item.session <= report.period_end:
            raise GateRefused("unavailable band lies outside the report period", code="report_tampered")
