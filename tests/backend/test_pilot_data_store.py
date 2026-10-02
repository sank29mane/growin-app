"""Store invariants (append-only, blob integrity, workspace pin, read-only) and the NSE HTTP seam."""

import json
import os
from datetime import date, datetime, timezone

import duckdb
import httpx
import pytest

from pilot_data.bhavcopy import ingest_udiff
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.models import QuarantineRecord
from pilot_data.nse_http import FetchedFile, NoFile, NseHttp, build_default_client
from pilot_data.store import DB_FILE_NAME, PilotDataStore

import pilot_data_testkit as kit

D2 = date(2025, 1, 2)
FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def desc(locator="https://nsearchives.nseindia.com/x") -> SourceDescriptor:
    return SourceDescriptor(source="nse_archive", kind="udiff_cm", locator=locator, fetched_at=FETCHED, for_date=D2)


def ingest(store, rows):
    return ingest_udiff(store, desc(), kit.udiff_zip(D2, rows), trade_date=D2)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "pilot"


def test_reingest_with_changed_price_changes_no_row_and_records_one_conflict(root):
    changed = dict(kit.UDIFF_RELIANCE_20250102, ClsPric="1240.00")
    with PilotDataStore(root, workspace="india") as store:
        first = ingest(store, [kit.UDIFF_RELIANCE_20250102, kit.UDIFF_NIFTYBEES_20250102])
        second = ingest(store, [changed, kit.UDIFF_NIFTYBEES_20250102])
        assert first.source_sha256 != second.source_sha256
        assert second.conflicts == 1 and second.bars_inserted == 0 and second.bars_identical == 1
        close = store.query("SELECT close FROM bhavcopy_bars WHERE nse_symbol = 'RELIANCE'")
        assert str(close[0][0]) == "1241.8000"
        rows = store.query(
            "SELECT evidence_json, detail_json FROM quarantine_records WHERE check_name = 'append_conflict'"
        )
        assert len(rows) == 1
        assert sorted(json.loads(rows[0][0])) == sorted([first.source_sha256, second.source_sha256])
        # a third identical re-run of the changed file adds nothing
        ingest(store, [changed, kit.UDIFF_NIFTYBEES_20250102])
        assert store.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'append_conflict'")[0][0] == 1


def test_identical_reingest_inserts_nothing_and_logs_a_fetch_event(root):
    with PilotDataStore(root, workspace="india") as store:
        ingest(store, [kit.UDIFF_RELIANCE_20250102])
        bars = store.query("SELECT count(*) FROM bhavcopy_bars")[0][0]
        again = ingest(store, [kit.UDIFF_RELIANCE_20250102])
        assert again.bars_inserted == 0 and again.bars_identical == 1 and again.conflicts == 0
        assert store.query("SELECT count(*) FROM bhavcopy_bars")[0][0] == bars
        assert store.query("SELECT count(*) FROM source_files")[0][0] == 1
        assert store.query("SELECT count(*) FROM fetch_events")[0][0] == 2


def test_blob_tampering_is_detected_and_bad_hashes_rejected(root):
    with PilotDataStore(root, workspace="india") as store:
        content = b"hello evidence"
        ref = store.register_source(desc(), content)
        assert store.read_blob(ref.source_sha256) == content
        blob = root / "blobs" / "sha256" / ref.source_sha256[:2] / ref.source_sha256
        assert oct(blob.stat().st_mode & 0o777) == oct(0o444)
        os.chmod(blob, 0o644)
        blob.write_bytes(b"hello evidencf")
        with pytest.raises(PilotDataError) as caught:
            store.read_blob(ref.source_sha256)
        assert caught.value.code == "blob_integrity"
        with pytest.raises(PilotDataError) as bad:
            store.read_blob("../x")
        assert bad.value.code == "source_hash_invalid"
        with pytest.raises(PilotDataError) as again:
            store.register_source(desc(), content)
        assert again.value.code == "blob_integrity"


def test_read_only_store_rejects_every_write_and_leaves_the_database_unchanged(root):
    with PilotDataStore(root, workspace="india") as store:
        ingest(store, [kit.UDIFF_RELIANCE_20250102])
    with PilotDataStore(root, workspace="india", read_only=True) as ro:
        before = ro.query("SELECT count(*) FROM bhavcopy_bars")[0][0]
        ro.ensure_table("bhavcopy_bars", "CREATE TABLE IF NOT EXISTS bhavcopy_bars(x INTEGER)", key_columns=())
        for call in (
            lambda: ro.append_rows("bhavcopy_bars", [{"row_sha256": "a", "source_sha256": "b"}], check="x"),
            lambda: ro.register_source(desc(), b"abc"),
            lambda: ro.record_quarantine(
                [QuarantineRecord(workspace="india", check="x", reason_code="y", scope="raw")]
            ),
        ):
            with pytest.raises(PilotDataError) as caught:
                call()
            assert caught.value.code == "store_read_only"
        assert ro.query("SELECT count(*) FROM bhavcopy_bars")[0][0] == before


