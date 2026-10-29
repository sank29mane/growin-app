"""64-08 follow-up: a session store seeded from the research store satisfies the runner's index-list precondition.

The runner tests stub ``latest_index_list``, so they cannot see a seed that leaves the tables empty. These rows run the
real seed, then the real ``open_run`` precondition and the real small-cap classification the signal step uses.
"""

import shutil
import socket
from datetime import date
from types import SimpleNamespace

import duckdb
import pytest

from pilot_data.constituents import latest_index_list
from pilot_data.core import PilotDataError
from pilot_data.store import DB_FILE_NAME, PilotDataStore
from pilot_data.universe import classify_smallcap
from shadow_india.runner import OpenRunRefused, open_run

import test_pilot_data_crosscheck as cc
import test_pilot_data_session_store as ss


class PastTheIndexListCheck(Exception):
    pass


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return ss.build_research(tmp_path_factory.mktemp("research_world_runner"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used in a seeded index list test")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def inputs_for(store):
    def current_hashes():
        raise PastTheIndexListCheck

    return SimpleNamespace(store=store, window_start=ss.WINDOW[0], current_hashes=current_hashes)


def test_open_run_gets_past_the_index_list_precondition_on_a_seeded_store(world, tmp_path):
    ss.seed(world, tmp_path / "session")
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        with pytest.raises(PastTheIndexListCheck):  # raised by the line after the check
            open_run(tmp_path / "ev", run_id="run64", inputs=inputs_for(seeded), orders=None)
    assert not (tmp_path / "ev").exists()


def test_open_run_refuses_on_a_store_seeded_from_a_research_store_with_no_list_fetched_by_the_window_start(world, tmp_path):
    root = tmp_path / "research_copy"
    shutil.copytree(world["root"], root)
    connection = duckdb.connect(str(root / DB_FILE_NAME))
    connection.execute("DELETE FROM index_list_members WHERE source_sha256 IN "
                       "(SELECT source_sha256 FROM index_list_snapshots WHERE fetched_at_utc < '2026-01-01')")
    connection.execute("DELETE FROM index_list_snapshots WHERE fetched_at_utc < '2026-01-01'")
    connection.close()
    ss.seed({**world, "root": root}, tmp_path / "session")  # only look-ahead lists are left, and none is seeded
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded:
        assert latest_index_list(seeded, "nifty500", fetched_by=ss.WINDOW[0]) is None
        with pytest.raises(OpenRunRefused) as caught:
            open_run(tmp_path / "ev", run_id="run64", inputs=inputs_for(seeded), orders=None)
    assert caught.value.code == "index_list_snapshot_not_seeded"


def test_the_small_cap_classification_on_the_seeded_store_reads_the_lists_fetched_by_each_day_and_no_later_one(world, tmp_path):
    ss.seed(world, tmp_path / "session")
    targets = world["targets"]
    steady, canbk = cc.STEADY_ISIN, cc.NEW
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded, \
            PilotDataStore(world["root"], workspace="india", read_only=True) as research:
        on_window_start = classify_smallcap(seeded, targets, as_of=ss.WINDOW[0])
        # the early smallcap list names STEADY only; the later one (not seeded) would also name CANBK
        assert on_window_start == {steady: "small", canbk: "not_small"}
        assert classify_smallcap(seeded, targets, as_of=ss.D5) == on_window_start
        assert classify_smallcap(seeded, targets) == on_window_start  # no as_of: still nothing later in the store
        assert classify_smallcap(research, targets, as_of=ss.WINDOW[0]) == on_window_start  # same answer as research
        assert classify_smallcap(research, targets) != on_window_start  # research "latest" would have looked ahead
        before_any_list = classify_smallcap(seeded, targets, as_of=ss.D1.replace(month=2, day=1))
        assert set(before_any_list.values()) == {"unclassified"}


def test_a_member_dropped_from_the_seeded_older_list_would_flip_the_classification_and_the_seed_refuses_it(
        world, tmp_path, monkeypatch):
    """CANBK is in the early nifty500 list, so on 2025-02-28 it is "not_small". Dropping it from the seeded copy makes
    that day "unclassified". The newest lists are untouched, so only a parity check over every snapshot sees it."""
    day = date(2025, 2, 28)
    ss.tamper_seeded_lists(monkeypatch, ss.drop_canbk("n_old"))
    with pytest.raises(PilotDataError) as caught:
        ss.seed(world, tmp_path / "session")
    assert caught.value.code == "seed_parity_failed"
    # the store the failed seed left behind really does classify differently, which is the harm the parity check stops
    with PilotDataStore(tmp_path / "session", workspace="india", read_only=True) as seeded, \
            PilotDataStore(world["root"], workspace="india", read_only=True) as research:
        assert classify_smallcap(research, world["targets"], as_of=day)[cc.NEW] == "not_small"
        assert classify_smallcap(seeded, world["targets"], as_of=day)[cc.NEW] == "unclassified"
