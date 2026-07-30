from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
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

_AMOUNT = r"\$?(-?\d+(?:\.\d+)?)\s*([KMBT]?)"


@dataclass(frozen=True)
class EarningsHeadlineMetrics:
    structured: bool
    actual_eps_surprise: float | None
    actual_revenue_surprise: float | None
    forward_eps_vs_consensus: float | None
    forward_revenue_vs_consensus: float | None
    eps_guidance_raise: float | None
    revenue_guidance_raise: float | None

    @property
    def actual_layer(self) -> bool:
        return (
            self.actual_eps_surprise is not None
            or self.actual_revenue_surprise is not None
        )

    @property
    def forward_layer(self) -> bool:
        return (
            self.forward_eps_vs_consensus is not None
            or self.forward_revenue_vs_consensus is not None
        )

    @property
    def raise_layer(self) -> bool:
        return (
            self.eps_guidance_raise is not None
            or self.revenue_guidance_raise is not None
        )


def _scaled_amount(value: str, suffix: str, inherited_suffix: str = "") -> float:
    multipliers = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    selected_suffix = suffix.upper() or inherited_suffix.upper()
    return float(value) * multipliers[selected_suffix]


def _ratio_from_match(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    left_suffix = match.group(2)
    right_suffix = match.group(4)
    left = _scaled_amount(match.group(1), left_suffix, right_suffix)
    right = _scaled_amount(match.group(3), right_suffix, left_suffix)
    if right == 0:
        return None
    return left / right - 1


def _range_vs_consensus(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    suffixes = (match.group(2), match.group(4), match.group(6))
    inherited = next((value for value in reversed(suffixes) if value), "")
    low = _scaled_amount(match.group(1), match.group(2), inherited)
    high = _scaled_amount(match.group(3), match.group(4), inherited)
    consensus = _scaled_amount(match.group(5), match.group(6), inherited)
    if consensus == 0:
        return None
    return ((low + high) / 2) / consensus - 1


def _raised_range(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    suffixes = tuple(match.group(index) for index in (2, 4, 6, 8))
    inherited = next((value for value in reversed(suffixes) if value), "")
    old_low = _scaled_amount(match.group(1), match.group(2), inherited)
    old_high = _scaled_amount(match.group(3), match.group(4), inherited)
    new_low = _scaled_amount(match.group(5), match.group(6), inherited)
    new_high = _scaled_amount(match.group(7), match.group(8), inherited)
    old_midpoint = (old_low + old_high) / 2
    if old_midpoint == 0:
        return None
    return ((new_low + new_high) / 2) / old_midpoint - 1


def parse_earnings_headline(headline: str | None) -> EarningsHeadlineMetrics:
    """Parse common structured earnings wires without using a company whitelist."""

    text = unicodedata.normalize("NFKC", headline or "")
    flags = re.IGNORECASE
    eps_actual = re.search(
        rf"(?:Adj\.?\s*)?EPS\s+{_AMOUNT}\s+"
        rf"(?:Beats?|Miss(?:es|ed)?)\s+{_AMOUNT}\s+(?:Est(?:imate)?|Consensus)",
        text,
        flags,
    )
    revenue_actual = re.search(
        rf"(?:Sales|Revenue)\s+{_AMOUNT}\s+"
        rf"(?:Beats?|Miss(?:es|ed)?)\s+{_AMOUNT}\s+(?:Est(?:imate)?|Consensus)",
        text,
        flags,
    )
    eps_forward = re.search(
        rf"(?:Sees|Expects|Guides)[^;]*?EPS\s+{_AMOUNT}\s*-\s*{_AMOUNT}"
        rf"\s+vs\.?\s+{_AMOUNT}\s+Est",
        text,
        flags,
    )
    revenue_forward = re.search(
        rf"(?:Sees|Expects|Guides)[^;]*?(?:Sales|Revenue)\s+"
        rf"{_AMOUNT}\s*-\s*{_AMOUNT}\s+vs\.?\s+{_AMOUNT}\s+Est",
        text,
        flags,
    )
    eps_raise = re.search(
        rf"Raises[^;]*?EPS\s+Guidance\s+from\s+{_AMOUNT}\s*-\s*{_AMOUNT}"
        rf"\s+to\s+{_AMOUNT}\s*-\s*{_AMOUNT}",
        text,
        flags,
    )
    revenue_raise = re.search(
        rf"Raises[^;]*?(?:Sales|Revenue)\s+Guidance\s+from\s+"
        rf"{_AMOUNT}\s*-\s*{_AMOUNT}\s+to\s+{_AMOUNT}\s*-\s*{_AMOUNT}",
        text,
        flags,
    )
    metrics = EarningsHeadlineMetrics(
        structured=any(
            value is not None
            for value in (
                eps_actual,
                revenue_actual,
                eps_forward,
                revenue_forward,
                eps_raise,
                revenue_raise,
            )
        ),
        actual_eps_surprise=_ratio_from_match(eps_actual),
        actual_revenue_surprise=_ratio_from_match(revenue_actual),
        forward_eps_vs_consensus=_range_vs_consensus(eps_forward),
        forward_revenue_vs_consensus=_range_vs_consensus(revenue_forward),
        eps_guidance_raise=_raised_range(eps_raise),
        revenue_guidance_raise=_raised_range(revenue_raise),
    )
    return metrics


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
    if parse_earnings_headline(str(row.get("headline") or "")).structured:
        return "earnings"
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
        earnings = parse_earnings_headline(str(row.get("headline") or ""))
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
        elif (
            row.get("event_type") == "news"
            and word_count < MIN_NEWS_WORDS
            and not earnings.structured
        ):
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
                "earnings_structured": earnings.structured,
                "earnings_actual_layer": earnings.actual_layer,
                "earnings_forward_layer": earnings.forward_layer,
                "earnings_raise_layer": earnings.raise_layer,
                "earnings_actual_eps_surprise": earnings.actual_eps_surprise,
                "earnings_actual_revenue_surprise": (
                    earnings.actual_revenue_surprise
                ),
                "earnings_forward_eps_vs_consensus": (
                    earnings.forward_eps_vs_consensus
                ),
                "earnings_forward_revenue_vs_consensus": (
                    earnings.forward_revenue_vs_consensus
                ),
                "earnings_eps_guidance_raise": earnings.eps_guidance_raise,
                "earnings_revenue_guidance_raise": (
                    earnings.revenue_guidance_raise
                ),
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
                "earnings_event_count": pl.UInt32,
                "earnings_actual_layer": pl.Boolean,
                "earnings_forward_layer": pl.Boolean,
                "earnings_raise_layer": pl.Boolean,
                "earnings_actual_eps_surprise": pl.Float64,
                "earnings_actual_revenue_surprise": pl.Float64,
                "earnings_forward_eps_vs_consensus": pl.Float64,
                "earnings_forward_revenue_vs_consensus": pl.Float64,
                "earnings_eps_guidance_raise": pl.Float64,
                "earnings_revenue_guidance_raise": pl.Float64,
                "earnings_evidence_layers": pl.UInt8,
                "earnings_intensity_score": pl.Float64,
                "earnings_strength_confirmed": pl.Boolean,
                "model_score": pl.Float64,
                "model_provenance": pl.String,
            }
        )

    exploded = overnight_events.explode("symbols", empty_as_null=True).rename(
        {"symbols": "symbol"}
    )
    eligible_symbols = universe.filter(pl.col("precheck_pass")).select("symbol")
    grouped = (
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
            pl.col("earnings_structured")
            .sum()
            .cast(pl.UInt32)
            .alias("earnings_event_count"),
            pl.col("earnings_actual_layer").any(),
            pl.col("earnings_forward_layer").any(),
            pl.col("earnings_raise_layer").any(),
            pl.col("earnings_actual_eps_surprise").max(),
            pl.col("earnings_actual_revenue_surprise").max(),
            pl.col("earnings_forward_eps_vs_consensus").max(),
            pl.col("earnings_forward_revenue_vs_consensus").max(),
            pl.col("earnings_eps_guidance_raise").max(),
            pl.col("earnings_revenue_guidance_raise").max(),
        )
        .with_columns(
            pl.col("evidence_sources")
            .list.len()
            .cast(pl.UInt32)
            .alias("independent_source_count"),
            pl.lit(None, dtype=pl.Float64).alias("model_score"),
            pl.lit(None, dtype=pl.String).alias("model_provenance"),
        )
    )

    def positive(name: str) -> pl.Expr:
        return pl.col(name).fill_null(0.0).clip(0.0, 1.0)

    layers = (
        pl.col("earnings_actual_layer").cast(pl.UInt8)
        + pl.col("earnings_forward_layer").cast(pl.UInt8)
        + pl.col("earnings_raise_layer").cast(pl.UInt8)
    )
    magnitude_score = (
        positive("earnings_actual_eps_surprise").clip(0.0, 0.10) / 0.10 * 8
        + positive("earnings_actual_revenue_surprise").clip(0.0, 0.05)
        / 0.05
        * 6
        + positive("earnings_forward_eps_vs_consensus").clip(0.0, 0.08)
        / 0.08
        * 6
        + positive("earnings_forward_revenue_vs_consensus").clip(0.0, 0.04)
        / 0.04
        * 5
        + positive("earnings_eps_guidance_raise").clip(0.0, 0.08) / 0.08 * 6
        + positive("earnings_revenue_guidance_raise").clip(0.0, 0.04)
        / 0.04
        * 4
    )
    actual_breadth = (
        pl.when(
            (positive("earnings_actual_eps_surprise") > 0)
            & (positive("earnings_actual_revenue_surprise") > 0)
        )
        .then(20.0)
        .otherwise(0.0)
    )
    forward_breadth = (
        pl.when(
            (positive("earnings_forward_eps_vs_consensus") > 0)
            & (positive("earnings_forward_revenue_vs_consensus") > 0)
        )
        .then(20.0)
        .otherwise(0.0)
    )
    raise_breadth = (
        pl.when(
            (positive("earnings_eps_guidance_raise") > 0)
            & (positive("earnings_revenue_guidance_raise") > 0)
        )
        .then(25.0)
        .otherwise(0.0)
    )
    scored = grouped.with_columns(
        layers.alias("earnings_evidence_layers"),
        (magnitude_score + actual_breadth + forward_breadth + raise_breadth)
        .clip(0.0, 100.0)
        .alias("earnings_intensity_score"),
    )
    return scored.with_columns(
        (
            (pl.col("earnings_evidence_layers") >= 2)
            & (pl.col("earnings_intensity_score") >= 50.0)
        ).alias("earnings_strength_confirmed")
    ).sort("symbol")
