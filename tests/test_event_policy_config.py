"""Public candidate-policy contract; no broker or production configuration access."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from data_plane.calendar import build_xnys_schedule
from kernel import event_policy
from kernel.event_policy import EventPolicy, load_event_policy

POLICY_FILE = Path(__file__).resolve().parents[1] / "config/event_intraday_policy.v1.json"


def test_candidate_loads_shadow_with_versioned_risk_and_time_rules():
    policy = load_event_policy(POLICY_FILE)
    assert policy.schema_version == "event_intraday_policy.v1"
    assert policy.status == "shadow"
    assert policy.capital_cap_usd == 200_000
    assert policy.symbol_risk_fraction == 0.005
    assert (policy.first_attempt_fraction, policy.second_attempt_fraction) == (0.6, 0.4)
    assert (policy.entry_start_et, policy.entry_cutoff_et, policy.flatten_by_et) == (
        "10:00",
        "15:00",
        "15:50",
    )


def test_candidate_contains_all_risk_safety_limits():
    policy = load_event_policy(POLICY_FILE)
    assert policy.symbol_notional_fraction == 0.35
    assert policy.gross_notional_fraction == 1.0
    assert policy.theme_risk_fraction == 0.0075
    assert policy.portfolio_risk_fraction == 0.015
    assert policy.daily_loss_fraction == 0.015
    assert policy.max_positions == 3
    assert policy.max_spread_fraction == 0.0025
    assert policy.max_quote_age_seconds == 2.0
    assert policy.cost_reserve_fraction == 0.005
    assert policy.max_all_in_stop_fraction == 0.02
    assert policy.participation_fraction == 0.01
    assert policy.market_timezone == "America/New_York"


@pytest.mark.parametrize(
    "update",
    [
        {"surprise": 1},
        {"schema_version": "event_intraday_policy.v2"},
        {"status": "approved"},
        {"status": "active"},
        {"approved_by": "owner"},
        {"capital_cap_usd": 200001},
        {"capital_cap_usd": 0},
        {"capital_cap_usd": "200000"},
        {"capital_cap_usd": True},
        {"symbol_risk_fraction": float("nan")},
        {"capital_cap_usd": float("inf")},
        {"symbol_risk_fraction": -0.001},
        {"symbol_risk_fraction": 0.006},
        {"first_attempt_fraction": 0.5},
        {"second_attempt_fraction": 0.5},
        {"symbol_notional_fraction": 0.36},
        {"gross_notional_fraction": 1.01},
        {"theme_risk_fraction": 0.008},
        {"portfolio_risk_fraction": 0.016},
        {"daily_loss_fraction": 0.016},
        {"max_positions": 4},
        {"max_positions": True},
        {"max_spread_fraction": 0.003},
        {"max_quote_age_seconds": 2.1},
        {"cost_reserve_fraction": 0.0049},
        {"max_all_in_stop_fraction": 0.021},
        {"participation_fraction": 0.02},
        {"entry_start_et": "9:00"},
        {"entry_start_et": "09:59"},
        {"entry_start_et": "10:００"},
        {"entry_cutoff_et": "15:01"},
        {"flatten_by_et": "15:51"},
        {"entry_cutoff_et": "24:00"},
        {"entry_cutoff_et": "10:00"},
        {"flatten_by_et": "15:00"},
        {"market_timezone": "UTC"},
        {"theme_risk_fraction": 0.004},
        {"portfolio_risk_fraction": 0.007},
        {"gross_notional_fraction": 0.3},
        {"cost_reserve_fraction": 0.02},
    ],
)
def test_config_rejects_unsafe_or_inconsistent_values(update):
    payload = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        EventPolicy.model_validate({**payload, **update})


def test_immutable_policy_and_canonical_hash_are_shared_across_modules():
    policy = load_event_policy(POLICY_FILE)
    with pytest.raises(ValidationError):
        policy.capital_cap_usd = 1
    payload = policy.model_dump()
    payload["capital_cap_usd"] = 200000  # Equivalent JSON number.
    reordered = json.dumps(dict(reversed(list(payload.items()))), indent=4)
    reloaded = EventPolicy.model_validate_json(reordered)
    assert reloaded.policy_hash == policy.policy_hash
    assert len(policy.policy_hash) == 64
    policy.validate_references({"research": policy.policy_hash, "risk": reloaded.policy_hash})
    with pytest.raises(ValueError, match="risk"):
        policy.validate_references({"research": policy.policy_hash, "risk": "0" * 64})
    with pytest.raises(ValueError):
        policy.validate_references({})
    with pytest.raises(ValueError):
        policy.validate_references({"": policy.policy_hash})
    assert load_event_policy(POLICY_FILE, expected_hash=policy.policy_hash) == policy
    with pytest.raises(ValueError, match="hash"):
        load_event_policy(POLICY_FILE, expected_hash="0" * 64)
    changed = EventPolicy.model_validate({**payload, "max_quote_age_seconds": 1.0})
    assert changed.policy_hash != policy.policy_hash


@pytest.mark.parametrize(
    "opening,current,base,symbol,first,second",
    [
        (300000, 400000, 200000, 1000, 600, 400),
        (100000, 150000, 100000, 500, 300, 200),
        (150000, 80000, 80000, 400, 240, 160),
        (0, 200000, 0, 0, 0, 0),
        (200000, 0, 0, 0, 0, 0),
    ],
)
def test_risk_budget_uses_cap_opening_and_current_equity_not_buying_power(
    opening,
    current,
    base,
    symbol,
    first,
    second,
):
    policy = load_event_policy(POLICY_FILE)
    budget = policy.risk_budget(opening_equity_usd=opening, current_equity_usd=current)
    assert budget.risk_base_usd == base
    assert budget.symbol_risk_usd == symbol
    assert budget.first_attempt_usd == first
    assert budget.second_attempt_usd == second
    if base == 200000:
        assert budget.symbol_notional_usd == 70000
        assert budget.gross_notional_usd == 200000
        assert budget.theme_risk_usd == 1500
        assert budget.portfolio_risk_usd == 3000
        assert budget.daily_loss_usd == 3000
    with pytest.raises((AttributeError, ValidationError)):
        budget.risk_base_usd = 1
    with pytest.raises(TypeError):
        policy.risk_budget(
            opening_equity_usd=opening, current_equity_usd=current, buying_power=800000
        )


@pytest.mark.parametrize("field", ["opening_equity_usd", "current_equity_usd"])
@pytest.mark.parametrize(
    "bad", [-1, float("nan"), float("inf"), -float("inf"), True, "200000", None]
)
def test_equity_inputs_must_be_strict_finite_nonnegative_numbers(field, bad):
    values = {"opening_equity_usd": 200000, "current_equity_usd": 200000, field: bad}
    with pytest.raises(ValueError):
        load_event_policy(POLICY_FILE).risk_budget(**values)


def test_attempt_budget_never_resets_consumed_losses_costs_or_pending_risk():
    budget = load_event_policy(POLICY_FILE).risk_budget(
        opening_equity_usd=200000,
        current_equity_usd=200000,
    )
    committed = dict(
        symbol_consumed_usd=0,
        theme_committed_usd=0,
        portfolio_committed_usd=0,
        daily_committed_usd=0,
    )
    assert budget.available_attempt_risk(attempt=1, **committed) == 600
    assert budget.available_attempt_risk(attempt=2, **committed) == 400
    assert (
        budget.available_attempt_risk(
            attempt=2,
            **{**committed, "symbol_consumed_usd": 750},
        )
        == 250
    )
    for field, used in [
        ("theme_committed_usd", 1400),
        ("portfolio_committed_usd", 2900),
        ("daily_committed_usd", 2900),
    ]:
        assert budget.available_attempt_risk(attempt=2, **{**committed, field: used}) == 100
    assert (
        budget.available_attempt_risk(
            attempt=2,
            **{**committed, "symbol_consumed_usd": 1100},
        )
        == 0
    )
    for attempt in [0, 3, True, 1.0, "1"]:
        with pytest.raises(ValueError):
            budget.available_attempt_risk(attempt=attempt, **committed)
    for field in committed:
        for bad in [-1, float("nan"), float("inf"), True, "0"]:
            with pytest.raises(ValueError):
                budget.available_attempt_risk(attempt=2, **{**committed, field: bad})


def calendar_session(day):
    """Use the existing official-calendar adapter, never a weekday approximation."""
    schedule = build_xnys_schedule(day, day)
    return event_policy.MarketSession.model_validate(schedule.row(0, named=True))


@pytest.mark.parametrize(
    "day,start,cutoff,flatten",
    [
        (date(2026, 3, 6), 15, 20, "20:50"),
        (date(2026, 3, 9), 14, 19, "19:50"),
        (date(2026, 10, 30), 14, 19, "19:50"),
        (date(2026, 11, 2), 15, 20, "20:50"),
        (date(2026, 11, 27), 15, 17, "17:50"),
    ],
)
def test_calendar_session_windows_dst_half_days_and_exact_boundaries(day, start, cutoff, flatten):
    policy = load_event_policy(POLICY_FILE)
    session = calendar_session(day)
    window = policy.session_window(session)
    assert window.entry_start_utc == datetime.combine(day, datetime.min.time(), UTC).replace(
        hour=start,
    )
    assert window.entry_cutoff_utc.hour == cutoff
    assert window.flatten_by_utc.strftime("%H:%M") == flatten
    assert window.flatten_by_utc.tzinfo is UTC
    for zone in [UTC, ZoneInfo("America/New_York"), ZoneInfo("Asia/Shanghai")]:
        assert not policy.entry_allowed_at(
            (window.entry_start_utc - timedelta(microseconds=1)).astimezone(zone),
            session,
        )
        assert policy.entry_allowed_at(window.entry_start_utc.astimezone(zone), session)
        assert policy.entry_allowed_at(
            (window.entry_cutoff_utc - timedelta(microseconds=1)).astimezone(zone),
            session,
        )
        assert not policy.entry_allowed_at(window.entry_cutoff_utc.astimezone(zone), session)
        assert not policy.must_flatten_at(
            (window.flatten_by_utc - timedelta(microseconds=1)).astimezone(zone),
            session,
        )
        assert policy.must_flatten_at(window.flatten_by_utc.astimezone(zone), session)
    assert policy.must_flatten_at(session.market_close_utc + timedelta(minutes=1), session)


@pytest.mark.parametrize("day", [date(2026, 9, 7), date(2026, 9, 13)])
def test_no_calendar_session_means_no_entry_not_a_weekday_guess(day):
    assert build_xnys_schedule(day, day).is_empty()
    policy = load_event_policy(POLICY_FILE)
    now = datetime(day.year, day.month, day.day, 14, tzinfo=UTC)
    assert not policy.entry_allowed_at(now, None)
    with pytest.raises(ValueError, match="session"):
        policy.session_window(None)
    with pytest.raises(ValueError, match="session"):
        policy.must_flatten_at(now, None)


def test_market_time_rejects_naive_and_mismatched_session_dates():
    policy = load_event_policy(POLICY_FILE)
    session = calendar_session(date(2026, 9, 14))
    for method in [policy.entry_allowed_at, policy.must_flatten_at]:
        with pytest.raises(ValueError, match="aware"):
            method(datetime(2026, 9, 14, 10), session)
        with pytest.raises(ValueError, match="session"):
            method(datetime(2026, 9, 15, 14, tzinfo=UTC), session)
    with pytest.raises(ValueError, match="aware"):
        policy.entry_allowed_at(datetime(2026, 9, 14, 10), None)


def test_session_rejects_missing_provenance_naive_or_inconsistent_calendar_data():
    session = calendar_session(date(2026, 9, 14))
    payload = session.model_dump()
    for update in [
        {"market_open_utc": datetime(2026, 9, 14, 13, 30)},
        {"market_close_utc": session.market_open_utc},
        {"trade_date": date(2026, 9, 15)},
        {"is_half_day": True},
        {"session_minutes": 200},
        {"source": " "},
        {"source_version": ""},
        {"unknown": 1},
    ]:
        with pytest.raises(ValidationError):
            event_policy.MarketSession.model_validate({**payload, **update})
    localized = event_policy.MarketSession.model_validate(
        {
            **payload,
            "market_open_utc": session.market_open_utc.astimezone(
                ZoneInfo("America/New_York"),
            ),
        }
    )
    assert localized.market_open_utc.tzinfo is UTC
    assert localized == session


def test_public_copy_cannot_bypass_candidate_or_session_validation():
    policy = load_event_policy(POLICY_FILE)
    for update in [{"status": "approved"}, {"capital_cap_usd": float("nan")}, {"unknown": 1}]:
        with pytest.raises(ValidationError):
            policy.model_copy(update=update)
    tighter = policy.model_copy(update={"max_quote_age_seconds": 1.0}, deep=True)
    assert tighter.policy_hash != policy.policy_hash
    assert policy.max_quote_age_seconds == 2.0
    session = calendar_session(date(2026, 9, 14))
    with pytest.raises(ValidationError):
        session.model_copy(update={"market_open_utc": datetime(2026, 9, 14, 13, 30)})


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        "{",
        '{"schema_version":"event_intraday_policy.v1"}',
    ],
)
def test_loader_rejects_nonobject_incomplete_and_malformed_json(tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_event_policy(path)


def test_loader_rejects_duplicate_keys_and_nonfinite_json(tmp_path):
    content = POLICY_FILE.read_text(encoding="utf-8")
    path = tmp_path / "invalid.json"
    for invalid in [
        content.replace('"status": "shadow",', '"status": "approved", "status": "shadow",'),
        content.replace("200000.0", "NaN"),
        content.replace("200000.0", "Infinity"),
    ]:
        path.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError):
            load_event_policy(path)


def test_omitted_status_defaults_to_shadow_with_identical_hash():
    policy = load_event_policy(POLICY_FILE)
    payload = policy.model_dump(exclude={"status"})
    assert EventPolicy.model_validate(payload).policy_hash == policy.policy_hash


def test_conservative_time_overrides_and_explicit_late_session_open():
    policy = load_event_policy(POLICY_FILE).model_copy(
        update={
            "entry_start_et": "10:30",
            "entry_cutoff_et": "14:00",
            "flatten_by_et": "15:40",
        }
    )
    session = calendar_session(date(2026, 9, 14))
    window = policy.session_window(session)
    assert window.entry_start_utc.hour == 14 and window.entry_start_utc.minute == 30
    assert window.entry_cutoff_utc.hour == 18
    assert window.flatten_by_utc.strftime("%H:%M") == "19:40"
    # Synthetic exceptional calendar rows are test-only, with explicit provenance.
    late = session.model_copy(
        update={
            "market_open_utc": datetime(2026, 9, 14, 15, tzinfo=UTC),
            "session_minutes": 300,
            "is_half_day": True,
            "source": "test.synthetic",
        }
    )
    assert policy.session_window(late).entry_start_utc == late.market_open_utc
    no_window = late.model_copy(
        update={
            "market_open_utc": datetime(2026, 9, 14, 19, tzinfo=UTC),
            "session_minutes": 60,
        }
    )
    assert not policy.entry_allowed_at(no_window.market_open_utc, no_window)
    assert policy.must_flatten_at(datetime(2026, 9, 14, 19, 50, tzinfo=UTC), no_window)