def test_workspace_pin_rejects_a_foreign_identity_row(tmp_path):
    root = tmp_path / "foreign"
    root.mkdir()
    con = duckdb.connect(str(root / DB_FILE_NAME))
    con.execute(
        "CREATE TABLE pilot_store_identity(workspace VARCHAR NOT NULL, created_at_utc TIMESTAMP NOT NULL, "
        "schema_version INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO pilot_store_identity VALUES ('uk', now()::TIMESTAMP, 1)")
    con.close()
    with pytest.raises(PilotDataError) as caught:
        PilotDataStore(root, workspace="india")
    assert caught.value.code == "store_workspace_mismatch"


def test_workspace_is_required_and_validated(root):
    with pytest.raises(TypeError):
        PilotDataStore(root)  # type: ignore[call-arg]
    with pytest.raises(PilotDataError) as caught:
        PilotDataStore(root, workspace="uk")  # type: ignore[arg-type]
    assert caught.value.code == "workspace_invalid"


def test_query_is_select_only_and_tables_must_be_registered(root):
    with PilotDataStore(root, workspace="india") as store:
        with pytest.raises(PilotDataError) as caught:
            store.query("INSERT INTO source_files VALUES ('a','b','c','d',now()::TIMESTAMP,1,NULL)")
        assert caught.value.code == "store_query_not_select"
        with pytest.raises(PilotDataError) as unregistered:
            store.append_rows("never_registered", [{"row_sha256": "a", "source_sha256": "b"}], check="x")
        assert unregistered.value.code == "table_not_registered"
        with pytest.raises(PilotDataError) as badname:
            store.ensure_table("Bad-Name", "CREATE TABLE IF NOT EXISTS Bad-Name(x INTEGER)", key_columns=("x",))
        assert badname.value.code == "table_name_invalid"


def test_append_rows_requires_hashes_and_known_columns(root):
    with PilotDataStore(root, workspace="india") as store:
        store.ensure_table(
            "probe_rows",
            "CREATE TABLE IF NOT EXISTS probe_rows(k VARCHAR PRIMARY KEY, v INTEGER, source_sha256 VARCHAR, "
            "row_sha256 VARCHAR)",
            key_columns=("k",),
        )
        with pytest.raises(PilotDataError) as missing:
            store.append_rows("probe_rows", [{"k": "a", "v": 1}], check="probe")
        assert missing.value.code == "append_row_invalid"
        with pytest.raises(PilotDataError) as unknown:
            store.append_rows("probe_rows", [{"k": "a", "zzz": 1, "row_sha256": "h", "source_sha256": "s"}],
                              check="probe")
        assert unknown.value.code == "append_column_unknown"
        outcome = store.append_rows(
            "probe_rows",
            [
                {"k": "a", "v": 1, "row_sha256": "h1", "source_sha256": "s"},
                {"k": "a", "v": 1, "row_sha256": "h1", "source_sha256": "s"},
                {"k": "b", "v": 2, "row_sha256": "h2", "source_sha256": "s"},
                {"k": "c", "v": 3, "row_sha256": "h3", "source_sha256": "s"},
                {"k": "c", "v": 4, "row_sha256": "h4", "source_sha256": "s2"},
            ],
            check="probe",
        )
        assert (outcome.inserted, outcome.conflicts) == (2, 1)
        assert store.query("SELECT k FROM probe_rows ORDER BY k") == [("a",), ("b",)]
        with pytest.raises(PilotDataError) as no_source_key:
            store.append_rows("probe_rows", [{"k": "z", "v": 9, "row_sha256": "h"}], check="probe")
        assert no_source_key.value.code == "append_row_invalid"
        # an attempt that produced no bytes may carry an explicit None source
        none_source = store.append_rows(
            "probe_rows", [{"k": "z", "v": 9, "row_sha256": "h", "source_sha256": None}], check="probe"
        )
        assert none_source.inserted == 1


# ----------------------------------------------------------------------- NseHttp
ARCHIVE_URL = "https://nsearchives.nseindia.com/content/cm/file.zip"
API_URL = "https://www.nseindia.com/api/reportASM"


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def make_http(responses, *, max_bytes=1024, min_interval=1.0):
    """responses: list of httpx.Response factories consumed in order; returns (http, calls, clock)."""
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    clock = Clock()
    http = NseHttp(
        httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval_seconds=min_interval, sleeper=clock.sleep, monotonic=clock.monotonic, max_bytes=max_bytes,
    )
    return http, calls, clock


