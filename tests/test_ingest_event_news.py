from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from data_plane.providers.alpaca_direct import (
    AlpacaNewsArticle,
    DirectAlpacaMarketDataClient,
)
from data_plane.providers.catalyst_news import fetch_alpaca_news_direct
from scripts import ingest_event_news


class FakeNewsClient(DirectAlpacaMarketDataClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]:
        self.calls.append(symbols)
        return (
            AlpacaNewsArticle(
                article_id="article-1",
                headline="Issuer wins contract",
                summary="Material multi-year award",
                author="Reporter",
                created_at_utc=start_utc,
                updated_at_utc=start_utc,
                url="https://example.test/article-1",
                symbols=symbols,
                source="Benzinga",
            ),
        )

    def close(self) -> None:
        self.closed = True


def _fixed_datetime(current: datetime) -> type[datetime]:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz=tz)

    return Clock


def test_injected_news_client_stays_open_and_merges_chunk_symbols() -> None:
    client = FakeNewsClient()
    start = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    frame = fetch_alpaca_news_direct(
        start,
        datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
        symbols=("MSFT", "AAPL"),
        chunk_size=1,
        client=client,
    )

    assert client.calls == [("AAPL",), ("MSFT",)]
    assert client.closed is False
    assert frame.height == 1
    assert frame.row(0, named=True)["symbols"] == ["AAPL", "MSFT"]


def test_cli_rejects_disallowed_time_before_reading_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "uncreated" / "events.sqlite3"
    monkeypatch.setattr(
        ingest_event_news,
        "datetime",
        _fixed_datetime(datetime(2026, 9, 14, 12, 29, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        ingest_event_news,
        "dotenv_values",
        lambda *_args, **_kwargs: pytest.fail("credentials were read"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest-event-news",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--ledger",
            str(ledger),
            "--symbols",
            "AAPL",
            "--start-utc",
            "2026-09-14T12:00:00Z",
            "--end-utc",
            "2026-09-14T12:30:00Z",
        ],
    )

    assert ingest_event_news.main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {"status": "failed", "error_type": "PermissionError"}
    assert not ledger.parent.exists()


def test_cli_empty_response_uses_only_fake_readonly_news_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
    env_file = tmp_path / "market.env"
    env_file.write_text("test fixture", encoding="utf-8")
    ledger = tmp_path / "ledger" / "events.sqlite3"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "data.alpaca.markets"
        assert request.url.path == "/v1beta1/news"
        return httpx.Response(200, json={"news": [], "next_page_token": None})

    real_client = httpx.Client

    def fake_client(
        *,
        timeout: float,
        follow_redirects: bool,
        trust_env: bool,
        event_hooks: dict[str, list[Callable[..., Any]]],
    ) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            event_hooks=event_hooks,
        )

    monkeypatch.setattr(ingest_event_news, "datetime", _fixed_datetime(current))
    monkeypatch.setattr(
        ingest_event_news,
        "dotenv_values",
        lambda *_args, **_kwargs: {
            "ALPACA_API_KEY": "fake-key",
            "ALPACA_SECRET_KEY": "fake-secret",
        },
    )
    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest-event-news",
            "--env-file",
            str(env_file),
            "--ledger",
            str(ledger),
            "--symbols",
            "AAPL",
            "--start-utc",
            "2026-09-14T12:00:00Z",
            "--end-utc",
            "2026-09-14T12:30:00Z",
        ],
    )

    assert ingest_event_news.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "empty_response"
    assert result["observations"] == 0
    assert len(requests) == 1
    assert ledger.exists()
