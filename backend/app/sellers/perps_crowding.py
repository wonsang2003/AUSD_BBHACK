"""
Seller `perps_crowding` (demo vendor, 0.02 AUSD): is a crypto perp crowded long or short?

`GET /sell/perps-crowding?assets=BTC,ETH,SOL` (SPEC §4.3). Data: Hyperliquid
`metaAndAssetCtxs` (`app/data/hyperliquid.py`) — funding rate, open interest and
mark price for every listed perp, in one request.

The score, per asset:

    crowding_z = funding_z + 0.5 · sign(funding_z) · oi_change_z

    funding_z     how unusual the current hourly funding rate is
    oi_change_z   how unusual the latest change in open interest (in coins) is;
                  OI building up in the direction funding already points makes the
                  crowding stronger, OI unwinding makes it weaker
    label         crowding_z ≥ +1 → "crowded long", ≤ −1 → "crowded short", else "neutral"

Two ways to get the z-scores:
  * time series (preferred): once this process has ≥ 12 snapshots of an asset in its
    rolling in-memory window (one snapshot per ≥ 60 s, last 96 kept), z-scores compare
    the current funding / latest OI change with that asset's own recent history.
  * cross-sectional (cold start): compare the asset's funding with all liquid
    Hyperliquid perps right now, using a robust z (median and MAD, because funding
    across small coins is heavy-tailed). OI change is not observable from a single
    snapshot, so on a cold start crowding_z = funding_z and confidence is lower.
    Hyperliquid funding often sits exactly at its 0.00125 %/h baseline for most coins,
    which makes MAD = 0; the robust z then falls back to the mean absolute deviation
    (σ ≈ 1.2533 · MeanAD), so a stretched BTC funding rate is still visible.

The rolling window fills from paid calls (`compute`) and, if the app schedules it,
from `sample_now()` — a free background snapshot so the time-series method is ready
sooner. History lives in process memory and is lost on restart.

Agents read `assets[i].crowding_z` (positive = crowded long) — see
`app/agents/beliefs.update_from_perps_crowding`.

This module never imports the x402 server; `app/sellers/routers.py` wraps `compute`.
"""

from __future__ import annotations

import re
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from app.config import get_settings
from app.data.hyperliquid import PerpContext, fetch_perp_contexts, index_by_symbol
from app.sellers.expected_move import require_payout_address
from app.x402.offer import SellerOffer

# ─── offer ───────────────────────────────────────────────────────────────────

SELLER_ID = "perps_crowding"
SELLER_PATH = "/sell/perps-crowding"
PRICE_ATOMIC = 20_000  # 0.02 AUSD (6 decimals)
PAYOUT_SETTING_NAME = "SELLER_PAYTO_PERPS"   # .env variable holding this seller's payout address
DESCRIPTION = "Perps crowding (demo vendor): Hyperliquid funding + open-interest z-scores → crowded long/short/neutral"

# ─── parameters ──────────────────────────────────────────────────────────────

DEFAULT_ASSETS = ("BTC", "ETH", "SOL")
MAX_ASSETS_PER_CALL = 10
ASSET_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,15}$")

# ─── scoring rules ───────────────────────────────────────────────────────────

CROWDING_LABEL_THRESHOLD_Z = 1.0
OI_BUILDUP_WEIGHT = 0.5
LABEL_CROWDED_LONG = "crowded long"
LABEL_CROWDED_SHORT = "crowded short"
LABEL_NEUTRAL = "neutral"

METHOD_TIME_SERIES = "time_series"
METHOD_CROSS_SECTIONAL = "cross_sectional"

# Rolling in-memory window (per asset).
ROLLING_WINDOW_SNAPSHOTS = 96
MIN_SNAPSHOTS_FOR_TIME_SERIES = 12
MIN_SECONDS_BETWEEN_SNAPSHOTS = 60.0

# Cross-section: only perps with real trading count, so dead markets do not distort it.
MIN_CROSS_SECTION_VOLUME_USD = 1_000_000.0
MIN_CROSS_SECTION_SIZE = 5
MAD_TO_STANDARD_DEVIATION = 1.4826       # σ ≈ 1.4826 · MAD for normally distributed data
MEAN_AD_TO_STANDARD_DEVIATION = 1.2533   # σ ≈ 1.2533 · mean absolute deviation (normal data)

# Confidence: a cross-sectional read is weaker than a time-series read.
CROSS_SECTIONAL_CONFIDENCE = 0.5
TIME_SERIES_MIN_CONFIDENCE = 0.6
TIME_SERIES_MAX_CONFIDENCE = 0.9

PERCENT = 100.0
DATA_DELAY = "real-time (Hyperliquid public API, cached ≤ 15 s)"