ZIP_BODY = kit._zip_bytes({"a.csv": b"x,y\n1,2\n"})
HTML_404 = httpx.Response(404, content=b"<html>" + b"x" * 3400 + b"</html>")


def test_404_returns_nofile():
    http, calls, _ = make_http([HTML_404])
    assert isinstance(http.fetch(ARCHIVE_URL, expect="zip"), NoFile)
    assert len(calls) == 1


def test_html_200_when_zip_expected_is_unexpected_content():
    http, _, _ = make_http([httpx.Response(200, content=b"<html>blocked</html>")])
    with pytest.raises(PilotDataError) as caught:
        http.fetch(ARCHIVE_URL, expect="zip")
    assert caught.value.code == "nse_unexpected_content"


def test_403_is_refused_after_exactly_one_call():
    http, calls, clock = make_http([httpx.Response(403, content=b"no")])
    with pytest.raises(PilotDataError) as caught:
        http.fetch(ARCHIVE_URL, expect="zip")
    assert caught.value.code == "nse_fetch_refused"
    assert len(calls) == 1 and clock.sleeps == []


def test_503_retried_with_backoff_then_succeeds():
    http, calls, clock = make_http(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, content=ZIP_BODY)]
    )
    result = http.fetch(ARCHIVE_URL, expect="zip")
    assert isinstance(result, FetchedFile) and result.content == ZIP_BODY
    assert clock.sleeps == [2, 4]
    assert len(calls) == 3


def test_persistent_5xx_and_transport_errors_fail_after_two_retries():
    http, calls, _ = make_http([httpx.Response(500), httpx.ConnectError("boom"), httpx.Response(502)])
    with pytest.raises(PilotDataError) as caught:
        http.fetch(ARCHIVE_URL, expect="zip")
    assert caught.value.code == "nse_fetch_failed"
    assert len(calls) == 3


@pytest.mark.parametrize(
    "url", ["http://nsearchives.nseindia.com/x.zip", "https://example.com/x.zip", "https://nseindia.com.evil.io/x"]
)
def test_urls_off_the_allow_list_are_refused_before_any_request(url):
    http, calls, _ = make_http([])
    with pytest.raises(PilotDataError) as caught:
        http.fetch(url, expect="zip")
    assert caught.value.code == "nse_url_not_allowed"
    assert calls == []


def test_redirects_are_refused():
    http, _, _ = make_http([httpx.Response(302, headers={"location": "https://evil.example/"})])
    with pytest.raises(PilotDataError) as caught:
        http.fetch(ARCHIVE_URL, expect="zip")
    assert caught.value.code == "nse_redirect_refused"


def test_oversized_bodies_are_refused():
    http, _, _ = make_http([httpx.Response(200, content=b"PK\x03\x04" + b"0" * 2000)], max_bytes=1024)
    with pytest.raises(PilotDataError) as caught:
        http.fetch(ARCHIVE_URL, expect="zip")
    assert caught.value.code == "nse_response_too_large"


def test_requests_are_paced_and_carry_browser_headers():
    http, calls, clock = make_http([httpx.Response(200, content=ZIP_BODY), httpx.Response(200, content=ZIP_BODY)])
    http.fetch(ARCHIVE_URL, expect="zip")
    clock.now += 0.25
    http.fetch(ARCHIVE_URL, expect="zip")
    assert clock.sleeps == [pytest.approx(0.75)]
    assert calls[0].headers["referer"] == "https://www.nseindia.com/"
    assert calls[0].headers["accept"] == "*/*"
    assert "Chrome" in calls[0].headers["user-agent"]


def test_json_and_csv_validation_and_descriptor_sources():
    http, _, _ = make_http(
        [
            httpx.Response(200, content=b'{"longterm": {"data": []}}'),
            httpx.Response(200, content=b"Company Name,Industry\nA,B\n"),
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, content=b"nocommahere"),
        ]
    )
    api = http.fetch(API_URL, expect="json")
    assert isinstance(api, FetchedFile)
    assert api.descriptor("surveillance_asm", None).source == "nse_api"
    csv_file = http.fetch(ARCHIVE_URL, expect="csv")
    assert csv_file.descriptor("index_list", D2).source == "nse_archive"
    for expect in ("json", "csv"):
        with pytest.raises(PilotDataError) as caught:
            http.fetch(API_URL if expect == "json" else ARCHIVE_URL, expect=expect)
        assert caught.value.code == "nse_unexpected_content"


def test_default_client_never_follows_redirects():
    client = build_default_client()
    try:
        assert client.follow_redirects is False
    finally:
        client.close()
