"""Immutable dataset snapshots: provenance, both price layers, read-only export and tamper detection."""

import json
import os
import stat
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from pilot_data.bhavcopy import ingest_pr_zip
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.core import PilotDataError, sha256_hex
from pilot_data.dataset import _read_parquet, build_dataset_snapshot, main, verify_dataset
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit
from test_pilot_data_crosscheck import (
    AS_OF,
    CANBK,
    CANBK_MEMBER,
    D1,
    D2,
    D3,
    D4,
    D5,
    START,
    END,
    STEADY,
    STEADY_MEMBER,
    breeze_rows,
    desc,
    load_bhavcopy,
    load_breeze,
    make_targets,
    run,
)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def built_run(store, *, steady_scale=Decimal(1)):
    load_bhavcopy(store)
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", CANBK))
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY, scale=steady_scale))
    return run(store, make_targets(CANBK_MEMBER, STEADY_MEMBER))


def snapshot(store, report, export_root=None):
    return build_dataset_snapshot(store, crosscheck_run_id=report.run_id, as_of=AS_OF, workspace="india",
                                  export_root=export_root)


def test_rows_carry_full_provenance_and_both_price_layers(store):
    report = built_run(store)
    manifest = snapshot(store, report)
    assert manifest.row_count == 10 and manifest.anchor_count == 2 and manifest.report_sha256 == report.report_sha256
    rows = store.query("SELECT manifest_json FROM dataset_snapshots")
    assert len(rows) == 1 and json.loads(rows[0][0])["dataset_sha256"] == manifest.dataset_sha256
    assert manifest.lineage_hashes.keys() == manifest.factor_set_hashes.keys() == {"INE476A01022", "INE002A01018"}
    assert manifest.spans["INE476A01022"]["short_history"] is False and manifest.targets_sha256 == "t" * 64


def test_dataset_rows_via_export_and_adjusted_values(store, tmp_path):
    report = built_run(store)
    manifest = snapshot(store, report, tmp_path / "export")
    path = tmp_path / "export" / manifest.dataset_sha256
    rows = _read_parquet(path / "rows.parquet")
    assert [(r.anchor_isin, r.trade_date) for r in rows] == sorted((r.anchor_isin, r.trade_date) for r in rows)
    first = next(r for r in rows if r.stock_code == "CANBAN" and r.trade_date == D1)
    assert first.isin == "INE476A01014" and first.anchor_isin == "INE476A01022"  # the ISIN valid on that date
    assert (first.raw_close, first.adj_close) == (Decimal("566.5500"), Decimal("113.3100"))
    assert first.adj_volume == first.raw_volume * 5 and not first.adjusted_quarantined
    later = next(r for r in rows if r.stock_code == "CANBAN" and r.trade_date == D3)
    assert later.adj_close == later.raw_close == Decimal("119.0000")
    for row in rows:
        for name in ("workspace", "anchor_isin", "isin", "nse_symbol", "series", "stock_code", "raw_adjustment_basis",
                     "adjusted_basis", "source", "source_sha256", "fetched_at_utc", "bhavcopy_source_sha256",
                     "crosscheck_run_id"):
            assert getattr(row, name), name
        assert row.source == "breeze_v2_1day_via_relay" and row.crosscheck_run_id == report.run_id
        assert len(row.source_sha256) == 64 and len(row.bhavcopy_source_sha256) == 64
        assert row.adjusted_basis.startswith("back_adjusted:as_of=2025-03-07:factor_set=")
    bases = {r.stock_code: r.raw_adjustment_basis for r in rows}
    assert bases == {"CANBAN": "as_traded_breeze_raw_confirmed", "STEADY": "as_traded_breeze_unverified"}
    assert [c.code for c in manifest.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "BREEZE_RAW_UNVERIFIED"]


def test_bars_before_an_unresolved_action_are_quarantined_for_adjusted_values_only(store, tmp_path):
    load_bhavcopy(store)
    ingest_pr_zip(
        store, desc(kind="pr_zip"),
        kit.pr_zip(D2, [kit.pd_index_row()],
                   [kit.bc_row("EQ", "STEADYCO", "STEADY CO", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=D4)], []),
        trade_date=D2,
    )
    derive_corporate_actions(store, workspace="india")
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY))
    report = run(store, make_targets(STEADY_MEMBER))
    manifest = snapshot(store, report, tmp_path / "export")
    rows = _read_parquet(tmp_path / "export" / manifest.dataset_sha256 / "rows.parquet")
    by_day = {r.trade_date: r for r in rows}
    for day in (D1, D2, D3):
        assert by_day[day].adjusted_quarantined and by_day[day].adj_close is None
        assert by_day[day].raw_close == Decimal("100.0000")
    for day in (D4, D5):
        assert not by_day[day].adjusted_quarantined and by_day[day].adj_close == Decimal("100.0000")