def make_offer() -> SellerOffer:
    """What this demo vendor charges (0.02 AUSD) and where the AUSD goes (SELLER_PAYTO_PERPS).
    Raises SellerConfigError if that address is empty or invalid."""
    return SellerOffer(
        seller_id=SELLER_ID,
        path=SELLER_PATH,
        description=DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=require_payout_address(get_settings().seller_payto_perps, PAYOUT_SETTING_NAME),
    )


# ─── statistics ──────────────────────────────────────────────────────────────

def zscore(value: float, samples: list[float]) -> float:
    """
    Standard z-score of `value` against `samples`: (value − mean) / sample std-dev.
    Returns 0.0 when there are fewer than 2 samples or no spread (nothing is unusual).
    """
    if len(samples) < 2:
        return 0.0
    spread = statistics.stdev(samples)
    if spread == 0:
        return 0.0
    return (value - statistics.fmean(samples)) / spread


def robust_spread(samples: list[float]) -> float:
    """
    Outlier-resistant estimate of the standard deviation of `samples`:

        1.4826 · MAD,       MAD = median(|x − median|)        (normal case)
        1.2533 · MeanAD,    MeanAD = mean(|x − median|)       (when MAD = 0)

    MAD is 0 whenever more than half the samples are identical — common for
    Hyperliquid funding, which many coins sit on at exactly the baseline rate.
    Returns 0.0 only when every sample is identical.
    """
    center = statistics.median(samples)
    deviations = [abs(sample - center) for sample in samples]
    mad = statistics.median(deviations)
    if mad > 0:
        return MAD_TO_STANDARD_DEVIATION * mad
    return MEAN_AD_TO_STANDARD_DEVIATION * statistics.fmean(deviations)


def robust_zscore(value: float, samples: list[float]) -> float:
    """
    Robust z-score: (value − median) / robust_spread(samples).
    Outliers barely move the median or the spread, so one wild small-cap funding
    rate cannot hide BTC's signal. Returns 0.0 with < 2 samples or no spread at all.
    """
    if len(samples) < 2:
        return 0.0
    spread = robust_spread(samples)
    if spread == 0:
        return 0.0
    return (value - statistics.median(samples)) / spread


def fractional_change(old: float, new: float) -> float | None:
    """(new − old) / old, or None when old is not positive."""
    if old <= 0:
        return None
    return (new - old) / old


def fractional_changes(values: list[float]) -> list[float]:
    """Successive fractional changes of a series (pairs with a non-positive start are skipped)."""
    changes: list[float] = []
    for old, new in zip(values, values[1:]):
        change = fractional_change(old, new)
        if change is not None:
            changes.append(change)
    return changes


def _sign(value: float) -> float:
    """−1.0, 0.0 or +1.0."""
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def combine_crowding_z(funding_z: float, oi_change_z: float | None) -> float:
    """
    crowding_z = funding_z + 0.5 · sign(funding_z) · oi_change_z

    OI building up (oi_change_z > 0) pushes the score further in the direction funding
    already points; OI unwinding pulls it back toward 0. Without OI data → funding_z.
    """
    if oi_change_z is None:
        return funding_z
    return funding_z + OI_BUILDUP_WEIGHT * _sign(funding_z) * oi_change_z


def crowding_label(crowding_z: float) -> str:
    """≥ +1 → crowded long, ≤ −1 → crowded short, otherwise neutral."""
    if crowding_z >= CROWDING_LABEL_THRESHOLD_Z:
        return LABEL_CROWDED_LONG
    if crowding_z <= -CROWDING_LABEL_THRESHOLD_Z:
        return LABEL_CROWDED_SHORT
    return LABEL_NEUTRAL


# ─── rolling in-memory history ───────────────────────────────────────────────

@dataclass(frozen=True)
class Snapshot:
    """What we remember about one asset at one moment."""

    taken_at: float              # unix seconds
    funding_hourly: float
    open_interest_coins: float   # coins, not USD, so price moves do not look like position changes


class PerpsHistory:
    """Per-asset rolling window of snapshots, kept in process memory (lost on restart)."""

    def __init__(
        self,
        window: int = ROLLING_WINDOW_SNAPSHOTS,
        min_seconds_between: float = MIN_SECONDS_BETWEEN_SNAPSHOTS,
    ) -> None:
        self._window = window
        self._min_seconds_between = min_seconds_between
        self._by_asset: dict[str, deque[Snapshot]] = {}

    def snapshots(self, asset: str) -> list[Snapshot]:
        """Snapshots of `asset`, oldest first."""
        return list(self._by_asset.get(asset.upper(), ()))

    def record_all(self, contexts: list[PerpContext], taken_at: float) -> None:
        """Add a snapshot of every asset, unless its last snapshot is younger than the minimum spacing."""
        for context in contexts:
            series = self._by_asset.setdefault(context.name.upper(), deque(maxlen=self._window))
            if series and taken_at - series[-1].taken_at < self._min_seconds_between:
                continue
            series.append(
                Snapshot(
                    taken_at=taken_at,
                    funding_hourly=context.funding_hourly,
                    open_interest_coins=context.open_interest_coins,
                )
            )

    def clear(self) -> None:
        """Forget everything (tests)."""
        self._by_asset.clear()


