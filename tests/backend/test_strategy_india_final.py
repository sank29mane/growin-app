"""Final review regressions: crash recovery, immutable outcomes and BE/BZ policy."""

import json

import pytest

from strategy_india import study
from strategy_india.errors import HoldoutInvalid, RegistryError
from strategy_india.holdout import open_holdout
from strategy_india.registry import Registry, _entry_hash, canonical_sha256
from strategy_india.portfolio import TICK_UNAVAILABLE
from test_strategy_india_support import default_criteria, registration_record, sha
from test_strategy_india_spend import HOLDOUT_DAYS, _cli_config, _etf_case, _registered


def _opened(tmp_path):
    reg = Registry(tmp_path / 'registry.jsonl')
    registration = reg.register(registration_record())
    grant = open_holdout(reg, criteria=default_criteria(), expected_head=reg.head_hash())
    return reg, registration, grant


def _verdict(registration, grant, verdict='PASS'):
    body = {'verdict': verdict, 'criteria_sha256': registration.payload['holdout_criteria_sha256']}
    return {'registration_entry_hash': registration.entry_hash, 'holdout_open_event_hash': grant.event_hash,
            'verdict': verdict, 'verdict_payload': body, 'verdict_sha256': canonical_sha256(body)}


def _raw_append(reg, kind, payload):
    # Bypass write-time validation to exercise the reader, with a correct enclosing chain hash.
    entries = reg.entries()
    seq, previous = len(entries), entries[-1].entry_hash
    record = {'seq': seq, 'kind': kind, 'payload': payload, 'prev_hash': previous,
              'entry_hash': _entry_hash(seq, kind, payload, previous)}
    with reg.path.open('a') as handle:
        handle.write(json.dumps(record) + '\n')


@pytest.mark.parametrize('kind', ['holdout_verdict', 'holdout_invalid'])
@pytest.mark.parametrize('later', ['holdout_verdict', 'holdout_invalid'])
def test_final_outcome_is_never_superseded(tmp_path, kind, later):
    reg, registration, grant = _opened(tmp_path)
    invalid = {'registration_entry_hash': registration.entry_hash, 'holdout_open_event_hash': grant.event_hash,
               'reason': 'evaluation_failed'}
    first = reg._append(kind, _verdict(registration, grant) if kind == 'holdout_verdict' else invalid)
    reg._append(later, _verdict(registration, grant, 'FAIL') if later == 'holdout_verdict' else invalid)
    assert reg.holdout_results() == (first,)
    assert len(reg.entries()) == 5  # full audit history is retained
    assert reg.invalid_events() == ((first,) if kind == 'holdout_invalid' else ())


@pytest.mark.parametrize('kind,bad_link,reading', [
    (kind, bad_link, reading)
    for kind in ('holdout_verdict', 'holdout_invalid')
    for bad_link in ('missing_open', 'unknown_open', 'missing_registration', 'unknown_registration',
                     'different_registration', 'wrong_digest', 'missing_reason_or_body', 'list_payload')
    for reading in (False, True)
    if (kind, bad_link, reading) != ('holdout_invalid', 'wrong_digest', False)
])
def test_malformed_outcome_refuses_with_registry_error(tmp_path, kind, bad_link, reading):
    reg, registration, grant = _opened(tmp_path)
    payload = _verdict(registration, grant) if kind == 'holdout_verdict' else {
        'registration_entry_hash': registration.entry_hash, 'holdout_open_event_hash': grant.event_hash,
        'reason': 'interrupted'}
    if bad_link == 'missing_open':
        del payload['holdout_open_event_hash']
    elif bad_link == 'unknown_open':
        payload['holdout_open_event_hash'] = sha('other open')
    elif bad_link == 'missing_registration':
        del payload['registration_entry_hash']
    elif bad_link == 'unknown_registration':
        payload['registration_entry_hash'] = sha('other registration')
    elif bad_link == 'different_registration':
        other = reg.register(registration_record(holdout_range={'start': '2035-01-01', 'end': '2035-06-30'},
                                               spent_holdout_event_hashes=[grant.event_hash]))
        payload['registration_entry_hash'] = other.entry_hash
    elif bad_link == 'wrong_digest':
        if kind == 'holdout_verdict':
            payload['verdict_sha256'] = sha('wrong digest')
        else:
            # INVALID has no nested body: its enclosing entry hash is the digest.
            _raw_append(reg, kind, payload)
            lines = reg.path.read_text().splitlines()
            record = json.loads(lines[-1])
            record['entry_hash'] = sha('wrong digest')
            reg.path.write_text('\n'.join(lines[:-1] + [json.dumps(record)]) + '\n')
            with pytest.raises(RegistryError):
                reg.holdout_results()
            return
    elif bad_link == 'missing_reason_or_body':
        del payload['verdict_payload' if kind == 'holdout_verdict' else 'reason']
    else:
        payload = []
    if reading:
        _raw_append(reg, kind, payload)
        with pytest.raises(RegistryError):
            reg.holdout_results()
    else:
        before = reg.path.read_bytes()
        with pytest.raises(RegistryError):
            reg._append(kind, payload)
        assert reg.path.read_bytes() == before