def test_adjusted_vendor_data_and_empty_runs_are_refused(store):
    load_bhavcopy(store)
    adjusted = {day: tuple(str(Decimal(p) * Decimal("0.2")) for p in prices) if day < D3 else prices
                for day, prices in CANBK.items()}
    load_breeze(store, "CANBAN", breeze_rows("CANBAN", adjusted))
    report = run(store, make_targets(CANBK_MEMBER))
    with pytest.raises(PilotDataError) as caught:
        snapshot(store, report)
    assert caught.value.code == "breeze_adjusted_detected"
    load_breeze(store, "STEADY", breeze_rows("STEADY", STEADY, scale=Decimal("0.5")))  # 50 percent off everywhere
    empty = run(store, make_targets(STEADY_MEMBER))
    assert empty.accepted_total == 0
    with pytest.raises(PilotDataError) as nothing:
        snapshot(store, empty)
    assert nothing.value.code == "dataset_empty"
    with pytest.raises(PilotDataError) as missing:
        build_dataset_snapshot(store, crosscheck_run_id="f" * 64, as_of=AS_OF, workspace="india", export_root=None)
    assert missing.value.code == "crosscheck_run_missing"


def test_the_hash_is_stable_and_a_second_export_verifies_instead_of_overwriting(store, tmp_path):
    report = built_run(store)
    first = snapshot(store, report)
    second = snapshot(store, report)
    assert first.dataset_sha256 == second.dataset_sha256 and len(first.dataset_sha256) == 64
    assert store.query("SELECT count(*) FROM dataset_snapshots")[0][0] == 1
    export = tmp_path / "export"
    published = snapshot(store, report, export)
    target = export / published.dataset_sha256
    assert sorted(p.name for p in target.iterdir()) == ["manifest.json", "rows.parquet"]
    assert published.dataset_sha256 == first.dataset_sha256 and published.parquet_sha256
    assert stat.S_IMODE((target / "rows.parquet").stat().st_mode) == 0o444
    assert stat.S_IMODE((target / "manifest.json").stat().st_mode) == 0o444
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    stamp = (target / "rows.parquet").stat().st_mtime_ns
    again = snapshot(store, report, export)
    assert again.parquet_sha256 == published.parquet_sha256 and (target / "rows.parquet").stat().st_mtime_ns == stamp
    on_disk = json.loads((target / "manifest.json").read_text())
    assert on_disk["parquet_sha256"] == published.parquet_sha256 and on_disk["workspace"] == "india"


def test_verify_detects_byte_flips_and_rewritten_values(store, tmp_path):
    report = built_run(store)
    export = tmp_path / "export"
    manifest = snapshot(store, report, export)
    target = export / manifest.dataset_sha256
    assert verify_dataset(target, workspace="india").dataset_sha256 == manifest.dataset_sha256
    with pytest.raises(PilotDataError) as wrong_workspace:
        verify_dataset(target, workspace="uk")  # type: ignore[arg-type]
    assert wrong_workspace.value.code == "workspace_mismatch"

    os.chmod(target, 0o755)
    parquet = target / "rows.parquet"
    original = parquet.read_bytes()
    os.chmod(parquet, 0o644)
    flipped = bytearray(original)
    flipped[len(flipped) // 2] ^= 0x01
    parquet.write_bytes(bytes(flipped))
    with pytest.raises(PilotDataError) as flip:
        verify_dataset(target, workspace="india")
    assert flip.value.code == "dataset_integrity"

    parquet.write_bytes(original)
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t AS SELECT * FROM read_parquet('{parquet}')")
    con.execute("UPDATE t SET raw_close = raw_close + 1 WHERE stock_code = 'STEADY' AND trade_date = DATE '2025-03-05'")
    parquet.unlink()
    con.execute(f"COPY t TO '{parquet}' (FORMAT PARQUET)")
    con.close()
    with pytest.raises(PilotDataError) as edited:
        verify_dataset(target, workspace="india")
    assert edited.value.code == "dataset_integrity"
    # even with the manifest hash repaired, the rows no longer reproduce the dataset hash
    body = json.loads((target / "manifest.json").read_text())
    body["parquet_sha256"] = sha256_hex(parquet.read_bytes())
    os.chmod(target / "manifest.json", 0o644)
    (target / "manifest.json").write_text(json.dumps(body))
    with pytest.raises(PilotDataError) as repaired:
        verify_dataset(target, workspace="india")
    assert repaired.value.code == "dataset_integrity"


def test_factor_set_drift_after_a_new_action_blocks_the_build(store):
    report = built_run(store)
    ingest_pr_zip(
        store, desc(kind="pr_zip"),
        kit.pr_zip(D2, [kit.pd_index_row()], [kit.bc_row("EQ", "STEADYCO", "STEADY CO", "BONUS 1:1", ex_date=D4)], []),
        trade_date=D2,
    )
    derive_corporate_actions(store, workspace="india")
    with pytest.raises(PilotDataError) as caught:
        snapshot(store, report)
    assert caught.value.code == "factor_set_drift"


def test_cli_build_and_verify(tmp_path, capsys):
    root = tmp_path / "cli"
    with PilotDataStore(root, workspace="india") as store:
        report = built_run(store)
    export = tmp_path / "export"
    code = main(["build", "--root", str(root), "--workspace", "india", "--run-id", report.run_id,
                 "--as-of", AS_OF.isoformat(), "--export-root", str(export)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and list(printed)[0] == "caveats" and printed["summary"]["row_count"] == 10
    path = export / printed["summary"]["dataset_sha256"]
    assert main(["verify", "--path", str(path), "--workspace", "india"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["row_count"] == 10
    assert main(["build", "--root", str(root), "--workspace", "india", "--run-id", "0" * 64,
                 "--as-of", AS_OF.isoformat()]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "crosscheck_run_missing"
    assert main(["verify", "--path", str(tmp_path / "nowhere"), "--workspace", "india"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "dataset_integrity"