_HISTORY = PerpsHistory()


# ─── scoring one asset ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetCrowding:
    """The crowding read for one asset (the JSON row in `assets`)."""

    asset: str
    method: str
    crowding_z: float
    label: str
    funding_hourly: float
    funding_annualized_pct: float
    funding_z: float
    open_interest_usd: float
    oi_change_pct: float | None
    oi_change_z: float | None
    mark_px: float
    day_volume_usd: float
    snapshots_used: int
    confidence: float

    def to_dict(self) -> dict:
        """Plain JSON form of this row."""
        return asdict(self)


def time_series_confidence(snapshot_count: int) -> float:
    """Grows linearly from 0.6 (at the 12-snapshot minimum) to 0.9 (full 96-snapshot window)."""
    span = ROLLING_WINDOW_SNAPSHOTS - MIN_SNAPSHOTS_FOR_TIME_SERIES
    progress = min(1.0, max(0.0, (snapshot_count - MIN_SNAPSHOTS_FOR_TIME_SERIES) / span))
    return TIME_SERIES_MIN_CONFIDENCE + (TIME_SERIES_MAX_CONFIDENCE - TIME_SERIES_MIN_CONFIDENCE) * progress


def _row(
    context: PerpContext,
    *,
    method: str,
    funding_z: float,
    oi_change: float | None,
    oi_change_z: float | None,
    snapshots_used: int,
    confidence: float,
) -> AssetCrowding:
    """Assemble an AssetCrowding from the computed pieces."""
    crowding_z = combine_crowding_z(funding_z, oi_change_z)
    return AssetCrowding(
        asset=context.name.upper(),
        method=method,
        crowding_z=crowding_z,
        label=crowding_label(crowding_z),
        funding_hourly=context.funding_hourly,
        funding_annualized_pct=context.funding_annualized * PERCENT,
        funding_z=funding_z,
        open_interest_usd=context.open_interest_usd,
        oi_change_pct=oi_change * PERCENT if oi_change is not None else None,
        oi_change_z=oi_change_z,
        mark_px=context.mark_px,
        day_volume_usd=context.day_notional_volume_usd,
        snapshots_used=snapshots_used,
        confidence=confidence,
    )


def score_time_series(context: PerpContext, history: list[Snapshot]) -> AssetCrowding:
    """
    Score against the asset's own past snapshots:
        funding_z   = z(current funding, past funding values)
        oi_change_z = z(OI change since the last snapshot, past snapshot-to-snapshot OI changes)
    """
    funding_z = zscore(context.funding_hourly, [snap.funding_hourly for snap in history])
    latest_change = fractional_change(history[-1].open_interest_coins, context.open_interest_coins)
    past_changes = fractional_changes([snap.open_interest_coins for snap in history])
    oi_change_z = zscore(latest_change, past_changes) if latest_change is not None else None
    return _row(
        context,
        method=METHOD_TIME_SERIES,
        funding_z=funding_z,
        oi_change=latest_change,
        oi_change_z=oi_change_z,
        snapshots_used=len(history),
        confidence=time_series_confidence(len(history)),
    )


def cross_section(contexts: list[PerpContext]) -> list[PerpContext]:
    """
    The comparison set for a cold start: perps with ≥ $1M 24 h volume. If fewer than
    5 qualify (unusual market), fall back to every listed perp.
    """
    liquid = [c for c in contexts if c.day_notional_volume_usd >= MIN_CROSS_SECTION_VOLUME_USD]
    return liquid if len(liquid) >= MIN_CROSS_SECTION_SIZE else list(contexts)


def score_cross_sectional(context: PerpContext, universe: list[PerpContext]) -> AssetCrowding:
    """Cold start: funding_z = robust z of this asset's funding across `universe`; OI change unknown."""
    funding_z = robust_zscore(context.funding_hourly, [peer.funding_hourly for peer in universe])
    return _row(
        context,
        method=METHOD_CROSS_SECTIONAL,
        funding_z=funding_z,
        oi_change=None,
        oi_change_z=None,
        snapshots_used=0,
        confidence=CROSS_SECTIONAL_CONFIDENCE,
    )