@pytest.mark.parametrize('tail', [b'{"seq":', b'{"reason":"\xe2'])
def test_torn_registry_tail_keeps_durable_outcome_and_can_append(tmp_path, caplog, tail):
    reg, registration, grant = _opened(tmp_path)
    final = reg.append_holdout_verdict(_verdict(registration, grant))
    before = reg.entries()
    with reg.path.open('ab') as handle:
        handle.write(tail)
    assert reg.entries(expected_head=final.entry_hash) == before
    assert reg.holdout_results() == (final,)
    assert 'Ignoring incomplete final line' in caplog.text
    reg.register(registration_record(holdout_range={'start': '2035-01-01', 'end': '2035-06-30'},
                                     spent_holdout_event_hashes=[grant.event_hash]))
    assert reg.entries()[:-1] == before
    assert reg.holdout_results() == (final,)


def test_malformed_complete_registry_line_is_not_ignored(tmp_path):
    reg, _, _ = _opened(tmp_path)
    with reg.path.open('ab') as handle:
        handle.write(b'{"seq":\n')
    with pytest.raises(RegistryError):
        reg.entries()


@pytest.mark.parametrize('tail', [b'{"holdout_range":', b'{"reason":"\xe2'])
def test_torn_ledger_tail_keeps_reserved_range_and_can_append(tmp_path, caplog, tail):
    inputs, _ = _registered(tmp_path)
    reg = inputs.registry
    entry = reg.registration()
    holdout = study.prepare(inputs).holdout
    study.record_spent(reg, entry, holdout)
    path = study.spent_ledger_path(reg)
    durable = path.read_bytes()
    with path.open('ab') as handle:
        handle.write(tail)
    with pytest.raises(HoldoutInvalid, match='INVALID: interrupted'):
        study.check_ledger_clear(reg, holdout)
    assert len(study._ledger_records(reg)) == 1
    assert 'Ignoring incomplete final line' in caplog.text
    study.record_spent(reg, entry, holdout)
    assert path.read_bytes() == durable + durable
    assert len(study._ledger_records(reg)) == 2


def test_crash_after_reservation_before_open_is_invalid_in_cli(tmp_path, monkeypatch, capsys):
    inputs, head = _registered(tmp_path)
    def crash(*_a, **_k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(study, 'open_holdout', crash)
    with pytest.raises(study.StrategyIndiaError):
        study.run_holdout(inputs, expected_head=head)
    before = inputs.registry.entries()
    assert len(study._ledger_records(inputs.registry)) == 1
    assert inputs.registry.holdout_events() == ()
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    from strategy_india import __main__ as cli
    assert cli.main(['holdout', '--config', str(config)]) == 2
    assert 'INVALID: interrupted' in capsys.readouterr().out
    assert inputs.registry.entries() == before


@pytest.mark.parametrize('step', ['report', 'verdict_append_return'])
def test_ctrl_c_after_durable_verdict_retains_verdict(tmp_path, monkeypatch, step):
    inputs, head = _registered(tmp_path)
    reg = inputs.registry
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt()
    if step == 'report':
        monkeypatch.setattr(study, 'write_report', interrupt)
    else:
        original = reg.append_holdout_verdict
        def durable_then_interrupt(payload):
            original(payload)
            interrupt()
        monkeypatch.setattr(reg, 'append_holdout_verdict', durable_then_interrupt)
    with pytest.raises(HoldoutInvalid, match='outcome is retained'):
        study.run_holdout(inputs, expected_head=head, report_root=tmp_path / 'reports')
    result, = reg.holdout_results()
    assert result.kind == 'holdout_verdict'
    assert reg.entries()[-1] == result
    assert reg.invalid_events() == ()


@pytest.mark.parametrize('series', ['BE', 'BZ'])
def test_universe_trade_for_trade_does_not_refuse_holdout(tmp_path, monkeypatch, series):
    anchor = 'INE000A01000'
    inputs, head = _etf_case(tmp_path, updates=lambda r: {'series': series}
                             if r.anchor_isin == anchor and r.trade_date >= HOLDOUT_DAYS[20] else None)
    runs = []
    original = study.run_holdout_segment
    def capture(*a, **k):
        run = original(*a, **k)
        runs.append(run)
        return run
    monkeypatch.setattr(study, 'run_holdout_segment', capture)
    outcome = study.run_holdout(inputs, expected_head=head)
    assert len(inputs.registry.holdout_events()) == 1
    assert outcome.report.units[0].etf_unknown_reason is None
    affected = [attempt for segment in runs[0].scenarios.values() for attempt in segment.attempts
                if attempt.anchor_isin == anchor and attempt.session >= HOLDOUT_DAYS[20] and attempt.affected]
    assert affected, 'unsupported stock series must record missed fills'
    assert all(attempt.outcome in (TICK_UNAVAILABLE, 'NO_ASSUMED_FILL') for attempt in affected)
