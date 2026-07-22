from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime

import polars as pl

MIN_NEWS_WORDS = 25
MAX_NEWS_SYMBOLS = 3
EVERGREEN_PHRASES = (
    "here s how much",
    "if you invested",
    "would have",
    "invested in",
    "over the past 5 years",
    "over the past 10 years",
)
LEGAL_SOLICITATION_PHRASES = (
    "class action",
    "lead plaintiff",
    "investor counsel",
    "securities fraud",
    "shareholder alert",
    "law firm",
    "rosen a",
    "pomerantz",
    "faruqi",
    "bronstein gewirtz",
    "kessler topaz",
)


def _normalized_words(value: str | None) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.findall(r"[a-z0-9]+", normalized)


def _fingerprint(headline: str | None, symbols: list[str]) -> str:
    material = " ".join(_normalized_words(headline)) + "|" + ",".join(sorted(symbols))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _category(row: dict[str, object]) -> str:
    subtype = str(row.get("event_subtype") or "").upper()
    form_items = set(_string_list(row.get("form_items")))
    words = set(
        _normalized_words(
            f"{row.get('headline') or ''} {row.get('summary') or ''} "
            f"{' '.join(_string_list(row.get('tags')))}"
        )
    )
    if form_items & {"1.03", "2.04", "2.05", "4.01", "4.02"}:
        return "distress_restatement"
    if "3.02" in form_items:
        return "financing_dilution"
    if subtype.startswith(("S-1", "S-3", "F-1", "F-3", "424B")) or words & {
        "offering",
        "financing",
        "dilution",
        "placement",
        "prospectus",
    }:
        return "financing_dilution"
    if subtype.startswith(("10-Q", "10-K", "20-F", "40-F")) or "2.02" in form_items:
        return "earnings"
    if "2.01" in form_items:
        return "merger_acquisition"
    if "5.02" in form_items:
        return "management_change"
    if "1.01" in form_items:
        return "contract_partnership"
    if words & {"earnings", "guidance", "revenue", "profit", "results"}:
        return "earnings"
    if words & {"fda", "clinical", "trial", "approval", "regulatory"}:
        return "regulatory_clinical"
    if words & {"acquisition", "acquire", "merger", "buyout", "takeover"}:
        return "merger_acquisition"
    if words & {"contract", "partnership", "agreement", "customer", "award"}:
        return "contract_partnership"
    if words & {"split", "dividend", "buyback", "repurchase", "spinoff"}:
        return "corporate_action"
    if words & {"ceo", "cfo", "director", "management", "resigns", "appointed"}:
        return "management_change"
    return "other_material" if row.get("event_type") == "sec_filing" else "general_news"


def _news_noise_reason(row: dict[str, object]) -> str | None:
    if row.get("event_type") != "news":
        return None
    publisher = " ".join(_normalized_words(str(row.get("publisher") or "")))
    headline = " ".join(_normalized_words(str(row.get("headline") or "")))
    if publisher == "the motley fool":
        return "editorial_analysis"
    if any(phrase in headline for phrase in LEGAL_SOLICITATION_PHRASES):
        return "legal_solicitation"
    if any(phrase in headline for phrase in EVERGREEN_PHRASES):
        return "evergreen_backward_looking"
    return None


def prepare_catalysts(events: pl.DataFrame, *, asof_utc: datetime) -> pl.DataFrame:
    """Apply deterministic Phase-1 cleaning without generating a sentiment score."""
    if asof_utc.tzinfo is None or asof_utc.utcoffset() is None:
        raise ValueError("asof_utc must be timezone-aware")
    rows = list(events.sort("published_utc", "source", "source_event_id").iter_rows(named=True))
    fingerprints: list[str] = []
    grouped_sources: dict[str, set[str]] = {}
    first_index: dict[str, int] = {}
    for index, row in enumerate(rows):
        symbols = _string_list(row.get("symbols"))
        fingerprint = _fingerprint(str(row.get("headline") or ""), symbols)
        fingerprints.append(fingerprint)
        grouped_sources.setdefault(fingerprint, set()).add(str(row["source"]))
        first_index.setdefault(fingerprint, index)

    output: list[dict[str, object]] = []
    for index, (row, fingerprint) in enumerate(zip(rows, fingerprints, strict=True)):
        symbols = _string_list(row.get("symbols"))
        word_count = len(
            _normalized_words(
                f"{row.get('headline') or ''} {row.get('summary') or ''}"
            )
        )
        published = row.get("published_utc")
        reason: str | None = None
        if not isinstance(published, datetime) or published > asof_utc:
            reason = "published_after_asof"
        elif row.get("event_type") == "news" and len(symbols) > MAX_NEWS_SYMBOLS:
            reason = "broad_multi_company_review"
        elif row.get("event_type") == "news" and word_count < MIN_NEWS_WORDS:
            reason = "insufficient_text"
        elif noise_reason := _news_noise_reason(row):
            reason = noise_reason
        elif first_index[fingerprint] != index:
            reason = "duplicate_event_chain"

        enriched = dict(row)
        sources = sorted(grouped_sources[fingerprint])
        enriched.update(
            {
                "word_count": word_count,
                "symbol_count": len(symbols),
                "content_fingerprint": fingerprint,
                "catalyst_category": _category(row),
                "eligible": reason is None,
                "exclude_reason": reason,
                "corroborating_sources": sources,
                "source_count": len(sources),
                "model_score": None,
                "model_provenance": None,
            }
        )
        output.append(enriched)
    return pl.DataFrame(output).sort("published_utc", "source", "source_event_id")


