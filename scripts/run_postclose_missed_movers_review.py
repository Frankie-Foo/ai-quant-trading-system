from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.http import DownloadError
from data_plane.providers.alpaca import (
    fetch_sparse_bars_for_monitoring,
    stock_data_policy_from_env,
)
from data_plane.providers.catalyst_news import fetch_alpaca_news
from data_plane.providers.massive import fetch_grouped_daily
from data_plane.storage import persist_snapshot
from research.intraday_selection_postmortem import (
    REVIEW_SCHEMA_VERSION,
    build_intraday_selection_postmortem,
)
from research.intraday_selection_postmortem import (
    classify_mover as classify_mover,
)

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
REQUIRED_DOLLAR_VOLUME = 20_000_000
REQUIRED_ADV_USD = 20_000_000
MINIMUM_PRICE = 5
REVIEW_SOURCE = "research.intraday_selection_postmortem"
LIVERMORE_PUSH_URL = "https://vps-service.vertu.cn/v1/im/user-robots/push"


@dataclass(frozen=True)
class ReviewPushConfig:
    trade_date: date
    channel_id: str


@dataclass(frozen=True)
class Signal:
    event: str
    symbol: str
    reason: str
    message: str
    dedupe_key: str


def _load_push_config(path: Path) -> ReviewPushConfig:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("push config must be a JSON object")
    return ReviewPushConfig(
        trade_date=date.fromisoformat(str(value["trade_date"])),
        channel_id=str(value["channel_id"]),
    )