def score_assets(
    wanted: list[str], contexts: list[PerpContext], history: PerpsHistory
) -> tuple[list[AssetCrowding], list[str]]:
    """
    Score each wanted asset with the time-series method when its history is long
    enough, else cross-sectionally. Returns (rows, symbols not listed on Hyperliquid).
    Does not modify `history` — the caller records the new snapshot afterwards.
    """
    by_symbol = index_by_symbol(contexts)
    universe = cross_section(contexts)
    rows: list[AssetCrowding] = []
    not_found: list[str] = []
    for symbol in wanted:
        context = by_symbol.get(symbol)
        if context is None:
            not_found.append(symbol)
            continue
        past = history.snapshots(symbol)
        if len(past) >= MIN_SNAPSHOTS_FOR_TIME_SERIES:
            rows.append(score_time_series(context, past))
        else:
            rows.append(score_cross_sectional(context, universe))
    return rows, not_found


# ─── request handling ────────────────────────────────────────────────────────

def parse_assets_param(params: dict) -> list[str]:
    """
    `assets=BTC,eth` → ["BTC", "ETH"] (upper-cased, de-duplicated, order kept).
    Missing/empty → BTC, ETH, SOL. Raises ValueError on bad symbols or > 10 assets.
    """
    raw = str(params.get("assets") or "").strip()
    if not raw:
        return list(DEFAULT_ASSETS)
    symbols: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        if not ASSET_SYMBOL_PATTERN.match(symbol):
            raise ValueError(f"invalid asset symbol {part.strip()!r}")
        if symbol not in symbols:
            symbols.append(symbol)
    if len(symbols) > MAX_ASSETS_PER_CALL:
        raise ValueError(f"at most {MAX_ASSETS_PER_CALL} assets per call, got {len(symbols)}")
    return symbols or list(DEFAULT_ASSETS)


def overall_confidence(rows: list[AssetCrowding]) -> float:
    """Mean of the per-asset confidences (0 when nothing was scored)."""
    if not rows:
        return 0.0
    return statistics.fmean(row.confidence for row in rows)


def build_response(
    wanted: list[str],
    rows: list[AssetCrowding],
    not_found: list[str],
    cross_section_size: int,
    now: datetime,
) -> dict:
    """The JSON body returned to a paying buyer."""
    return {
        "seller_id": SELLER_ID,
        "assets": [row.to_dict() for row in rows],
        "not_found": not_found,
        "method": (
            "crowding_z = funding_z + 0.5·sign(funding_z)·oi_change_z; label ±1. "
            f"time_series: z vs this asset's last ≤{ROLLING_WINDOW_SNAPSHOTS} in-memory snapshots "
            f"(needs ≥{MIN_SNAPSHOTS_FOR_TIME_SERIES}); cross_sectional (cold start): robust z (median/MAD) "
            "of hourly funding across liquid Hyperliquid perps, OI change unavailable"
        ),
        "inputs_used": {
            "source": "Hyperliquid POST /info metaAndAssetCtxs",
            "assets_requested": wanted,
            "cross_section_size": cross_section_size,
            "snapshots_per_asset": {row.asset: row.snapshots_used for row in rows},
        },
        "computed_at": now.isoformat(timespec="seconds"),
        "confidence": overall_confidence(rows),
        "data_delay": DATA_DELAY,
    }


async def compute(params: dict) -> dict:
    """
    Fetch Hyperliquid, score the requested assets, then remember this snapshot for next time.

    Raises ValueError for bad parameters or when no requested asset is listed (a buyer
    error, so the x402 server should answer 400 without settling), and UpstreamError
    when Hyperliquid is unreachable.
    """
    wanted = parse_assets_param(params)
    contexts = await fetch_perp_contexts()
    now = datetime.now(timezone.utc)
    rows, not_found = score_assets(wanted, contexts, _HISTORY)
    _HISTORY.record_all(contexts, now.timestamp())
    if not rows:
        raise ValueError(f"none of {wanted} is a listed Hyperliquid perp")
    return build_response(wanted, rows, not_found, len(cross_section(contexts)), now)


async def sample_now() -> int:
    """
    Free background snapshot (no buyer, no payment): fetch Hyperliquid and add one
    snapshot per asset to the rolling window. Returns how many perps were seen.
    A scheduler may call this every few minutes so time-series scores are ready sooner.
    """
    contexts = await fetch_perp_contexts()
    _HISTORY.record_all(contexts, datetime.now(timezone.utc).timestamp())
    return len(contexts)


def reset_history() -> None:
    """Forget the in-memory window (tests, or after a long outage)."""
    _HISTORY.clear()
