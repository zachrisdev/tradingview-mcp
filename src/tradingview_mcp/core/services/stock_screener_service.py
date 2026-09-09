"""
Stock Screener Service — share-type (common/preferred) stock screening and
direct multi-symbol price lookups via tradingview_screener.

The discriminator mirrors TradingView's own symbol-search filter and was
verified against the live scanner (2026-07-13, tradingview-screener==3.0.0,
the version pinned in pyproject.toml): a "Common stock" / "Preferred stock"
row in the UI corresponds to ``col('type') == 'stock'`` plus
``col('typespecs').has(['common'])`` / ``(['preferred'])``. Measured then:
market 'america' returned 10,974 common / 676 preferred rows; 'korea' 2,637
common (KRX, prices in KRW).

IMPORTANT: do NOT add an ``is_primary`` filter to the preferred query —
preferred shares are almost never the primary listing, so the scan silently
returns 0 rows (america preferred: 676 without the filter, 0 with it). See
also futures_service._futures_query() for why bumping tradingview-screener
past 3.0.0 would inject exactly that kind of preset by default.

Fundamental filters (optional, AND-combined) were verified against the live
scanner metainfo + america/scan (2026-09-09):
  - market_cap_basic
  - total_revenue_yoy_growth_ttm  (percent, e.g. 15 = +15% YoY TTM)
  - free_cash_flow_ttm            (USD absolute)
  - net_debt_to_ebitda_fq
  - sector                        (text enum; "Finance" exists)
  - recommendation_total          (sum of buy/hold/sell/over/under — proxy
                                  for analyst coverage count; there is NO
                                  dedicated number_of_analysts field)

Fragility: this hits TradingView's undocumented scanner API
(https://scanner.tradingview.com/<market>/scan). Field names and operators
can change without notice.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

try:
    from tradingview_screener import Query, col
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

STOCK_TYPES = ("common", "preferred")

# Not exhaustive — any market name tradingview_screener accepts works. This
# list only feeds error messages so a caller who typos a country sees
# known-good options instead of a bare upstream error.
EXAMPLE_MARKETS = (
    "america", "korea", "germany", "brazil", "japan", "uk",
    "india", "turkey", "canada", "australia", "france", "hongkong",
)

_SCREEN_COLUMNS = (
    "name", "description", "exchange", "open", "high", "low", "close",
    "currency", "change", "dividends_yield_current", "market_cap_basic",
)

# Extra columns pulled when any fundamental filter is active (so results can
# be audited against the criteria that produced them).
_FUNDAMENTAL_COLUMNS = (
    "total_revenue_yoy_growth_ttm",
    "free_cash_flow_ttm",
    "price_free_cash_flow_ttm",
    "net_debt_to_ebitda_fq",
    "sector",
    "recommendation_total",
)

MAX_SCREEN_LIMIT = 2000
MAX_PRICE_TICKERS = 2000

# sort_by -> scanner column. Every entry must also be in selected columns.
# Field-tested motivation: without a server-side sort, "top dividend payers"
# can only be computed inside the market-cap-ranked window the caller pulled —
# the real leaders in the long tail stay invisible.
SORT_FIELDS = {
    "market_cap": "market_cap_basic",
    "dividend_yield": "dividends_yield_current",
    "change": "change",
    "price": "close",
    "revenue_growth": "total_revenue_yoy_growth_ttm",
    "fcf": "free_cash_flow_ttm",
    "net_debt_ebitda": "net_debt_to_ebitda_fq",
}

# Documented API gaps — surfaced in tool docs / envelopes so callers (e.g.
# incubator-protocol) know what cannot be automated from this endpoint alone.
API_LIMITATIONS = (
    "No dedicated number_of_analysts field; recommendation_total "
    "(buy+hold+sell+over+under) is the closest proxy.",
    "No standalone FCF yield field; price_free_cash_flow_ttm (P/FCF) is "
    "returned instead when fundamental columns are requested.",
    "Uses TradingView's undocumented scanner API — field names/operators "
    "may change or be rate-limited without notice.",
)


def _clean(value: Any) -> Any:
    """NaN -> None so rows serialize to JSON cleanly."""
    try:
        if value != value:  # noqa: PLR0124 — NaN is the only x != x
            return None
    except Exception:
        pass
    return value


def _require_available() -> None:
    if not _AVAILABLE:
        raise RuntimeError("tradingview_screener not installed")


def _normalize_sector_exclude(sector_exclude: Any) -> list[str]:
    if sector_exclude is None:
        return []
    if isinstance(sector_exclude, str):
        # Allow comma-separated string from clients that don't send JSON arrays.
        parts = [s.strip() for s in sector_exclude.split(",")]
        return [s for s in parts if s]
    if isinstance(sector_exclude, Sequence):
        return [str(s).strip() for s in sector_exclude if str(s).strip()]
    raise ValueError(
        "sector_exclude must be a string list (e.g. [\"Finance\"]) "
        f"or a comma-separated string, got {type(sector_exclude).__name__}"
    )


def _any_fundamental_filter(
    market_cap_min: Optional[float],
    market_cap_max: Optional[float],
    revenue_growth_yoy_min: Optional[float],
    fcf_positive: Optional[bool],
    fcf_min: Optional[float],
    net_debt_to_ebitda_max: Optional[float],
    sector_exclude: Sequence[str],
    analyst_count_max: Optional[float],
) -> bool:
    return any(
        v is not None
        for v in (
            market_cap_min,
            market_cap_max,
            revenue_growth_yoy_min,
            fcf_positive,
            fcf_min,
            net_debt_to_ebitda_max,
            analyst_count_max,
        )
    ) or bool(sector_exclude)


def screen_stocks(
    country: str = "america",
    stock_type: str = "common",
    limit: int = 50,
    exclude_otc: bool = True,
    compact: bool = False,
    sort_by: str = "market_cap",
    market_cap_min: Optional[float] = None,
    market_cap_max: Optional[float] = None,
    revenue_growth_yoy_min: Optional[float] = None,
    fcf_positive: Optional[bool] = None,
    fcf_min: Optional[float] = None,
    net_debt_to_ebitda_max: Optional[float] = None,
    sector_exclude: Optional[Sequence[str] | str] = None,
    analyst_count_max: Optional[float] = None,
) -> dict[str, Any]:
    """Screen stocks of one share type for a country market.

    Returns an envelope: total_matches is the market-wide count, rows are the
    top-N by sort_by (always descending, server-side, so the ranking covers
    the WHOLE filtered set, not just a client-side window).

    Optional fundamental filters are AND-combined. Units (verified live):
      - market_cap_* : USD
      - revenue_growth_yoy_min : percent (15 means +15% YoY TTM)
      - fcf_min / fcf_positive : free_cash_flow_ttm in USD
      - net_debt_to_ebitda_max : ratio on net_debt_to_ebitda_fq
      - sector_exclude : TradingView sector names (e.g. "Finance")
      - analyst_count_max : recommendation_total (proxy — see API_LIMITATIONS)

    exclude_otc (default True): TradingView's 'america' market means "trades
    on a US venue", not "is a US company" — without this filter ~1/3 of the
    top-100 is OTC foreign listings (Tencent, Roche, Nestlé...). Field-tested
    on day one: a user asking for "the biggest 100 US stocks" got 29 OTC rows.
    Pass exclude_otc=False to include them.

    compact (default False): True trims base fields to ticker/symbol/price/
    currency/change_percent. Fundamental columns used for filtering are kept
    so auditability is not lost.

    Deliberately NOT deduplicated across share classes (GOOG/GOOGL, BRK.A/
    BRK.B): those are distinct instruments with distinct real prices, and
    which one is "canonical" is a consumer-side decision, not a data-layer one.
    """
    _require_available()
    stock_type = (stock_type or "common").strip().lower()
    if stock_type not in STOCK_TYPES:
        raise ValueError(
            f"stock_type must be one of {list(STOCK_TYPES)}, got {stock_type!r}"
        )
    country = (country or "america").strip().lower()
    limit = max(1, min(int(limit), MAX_SCREEN_LIMIT))
    sort_key = (sort_by or "market_cap").strip().lower()
    sort_col = SORT_FIELDS.get(sort_key)
    if not sort_col:
        raise ValueError(
            f"sort_by must be one of {list(SORT_FIELDS)}, got {sort_by!r}"
        )

    sectors = _normalize_sector_exclude(sector_exclude)
    use_fundamentals = _any_fundamental_filter(
        market_cap_min,
        market_cap_max,
        revenue_growth_yoy_min,
        fcf_positive,
        fcf_min,
        net_debt_to_ebitda_max,
        sectors,
        analyst_count_max,
    )

    if market_cap_min is not None and market_cap_max is not None:
        if float(market_cap_min) > float(market_cap_max):
            raise ValueError("market_cap_min must be <= market_cap_max")
    if fcf_positive is not None and fcf_min is not None and bool(fcf_positive) and float(fcf_min) <= 0:
        # fcf_min is the stricter numeric bound when both are set.
        pass

    filters = [col("type") == "stock", col("typespecs").has([stock_type])]
    if exclude_otc:
        filters.append(col("exchange") != "OTC")

    if market_cap_min is not None and market_cap_max is not None:
        filters.append(col("market_cap_basic").between(float(market_cap_min), float(market_cap_max)))
    elif market_cap_min is not None:
        filters.append(col("market_cap_basic") >= float(market_cap_min))
    elif market_cap_max is not None:
        filters.append(col("market_cap_basic") <= float(market_cap_max))

    if revenue_growth_yoy_min is not None:
        filters.append(
            col("total_revenue_yoy_growth_ttm") >= float(revenue_growth_yoy_min)
        )

    # fcf_min wins when both are provided (stricter absolute floor).
    if fcf_min is not None:
        filters.append(col("free_cash_flow_ttm") >= float(fcf_min))
    elif fcf_positive is True:
        filters.append(col("free_cash_flow_ttm") > 0)
    elif fcf_positive is False:
        filters.append(col("free_cash_flow_ttm") <= 0)

    if net_debt_to_ebitda_max is not None:
        filters.append(
            col("net_debt_to_ebitda_fq") <= float(net_debt_to_ebitda_max)
        )

    if sectors:
        filters.append(col("sector").not_in(list(sectors)))

    if analyst_count_max is not None:
        filters.append(col("recommendation_total") <= float(analyst_count_max))

    select_cols = list(_SCREEN_COLUMNS)
    if use_fundamentals or sort_col in _FUNDAMENTAL_COLUMNS:
        for c in _FUNDAMENTAL_COLUMNS:
            if c not in select_cols:
                select_cols.append(c)
    if sort_col not in select_cols:
        select_cols.append(sort_col)

    query = (
        Query()
        .set_markets(country)
        .select(*select_cols)
        .where(*filters)
        .order_by(sort_col, ascending=False)
        .limit(limit)
    )
    # get_scanner_data forwards kwargs to requests.post, which has no default
    # timeout — a stalled endpoint would hang the worker thread forever.
    total, df = query.get_scanner_data(timeout=20)

    applied_filters: dict[str, Any] = {}
    if market_cap_min is not None:
        applied_filters["market_cap_min"] = float(market_cap_min)
    if market_cap_max is not None:
        applied_filters["market_cap_max"] = float(market_cap_max)
    if revenue_growth_yoy_min is not None:
        applied_filters["revenue_growth_yoy_min"] = float(revenue_growth_yoy_min)
    if fcf_min is not None:
        applied_filters["fcf_min"] = float(fcf_min)
    elif fcf_positive is not None:
        applied_filters["fcf_positive"] = bool(fcf_positive)
    if net_debt_to_ebitda_max is not None:
        applied_filters["net_debt_to_ebitda_max"] = float(net_debt_to_ebitda_max)
    if sectors:
        applied_filters["sector_exclude"] = list(sectors)
    if analyst_count_max is not None:
        applied_filters["analyst_count_max"] = float(analyst_count_max)

    rows = []
    for r in df.to_dict("records"):
        row: dict[str, Any] = {
            "ticker": _clean(r.get("ticker")),
            "symbol": _clean(r.get("name")),
            "description": _clean(r.get("description")),
            "exchange": _clean(r.get("exchange")),
            "price": _clean(r.get("close")),
            "open": _clean(r.get("open")),
            "high": _clean(r.get("high")),
            "low": _clean(r.get("low")),
            "currency": _clean(r.get("currency")),
            "change_percent": _clean(r.get("change")),
            "dividend_yield": _clean(r.get("dividends_yield_current")),
            "market_cap": _clean(r.get("market_cap_basic")),
        }
        if use_fundamentals or sort_col in _FUNDAMENTAL_COLUMNS:
            # Always echo filter-relevant fundamentals when any fund filter
            # ran — even columns that weren't constrained — for auditability.
            row["revenue_growth_yoy_ttm"] = _clean(
                r.get("total_revenue_yoy_growth_ttm")
            )
            row["free_cash_flow_ttm"] = _clean(r.get("free_cash_flow_ttm"))
            row["price_free_cash_flow_ttm"] = _clean(
                r.get("price_free_cash_flow_ttm")
            )
            row["net_debt_to_ebitda_fq"] = _clean(r.get("net_debt_to_ebitda_fq"))
            row["sector"] = _clean(r.get("sector"))
            row["analyst_count"] = _clean(r.get("recommendation_total"))
        rows.append(row)

    if compact:
        keep = ["ticker", "symbol", "price", "currency", "change_percent"]
        if use_fundamentals or sort_col in _FUNDAMENTAL_COLUMNS:
            keep.extend(
                [
                    "market_cap",
                    "revenue_growth_yoy_ttm",
                    "free_cash_flow_ttm",
                    "price_free_cash_flow_ttm",
                    "net_debt_to_ebitda_fq",
                    "sector",
                    "analyst_count",
                ]
            )
        rows = [{k: r[k] for k in keep if k in r} for r in rows]

    result: dict[str, Any] = {
        "country": country,
        "stock_type": stock_type,
        "exclude_otc": exclude_otc,
        "sort_by": sort_key,
        "filters_applied": applied_filters,
        "total_matches": total,
        "returned": len(rows),
        "rows": rows,
    }
    if use_fundamentals:
        result["api_limitations"] = list(API_LIMITATIONS)
    return result


def fetch_stock_prices(tickers: str) -> dict[str, Any]:
    """Current price + daily % change for specific symbols.

    ``tickers`` is a comma-separated list in EXCHANGE:SYMBOL form, e.g.
    ``"NASDAQ:NVDA, KRX:005930"`` — the exchange prefix is required because
    the scanner's direct-ticker lookup is exchange-scoped.
    """
    _require_available()
    parsed = [t.strip().upper() for t in (tickers or "").split(",") if t.strip()]
    if not parsed:
        raise ValueError(
            "tickers required — comma-separated EXCHANGE:SYMBOL, "
            "e.g. 'NASDAQ:NVDA, KRX:005930'"
        )
    if len(parsed) > MAX_PRICE_TICKERS:
        raise ValueError(f"max {MAX_PRICE_TICKERS} tickers per call, got {len(parsed)}")
    malformed = [t for t in parsed if ":" not in t]
    if malformed:
        raise ValueError(
            f"tickers must be EXCHANGE:SYMBOL (e.g. NASDAQ:NVDA, KRX:005930); "
            f"invalid: {malformed}"
        )

    # .limit() is load-bearing: the scanner's default page size is 50, so
    # without it a 1,000-ticker request silently returns only 50 rows
    # (measured live 2026-07-14). With it, 1,000 prices come back in one
    # HTTP request in ~0.5s.
    _PRICE_COLUMNS = (
        "name", "description", "exchange", "open", "high", "low", "close",
        "currency", "change",
    )
    query = Query().set_tickers(*parsed).select(*_PRICE_COLUMNS).limit(len(parsed))
    _total, df = query.get_scanner_data(timeout=20)  # requests has no default timeout
    found: dict[str, dict[str, Any]] = {}
    for r in df.to_dict("records"):
        row = {
            "ticker": _clean(r.get("ticker")),
            "symbol": _clean(r.get("name")),
            "description": _clean(r.get("description")),
            "exchange": _clean(r.get("exchange")),
            "price": _clean(r.get("close")),
            "open": _clean(r.get("open")),
            "high": _clean(r.get("high")),
            "low": _clean(r.get("low")),
            "currency": _clean(r.get("currency")),
            "change_percent": _clean(r.get("change")),
        }
        if row["ticker"]:
            found[str(row["ticker"]).upper()] = row
    missing = [t for t in parsed if t not in found]
    return {
        "requested": len(parsed),
        "returned": len(found),
        "rows": list(found.values()),
        # Surface misses explicitly — a silent drop reads as "price service
        # is broken" to the caller, a named miss reads as "typo in my list".
        "not_found": missing,
    }