def _send_vps(
    channel_id: str,
    signal: Signal,
    *,
    client: httpx.Client | None = None,
) -> str:
    app_id = os.getenv("VPS_LIVERMORE_APP_ID", "").strip()
    app_secret = os.getenv("VPS_LIVERMORE_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("Livermore robot credentials are not configured")
    owns_client = client is None
    http_client = client or httpx.Client(timeout=20)
    try:
        request_body = json.dumps(
            {"channel_id": channel_id, "body": signal.message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = http_client.post(
            LIVERMORE_PUSH_URL,
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-vertu-bot-app-id": app_id,
                "x-vertu-bot-app-secret": app_secret,
            },
            content=request_body,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()
    if not isinstance(payload, dict):
        raise RuntimeError("Livermore push returned an invalid response")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Livermore push did not return a message")
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Livermore push did not return a message id")
    if message.get("sender_type") != "bot":
        raise RuntimeError("Livermore push sender identity was not a bot")
    return message_id


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _previous_xnys_session(trade_date: date) -> date:
    schedule = build_xnys_schedule(trade_date - timedelta(days=10), trade_date)
    prior = schedule.filter(pl.col("trade_date") < trade_date)
    if prior.is_empty():
        raise ValueError(f"previous XNYS session unavailable for {trade_date}")
    value = prior.get_column("trade_date")[-1]
    if not isinstance(value, date):
        raise ValueError("previous XNYS session is invalid")
    return value


def _latest_snapshot(
    data_root: Path,
    *,
    prefix: str,
    date_column: str,
    trade_date: date,
) -> Path:
    matches: list[Path] = []
    for path in (data_root / "accepted").glob(f"{prefix}-*/data.parquet"):
        try:
            values = (
                pl.read_parquet(path, columns=[date_column])
                .get_column(date_column)
                .cast(pl.Date)
                .unique()
                .to_list()
            )
        except (OSError, pl.exceptions.PolarsError):
            continue
        if values == [trade_date]:
            matches.append(path)
    if not matches:
        raise FileNotFoundError(f"no {prefix} snapshot for {trade_date}")
    return max(matches, key=lambda path: path.parent.stat().st_mtime_ns)


def _chunks(values: tuple[str, ...], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _fetch_alpaca_chunks(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
) -> pl.DataFrame:
    policy = stock_data_policy_from_env()
    frames: list[pl.DataFrame] = []
    for chunk in _chunks(symbols, 200):
        last_error: DownloadError | None = None
        for attempt in range(3):
            try:
                frame, _ = fetch_sparse_bars_for_monitoring(
                    chunk,
                    start_utc,
                    end_utc,
                    feed=policy.feed,
                )
                frames.append(frame)
                last_error = None
                break
            except DownloadError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2)
        if last_error is not None:
            raise RuntimeError(
                f"Alpaca SIP failed for a {len(chunk)}-symbol chunk"
            ) from last_error
    if not frames:
        raise RuntimeError("Alpaca SIP returned no close bars")
    return pl.concat(frames, how="diagonal_relaxed")


def _fetch_alpaca_close_frame(
    trade_date: date,
    universe: pl.DataFrame,
) -> pl.DataFrame:
    eligible = universe.filter(
        pl.col("precheck_pass")
        & (pl.col("security_type") == "CS")
        & (pl.col("price") >= MINIMUM_PRICE)
        & (pl.col("adv_usd") >= REQUIRED_ADV_USD)
    ).select("symbol", pl.col("price").alias("previous_close"))
    symbols = tuple(sorted(eligible.get_column("symbol").to_list()))
    close_utc = datetime.combine(
        trade_date,
        clock_time(16),
        tzinfo=EASTERN,
    ).astimezone(UTC)
    closing_bars = _fetch_alpaca_chunks(
        symbols,
        close_utc - timedelta(minutes=10),
        close_utc + timedelta(minutes=1),
    )
    closing_prices = (
        closing_bars.sort("ts_utc")
        .group_by("symbol")
        .tail(1)
        .select("symbol", "close")
        .join(eligible, on="symbol", how="inner")
        .with_columns(
            (pl.col("close") / pl.col("previous_close") - 1).alias(
                "close_return"
            )
        )
        .sort("close_return", descending=True)
        .head(100)
    )
    candidate_symbols = tuple(closing_prices.get_column("symbol").to_list())
    regular_open = datetime.combine(
        trade_date,
        clock_time(9, 30),
        tzinfo=EASTERN,
    ).astimezone(UTC)
    day_bars = _fetch_alpaca_chunks(
        candidate_symbols,
        regular_open,
        close_utc + timedelta(minutes=1),
    )
    return (
        day_bars.sort("ts_utc")
        .group_by("symbol")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
        )
        .with_columns(pl.lit(trade_date).alias("trade_date"))
    )


def _fetch_close_frame(
    trade_date: date,
    universe: pl.DataFrame,
    *,
    attempts: int,
) -> pl.DataFrame:
    for attempt in range(attempts):
        try:
            frame = fetch_grouped_daily(trade_date)
            if frame.height >= 1_000:
                return frame
        except DownloadError as exc:
            if "HTTP 403" in str(exc):
                break
        if attempt + 1 < attempts:
            time.sleep(30)
    try:
        return _fetch_alpaca_close_frame(trade_date, universe)
    except Exception as exc:
        raise RuntimeError(
            "Massive close data was unavailable and Alpaca SIP fallback failed"
        ) from exc


def _top_movers(
    current: pl.DataFrame,
    universe: pl.DataFrame,
    *,
    limit: int,
) -> pl.DataFrame:
    eligible = universe.filter(
        pl.col("precheck_pass")
        & (pl.col("security_type") == "CS")
        & (pl.col("price") >= MINIMUM_PRICE)
        & (pl.col("adv_usd") >= REQUIRED_ADV_USD)
    ).select(
        "symbol",
        pl.col("price").alias("previous_close"),
        "adv_usd",
        "atr_pct",
    )
    return (
        current.join(eligible, on="symbol", how="inner")
        .with_columns(
            (pl.col("close") / pl.col("previous_close") - 1).alias(
                "close_return"
            ),
            (pl.col("close") * pl.col("volume")).alias("dollar_volume"),
        )
        .filter(
            (pl.col("close_return") > 0)
            & (pl.col("dollar_volume") >= REQUIRED_DOLLAR_VOLUME)
        )
        .sort("close_return", descending=True)
        .head(limit)
    )


def _render_review(
    *,
    trade_date: date,
    review: pl.DataFrame,
    news_error: str | None,
) -> str:
    lines = [
        f"【利弗莫尔｜{trade_date.isoformat()}收盘漏选复盘】",
        "口径：普通股、前收不低于5、20日平均成交额不低于2000万、"
        "当日成交额不低于2000万。",
        "",
        "当日强势股与漏选原因：",
    ]
    counts: dict[str, int] = {}
    for row in review.iter_rows(named=True):
        symbol = str(row["symbol"])
        category = str(row["root_cause"])
        counts[category] = counts.get(category, 0) + 1
        lines.append(
            f"{int(row['opportunity_rank'])}. {symbol} "
            f"{float(row['close_return']) * 100:+.2f}%："
            f"{row['root_cause_detail']}"
        )

    lines.extend(
        [
            "",
            "归因结论：",
            f"- 已选中：{counts.get('selected', 0)}只",
            f"- 硬闸主动放弃：{counts.get('intentional_gate', 0)}只",
            f"- 盘中新增催化、盘前不可知：{counts.get('late_catalyst', 0)}只",
            f"- 新闻抓取/分类疑似缺口："
            f"{counts.get('data_or_classifier_gap', 0)}只",
            f"- 技术/资金流因子缺口：{counts.get('factor_gap', 0)}只",
            f"- 证据不足待复核：{counts.get('incomplete_evidence', 0)}只",
        ]
    )
    if news_error is not None:
        lines.append(f"- 新闻源本次不可用：{news_error}，相关归因暂标为待复核")
    lines.extend(
        [
            "",
            "改进原则：只把重复出现的漏选模式送入沙盒回测；"
            "单日偶发拉升不直接修改生产因子。",
            "本复盘只用于研究，不构成次日追涨建议。",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--push", action="store_true")
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    if args.top <= 0 or args.attempts <= 0:
        raise ValueError("top and attempts must be positive")
    config = None
    if args.push:
        if args.config is None:
            raise ValueError("--config is required with --push")
        config = _load_push_config(args.config)
        if config.trade_date != args.trade_date:
            raise ValueError("config trade date does not match")

    close_utc = datetime.combine(
        args.trade_date,
        clock_time(16),
        tzinfo=EASTERN,
    ).astimezone(UTC)
    if datetime.now(UTC) < close_utc:
        raise RuntimeError("postclose review cannot run before 16:00 ET")

    universe_path = _latest_snapshot(
        args.data_root,
        prefix="kernel.universe.daily_precheck",
        date_column="asof_date",
        trade_date=_previous_xnys_session(args.trade_date),
    )
    gates_path = _latest_snapshot(
        args.data_root,
        prefix="kernel.universe.selection_gates",
        date_column="session_date",
        trade_date=args.trade_date,
    )
    universe = pl.read_parquet(universe_path)
    gates = pl.read_parquet(gates_path)
    current = _fetch_close_frame(
        args.trade_date,
        universe,
        attempts=args.attempts,
    )
    movers = _top_movers(current, universe, limit=args.top)
    if movers.is_empty():
        raise RuntimeError("no liquid positive movers found")

    news_error: str | None = None
    try:
        news = fetch_alpaca_news(
            close_utc - timedelta(days=4),
            close_utc + timedelta(minutes=10),
        )
    except DownloadError as exc:
        news = pl.DataFrame(
            schema={
                "published_utc": pl.Datetime("us", "UTC"),
                "symbols": pl.List(pl.String),
                "headline": pl.String,
            }
        )
        news_error = f"{type(exc).__name__}"

    review = build_intraday_selection_postmortem(
        trade_date=args.trade_date,
        movers=movers,
        gates=gates,
        news=news,
        news_complete=news_error is None,
    )
    body = _render_review(
        trade_date=args.trade_date,
        review=review,
        news_error=news_error,
    )
    universe_snapshot = DatasetSnapshot.model_validate_json(
        (universe_path.parent / "manifest.json").read_text(encoding="utf-8")
    ).assert_usable()
    gates_snapshot = DatasetSnapshot.model_validate_json(
        (gates_path.parent / "manifest.json").read_text(encoding="utf-8")
    ).assert_usable()
    duplicates = review.height - review.get_column("symbol").n_unique()
    expected_ranks = list(range(1, review.height + 1))
    actual_ranks = review.get_column("opportunity_rank").to_list()
    checks = (
        DataQualityCheck(
            name="unique_symbol",
            severity=QualitySeverity.CRITICAL,
            passed=duplicates == 0,
            observed=str(duplicates),
            expected="0",
            provenance="intraday_selection_postmortem|symbol_uniqueness.v1",
        ),
        DataQualityCheck(
            name="exact_session",
            severity=QualitySeverity.CRITICAL,
            passed=review.get_column("session_date").unique().to_list()
            == [args.trade_date],
            observed=",".join(
                str(value)
                for value in review.get_column("session_date").unique().to_list()
            ),
            expected=args.trade_date.isoformat(),
            provenance="intraday_selection_postmortem|session_identity.v1",
        ),
        DataQualityCheck(
            name="consecutive_opportunity_rank",
            severity=QualitySeverity.CRITICAL,
            passed=actual_ranks == expected_ranks,
            observed=str(actual_ranks),
            expected=str(expected_ranks),
            provenance="intraday_selection_postmortem|rank_integrity.v1",
        ),
        DataQualityCheck(
            name="no_production_mutation",
            severity=QualitySeverity.CRITICAL,
            passed=not review.get_column("production_change_allowed").any(),
            observed=str(review.get_column("production_change_allowed").any()),
            expected="False",
            provenance="intraday_selection_postmortem|research_only.v1",
        ),
    )
    snapshot, review_path = persist_snapshot(
        review,
        root=args.data_root,
        source=REVIEW_SOURCE,
        schema_version=REVIEW_SCHEMA_VERSION,
        checks=checks,
        parent_snapshot_ids=(
            universe_snapshot.dataset_id,
            gates_snapshot.dataset_id,
        ),
    )
    snapshot.assert_usable()
    if args.push:
        if config is None:
            raise AssertionError("push config was not loaded")
        message_id = _send_vps(
            config.channel_id,
            Signal(
                event="postclose_missed_movers_review",
                symbol="MARKET",
                reason=f"{args.trade_date}:close_review",
                message=body,
                dedupe_key=f"postclose-review:{args.trade_date}",
            ),
        )
        result = {
            "ok": True,
            "pushed": True,
            "sender_type": "bot",
            "message_id": message_id,
            "mover_count": movers.height,
            "dataset_id": snapshot.dataset_id,
            "path": str(review_path),
        }
    else:
        result = {
            "ok": True,
            "pushed": False,
            "mover_count": movers.height,
            "body": body,
            "dataset_id": snapshot.dataset_id,
            "path": str(review_path),
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
