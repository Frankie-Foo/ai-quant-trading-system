from pathlib import Path


def test_external_dependency_check_is_structurally_read_only() -> None:
    source = Path(__file__).parents[1].joinpath(
        "scripts", "check_paper_external_dependencies.py"
    ).read_text(encoding="utf-8")

    assert "writes_enabled=False" in source
    assert ".get_account()" in source
    assert ".check_access()" in source
    assert ".configured_channel_available()" in source
    assert ".push(" not in source
    assert "record_event(" not in source
    assert "submit_" not in source
