from __future__ import annotations

from research.decision_quality import (
    DecisionOutcomeFacts,
    DecisionQuality,
    TailShadowFacts,
    build_bidirectional_review,
    build_tail_shadow,
)


def test_review_separates_process_quality_from_realized_profit() -> None:
    review = build_bidirectional_review(
        (
            DecisionOutcomeFacts(
                symbol="GOODWIN",
                rules_compliant=True,
                net_return=0.04,
                selection_facts=("material_earnings_surprise",),
                entry_facts=("order_flow_confirmed",),
                exit_facts=("planned_target",),
                violation_facts=(),
            ),
            DecisionOutcomeFacts(
                symbol="GOODLOSS",
                rules_compliant=True,
                net_return=-0.01,
                selection_facts=("catalyst_confirmed",),
                entry_facts=("structure_confirmed",),
                exit_facts=("hard_stop",),
                violation_facts=(),
            ),
            DecisionOutcomeFacts(
                symbol="LUCKY",
                rules_compliant=False,
                net_return=0.03,
                selection_facts=(),
                entry_facts=(),
                exit_facts=("late_spike",),
                violation_facts=("chased_above_limit",),
            ),
            DecisionOutcomeFacts(
                symbol="BADLOSS",
                rules_compliant=False,
                net_return=-0.02,
                selection_facts=(),
                entry_facts=(),
                exit_facts=("hard_stop",),
                violation_facts=("missing_order_flow_confirmation",),
            ),
        )
    )

    by_symbol = {item.symbol: item for item in review}
    assert by_symbol["GOODWIN"].quality is DecisionQuality.DISCIPLINED_WIN
    assert by_symbol["GOODLOSS"].quality is DecisionQuality.DISCIPLINED_LOSS
    assert by_symbol["LUCKY"].quality is DecisionQuality.LUCKY_WIN
    assert by_symbol["BADLOSS"].quality is DecisionQuality.AVOIDABLE_LOSS
    assert by_symbol["GOODWIN"].profit_reasons == (
        "material_earnings_surprise",
        "order_flow_confirmed",
        "planned_target",
    )
    assert by_symbol["GOODLOSS"].loss_reasons == (
        "catalyst_confirmed",
        "structure_confirmed",
        "hard_stop",
    )
    assert by_symbol["LUCKY"].violations == ("chased_above_limit",)


def test_missing_compliance_or_return_stays_unclassified() -> None:
    review = build_bidirectional_review(
        (
            DecisionOutcomeFacts(
                symbol="MISSING",
                rules_compliant=None,
                net_return=None,
                selection_facts=(),
                entry_facts=(),
                exit_facts=(),
                violation_facts=(),
            ),
        )
    )

    assert review[0].quality is DecisionQuality.UNCLASSIFIED
    assert review[0].profit_reasons == ()
    assert review[0].loss_reasons == ()


def test_tail_shadow_keeps_normal_high_and_a_plus_plus_ledgers_separate() -> None:
    shadow = build_tail_shadow(
        TailShadowFacts(
            symbol="RNG",
            entry_price=40.0,
            main_exit_price=46.0,
            standard_tail_exit_price=48.0,
            high_tail_exit_price=49.0,
            a_plus_plus_tail_exit_price=50.0,
        )
    )

    assert shadow.no_tail_return == 0.15
    assert shadow.standard_20_return == 0.16
    assert shadow.high_25_return == 0.16875
    assert shadow.a_plus_plus_30_return == 0.18
    assert shadow.best_shadow == "a_plus_plus_30"
