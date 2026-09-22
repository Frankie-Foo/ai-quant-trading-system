from datetime import time

from schedule.modern_funnel import FunnelStage, _prerequisite, _stage_for


def test_four_checkpoints_use_eastern_session_times() -> None:
    assert _stage_for(time(8, 29)) is None
    assert _stage_for(time(8, 30)) is FunnelStage.FIRST_WAVE
    assert _stage_for(time(8, 59)) is FunnelStage.FIRST_WAVE
    assert _stage_for(time(9, 0)) is FunnelStage.SECOND_WAVE
    assert _stage_for(time(9, 29)) is FunnelStage.SECOND_WAVE
    assert _stage_for(time(9, 30)) is FunnelStage.FINAL_RANK
    assert _stage_for(time(9, 34)) is FunnelStage.FINAL_RANK
    assert _stage_for(time(9, 35)) is FunnelStage.OPEN_CONFIRMATION
    assert _stage_for(time(9, 45)) is None


def test_final_rank_must_follow_both_top_twenty_waves() -> None:
    assert _prerequisite(FunnelStage.FINAL_RANK) is FunnelStage.SECOND_WAVE
    assert _prerequisite(FunnelStage.OPEN_CONFIRMATION) is FunnelStage.FINAL_RANK