def select_overnight_catalysts(
    prepared: pl.DataFrame,
    *,
    schedule: pl.DataFrame,
    target_date: date,
    asof_utc: datetime,
) -> pl.DataFrame:
    """Assign post-close/weekend evidence to the next XNYS trading session."""
    if asof_utc.tzinfo is None or asof_utc.utcoffset() is None:
        raise ValueError("asof_utc must be timezone-aware")
    target = schedule.filter(pl.col("trade_date") == target_date)
    previous = schedule.filter(pl.col("trade_date") < target_date).sort("trade_date").tail(1)
    if target.height != 1 or previous.height != 1:
        raise ValueError("target and previous XNYS sessions are required")
    market_open = target.get_column("market_open_utc")[0]
    previous_close = previous.get_column("market_close_utc")[0]
    if not isinstance(market_open, datetime) or not isinstance(previous_close, datetime):
        raise ValueError("calendar timestamps are invalid")
    cutoff = min(asof_utc, market_open)
    return prepared.filter(
        pl.col("eligible")
        & (pl.col("published_utc") > previous_close)
        & (pl.col("published_utc") <= cutoff)
    ).with_columns(pl.lit(target_date).cast(pl.Date).alias("session_date"))


def build_catalyst_candidates(
    universe: pl.DataFrame, overnight_events: pl.DataFrame
) -> pl.DataFrame:
    """Attach eligible evidence only to symbols that passed the daily precheck."""
    required_universe = {"symbol", "precheck_pass"}
    missing = required_universe - set(universe.columns)
    if missing:
        raise ValueError(f"universe missing required columns: {sorted(missing)}")
    if overnight_events.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "event_count": pl.UInt32,
                "catalyst_categories": pl.List(pl.String),
                "latest_event_utc": pl.Datetime("ms", "UTC"),
                "evidence_sources": pl.List(pl.String),
                "independent_source_count": pl.UInt32,
                "evidence_event_ids": pl.List(pl.String),
                "evidence_provenance": pl.List(pl.String),
                "session_date": pl.Date,
                "model_score": pl.Float64,
                "model_provenance": pl.String,
            }
        )

    exploded = overnight_events.explode("symbols", empty_as_null=True).rename(
        {"symbols": "symbol"}
    )
    eligible_symbols = universe.filter(pl.col("precheck_pass")).select("symbol")
    return (
        exploded.join(eligible_symbols, on="symbol", how="inner")
        .group_by("symbol")
        .agg(
            pl.len().alias("event_count"),
            pl.col("catalyst_category")
            .unique()
            .sort()
            .alias("catalyst_categories"),
            pl.col("published_utc").max().alias("latest_event_utc"),
            pl.col("corroborating_sources")
            .list.explode(keep_nulls=False, empty_as_null=False)
            .unique()
            .sort()
            .alias("evidence_sources"),
            pl.concat_str("source", "source_event_id", separator=":")
            .unique()
            .sort()
            .alias("evidence_event_ids"),
            pl.col("provenance").unique().sort().alias("evidence_provenance"),
            pl.col("session_date").first(),
        )
        .with_columns(
            pl.col("evidence_sources")
            .list.len()
            .cast(pl.UInt32)
            .alias("independent_source_count"),
            pl.lit(None, dtype=pl.Float64).alias("model_score"),
            pl.lit(None, dtype=pl.String).alias("model_provenance"),
        )
        .sort("symbol")
    )
