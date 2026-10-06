"""Phase 59 band coverage gate (D-01, D-01a, D-02).

The engine loads the coverage report JSON by path, validates it as a
``BandCoverageReport`` and refuses to run when the report is missing,
unreadable, inconsistent, too short for the run window or ``phase62_blocked``.

``phase62_blocked`` false is permission to run, not evidence that coverage is
sufficient: every ``unavailable_bands`` entry is handed to the fill model as a
``BandUnavailable`` and surfaces as missing evidence in the report.

``report_sha256`` in the file cannot be recomputed here (59 mixes in the target
universe hash, which the file does not carry). Tamper detection therefore uses
four anchors: the sha the registration recorded, the sha prefix in the file
name, the raw file sha256 the registration recorded, and internal consistency
between the blocked flag and the reasons and between the unavailable list and
its per-reason counts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from pilot_data.price_bands import NONBLOCKING_ROW_DEFECTS, BandCoverageReport, UnavailableBand

from .errors import GateRefused

_NAME_RE = re.compile(r"^band-coverage-\d{4}-\d{2}-\d{2}-\d{4}-\d{2}-\d{2}-([0-9a-f]{12})\.json$")


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


def load_coverage_report(
    path: Path,
    *,
    run_start: date,
    run_end: date,
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
    if named is not None and not report.report_sha256.startswith(named.group(1)):
        raise GateRefused("band coverage report_sha256 does not match its file name", code="report_tampered")
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
