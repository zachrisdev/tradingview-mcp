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

Fundamental filters (optional, AND-combined) verified live 2026-09-09 /
extended 2026-10-13:
  - market_cap_basic (raw; unit is market-dependent — see market_cap_usd)
  - total_revenue_yoy_growth_ttm / total_revenue_yoy_growth_fq
  - gross_margin_ttm, total_revenue_ttm, free_cash_flow_ttm
  - net_debt_to_ebitda_fq, sector, industry, is_primary
  - recommendation_total (analyst-coverage proxy)

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
    "sweden",
)

# Dual-list checks (2026-10-13): market_cap_basic already USD-scale.
_USD_NORMALIZED_MARKETS = frozenset({"america", "germany", "canada"})

_SCREEN_COLUMNS = (
    "name", "description", "exchange", "open", "high", "low", "close",
    "currency", "change", "dividends_yield_current", "market_cap_basic",
)

# Pre-101 (legacy) scanner columns / response audit keys — keep byte-stable
# when only legacy filters or legacy fund sort_by are used.
_LEGACY_FUNDAMENTAL_COLUMNS = (
    "total_revenue_yoy_growth_ttm",
    "free_cash_flow_ttm",
    "price_free_cash_flow_ttm",
    "net_debt_to_ebitda_fq",
    "sector",
    "recommendation_total",
)

# TODO-Dev 101 extras — only when any *new* filter or *new* sort key is used.
_EXTENDED_FUNDAMENTAL_COLUMNS = (
    "total_revenue_yoy_growth_fq",
    "total_revenue_ttm",
    "total_revenue_fq",
    "gross_margin_ttm",
    "gross_margin_fq",  # scanner field exists but is typically empty — see compute
    "gross_profit_fq",
    "industry",
    "is_primary",
)

_FUNDAMENTAL_COLUMNS = _LEGACY_FUNDAMENTAL_COLUMNS + _EXTENDED_FUNDAMENTAL_COLUMNS

MAX_SCREEN_LIMIT = 2000
MAX_PRICE_TICKERS = 2000
# Post-filter / computed-sort scan: page through the TV where-set (ordered by
# market_cap_basic when the sort is computed) until exhausted or max_scan_rows.
_POST_FILTER_PAGE_SIZE = 1000
_DEFAULT_MAX_SCAN_ROWS = 5000
_MAX_SCAN_ROWS_CAP = 10000

# sort_by -> scanner column (TV server-side). Computed sorts are handled
# client-side after over-fetch (see _COMPUTED_SORT_FIELDS).
SORT_FIELDS = {
    "market_cap": "market_cap_basic",
    "dividend_yield": "dividends_yield_current",
    "change": "change",
    "price": "close",
    "revenue_growth": "total_revenue_yoy_growth_ttm",
    "revenue_growth_fq": "total_revenue_yoy_growth_fq",
    "gross_margin": "gross_margin_ttm",
    "fcf": "free_cash_flow_ttm",
    "net_debt_ebitda": "net_debt_to_ebitda_fq",
}

# Pre-101 fund sort keys (trigger legacy audit payload, not extended).
_LEGACY_FUND_SORT_KEYS = frozenset({"revenue_growth", "fcf", "net_debt_ebitda"})

# TODO-Dev 101 sort keys (trigger full extended audit payload).
_NEW_SORT_KEYS = frozenset({
    "revenue_growth_fq",
    "gross_margin",
    "gross_margin_fq",
    "rule_of_40",
})

# sort_by values that exist only after client-side compute (post-sort).
_COMPUTED_SORT_FIELDS = {
    "rule_of_40": "rule_of_40_fq",
    "gross_margin_fq": "gross_margin_fq",
}

# Documented API gaps — legacy subset is BC-stable for pre-101 fund callers.
_LEGACY_API_LIMITATIONS = (
    "No dedicated number_of_analysts field; recommendation_total "
    "(buy+hold+sell+over+under) is the closest proxy.",
    "No standalone FCF yield field; price_free_cash_flow_ttm (P/FCF) is "
    "returned instead when fundamental columns are requested.",
    "Uses TradingView's undocumented scanner API — field names/operators "
    "may change or be rate-limited without notice.",
)

_EXTENDED_API_LIMITATIONS = (
    "No previous-quarter revenue YoY on the scanner "
    "(total_revenue_yoy_growth_fq_1 etc. empty) — only latest FQ YoY.",
    "gross_margin_ttm is TradingView scanner gross_margin_ttm "
    "(= gross_profit_ttm / total_revenue_ttm). TV's Gross profit / Cost of "
    "goods sold often includes depreciation & amortization that the issuer "
    "reports below the gross-profit line (e.g. PAYS Q2'26: TV GM ~55% / "
    "TTM ~52% vs company-reported ~63% / ~62%; delta ≈ D&A). CGNT (~73%) "
    "and MNDY (~89%) match company figures — not a uniform scale bias. "
    "Cannot be reconciled from the scanner alone.",
    "gross_margin_fq scanner column is typically empty; we expose "
    "gross_margin_fq computed as gross_profit_fq / total_revenue_fq "
    "(same TV COGS definition). gross_margin_fq_min is a client post-filter.",
    "rule_of_40_min filters on rule_of_40_fq = FQ revenue YoY + TTM FCF "
    "margin (FCF remains TTM on the fq arm).",
    "market_cap (market_cap_basic) unit is market-dependent: america/"
    "germany/canada treated as USD-scale (market_cap_usd_* also pushed into "
    "TV where there); other markets convert via FX_IDC:{CCY}USD into "
    "market_cap_usd and filter client-side only. Raw market_cap_min/max "
    "always filter market_cap_basic.",
    "Post-filters (rule_of_40_min, market_cap_usd_*, gross_margin_fq_min) "
    "and computed sorts (rule_of_40, gross_margin_fq) page through the TV "
    f"where-set (page size {_POST_FILTER_PAGE_SIZE}, default max_scan_rows="
    f"{_DEFAULT_MAX_SCAN_ROWS}) in market_cap order, then filter/sort "
    "client-side. total_matches is the passer count among scanned rows. "
    "post_filter_incomplete=true (+ warning) when tv_total_matches > "
    "max_scan_rows — ranking covers only the first N market_cap rows. "
    "Native-column sorts (market_cap, revenue_growth, gross_margin, …) "
    "remain a single TV-side request.",
)

API_LIMITATIONS = _LEGACY_API_LIMITATIONS + _EXTENDED_API_LIMITATIONS

_LEGACY_FUND_AUDIT_KEYS = (
    "market_cap",
    "revenue_growth_yoy_ttm",
    "free_cash_flow_ttm",
    "price_free_cash_flow_ttm",
    "net_debt_to_ebitda_fq",
    "sector",
    "analyst_count",
)

_EXTENDED_FUND_AUDIT_KEYS = _LEGACY_FUND_AUDIT_KEYS + (
    "market_cap_usd",
    "revenue_growth_yoy_fq",
    "total_revenue_ttm",
    "gross_margin_ttm",
    "gross_margin_fq",
    "fcf_margin_ttm",
    "rule_of_40_ttm",
    "rule_of_40_fq",
    "industry",
    "is_primary",
)

# Alias used by compact keep-list on extended path.
_FUND_AUDIT_KEYS = _EXTENDED_FUND_AUDIT_KEYS

# New response keys that must NOT appear on the legacy fund path (BC-A / test c).
_NEW_RESPONSE_KEYS = frozenset({
    "market_cap_usd",
    "revenue_growth_yoy_fq",
    "total_revenue_ttm",
    "gross_margin_ttm",
    "gross_margin_fq",
    "fcf_margin_ttm",
    "rule_of_40_ttm",
    "rule_of_40_fq",
    "industry",
    "is_primary",
    "tv_total_matches",
    "fetch_limit",
    "max_scan_rows",
    "scanned_rows",
    "post_filter_incomplete",
    "warning",
})


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


def _any_legacy_fundamental_filter(
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


def _any_new_fundamental_filter(
    gross_margin_min: Optional[float],
    revenue_growth_yoy_fq_min: Optional[float],
    rule_of_40_min: Optional[float],
    primary_only: Optional[bool],
    market_cap_usd_min: Optional[float],
    market_cap_usd_max: Optional[float],
    gross_margin_fq_min: Optional[float] = None,
) -> bool:
    return any(
        v is not None
        for v in (
            gross_margin_min,
            revenue_growth_yoy_fq_min,
            rule_of_40_min,
            market_cap_usd_min,
            market_cap_usd_max,
            gross_margin_fq_min,
        )
    ) or bool(primary_only)


def _any_fundamental_filter(
    market_cap_min: Optional[float],
    market_cap_max: Optional[float],
    revenue_growth_yoy_min: Optional[float],
    fcf_positive: Optional[bool],
    fcf_min: Optional[float],
    net_debt_to_ebitda_max: Optional[float],
    sector_exclude: Sequence[str],
    analyst_count_max: Optional[float],
    gross_margin_min: Optional[float] = None,
    revenue_growth_yoy_fq_min: Optional[float] = None,
    rule_of_40_min: Optional[float] = None,
    primary_only: Optional[bool] = None,
    market_cap_usd_min: Optional[float] = None,
    market_cap_usd_max: Optional[float] = None,
    gross_margin_fq_min: Optional[float] = None,
) -> bool:
    return _any_legacy_fundamental_filter(
        market_cap_min,
        market_cap_max,
        revenue_growth_yoy_min,
        fcf_positive,
        fcf_min,
        net_debt_to_ebitda_max,
        sector_exclude,
        analyst_count_max,
    ) or _any_new_fundamental_filter(
        gross_margin_min,
        revenue_growth_yoy_fq_min,
        rule_of_40_min,
        primary_only,
        market_cap_usd_min,
        market_cap_usd_max,
        gross_margin_fq_min,
    )


def _needs_post_filter(
    rule_of_40_min: Optional[float],
    market_cap_usd_min: Optional[float],
    market_cap_usd_max: Optional[float],
    computed_sort: bool,
    gross_margin_fq_min: Optional[float] = None,
) -> bool:
    return (
        rule_of_40_min is not None
        or market_cap_usd_min is not None
        or market_cap_usd_max is not None
        or gross_margin_fq_min is not None
        or computed_sort
    )


def _post_filter_incomplete(scanned: int, tv_total: int) -> bool:
    """True when unscanned TV where-rows remain after post-filter paging.

    Meaning: passers of client-side post-filters may exist outside the
    scanned pages. Independent of whether the returned page was full.
    """
    try:
        return int(scanned) < int(tv_total)
    except (TypeError, ValueError):
        return False


def _resolve_gross_margin_fq(row: dict[str, Any]) -> Optional[float]:
    """Prefer scanner gross_margin_fq; else gross_profit_fq / total_revenue_fq."""
    direct = _clean(row.get("gross_margin_fq"))
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    return _safe_div_pct(
        _clean(row.get("gross_profit_fq")),
        _clean(row.get("total_revenue_fq")),
    )


def _tv_range_bounds(start: int, page_size: int) -> tuple[int, int]:
    """TradingView scanner ``range`` is ``[start, end)``, not ``[offset, count]``.

    tradingview-screener's ``.offset(a).limit(b)`` writes ``range=[a, b]``, so
    for a page of ``page_size`` rows starting at ``start`` callers must pass
    ``limit=start+page_size`` (end), not ``limit=page_size``.
    """
    start_i = max(0, int(start))
    size_i = max(0, int(page_size))
    return start_i, start_i + size_i


def _fetch_stock_scanner_page(
    country: str,
    select_cols: Sequence[str],
    filters: Sequence[Any],
    order_col: str,
    start: int,
    page_size: int,
) -> tuple[int, list[dict[str, Any]]]:
    """One scanner page. Empty/null ``data`` or invalid range → ``(total, [])``."""
    _require_available()
    start_i, end_i = _tv_range_bounds(start, page_size)
    if end_i <= start_i:
        return 0, []

    query = (
        Query()
        .set_markets(country)
        .select(*select_cols)
        .where(*filters)
        .order_by(order_col, ascending=False)
        # range = [start, end) — see _tv_range_bounds
        .offset(start_i)
        .limit(end_i)
    )
    try:
        payload = query.get_scanner_data_raw(timeout=20)
    except Exception as exc:
        msg = str(exc).lower()
        if "invalid range" in msg or "not iterable" in msg:
            return 0, []
        raise

    try:
        total = int(payload.get("totalCount") or 0)
    except (TypeError, ValueError, AttributeError):
        total = 0
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        # TV returns data:null past the end / on empty windows — exhausted page.
        return total, []

    columns = ["ticker", *list(select_cols)]
    records: list[dict[str, Any]] = []
    for row in data:
        values = [row.get("s"), *(row.get("d") or [])]
        rec = {
            columns[i]: (values[i] if i < len(values) else None)
            for i in range(len(columns))
        }
        records.append(rec)
    return total, records


def _fx_ccy_from_scanner_row(row: dict[str, Any]) -> Optional[str]:
    """Extract quote currency from an FX_IDC:{CCY}USD scanner row."""
    ticker = str(row.get("ticker") or "").upper()
    name = str(row.get("name") or "").upper()
    if ticker.startswith("FX_IDC:") and ticker.endswith("USD") and len(ticker) > 10:
        return ticker[len("FX_IDC:") : -3]
    if ":" in ticker:
        pair = ticker.split(":", 1)[1]
        if pair.endswith("USD") and len(pair) >= 6:
            return pair[:-3]
    if name.endswith("USD") and len(name) >= 6 and name.isalpha():
        return name[:-3]
    return None


def _fetch_fx_to_usd(currencies: Sequence[str]) -> dict[str, Optional[float]]:
    """Batch-fetch FX_IDC:{CCY}USD close rates. Missing → None (no crash)."""
    rates: dict[str, Optional[float]] = {"USD": 1.0}
    needed = sorted({
        str(c).strip().upper()
        for c in currencies
        if c and str(c).strip().upper() not in ("", "USD")
    })
    if not needed:
        return rates

    tickers = [f"FX_IDC:{ccy}USD" for ccy in needed]
    try:
        query = (
            Query()
            .set_tickers(*tickers)
            .select("name", "close")
            .limit(len(tickers))
        )
        _total, df = query.get_scanner_data(timeout=20)
    except Exception:
        for ccy in needed:
            rates[ccy] = None
        return rates

    by_ccy: dict[str, Any] = {}
    for r in df.to_dict("records"):
        ccy = _fx_ccy_from_scanner_row(r)
        close = _clean(r.get("close"))
        if ccy and close is not None:
            by_ccy[ccy] = close

    for ccy in needed:
        rates[ccy] = by_ccy.get(ccy)
    return rates


def _compute_market_cap_usd(
    market_cap: Any,
    currency: Any,
    country: str,
    fx_rates: dict[str, Optional[float]],
) -> tuple[Optional[float], Optional[str]]:
    """Return (market_cap_usd, limitation_note_or_None)."""
    if market_cap is None:
        return None, None
    try:
        cap = float(market_cap)
    except (TypeError, ValueError):
        return None, None

    if country in _USD_NORMALIZED_MARKETS:
        return cap, None

    ccy = str(currency).strip().upper() if currency else ""
    if not ccy:
        return None, "currency missing; market_cap_usd=null"
    if ccy == "USD":
        return cap, None

    rate = fx_rates.get(ccy)
    if rate is None:
        return None, f"FX rate unavailable for {ccy}USD; market_cap_usd=null"
    try:
        return cap * float(rate), None
    except (TypeError, ValueError):
        return None, f"FX rate invalid for {ccy}USD; market_cap_usd=null"


def _safe_div_pct(num: Any, den: Any) -> Optional[float]:
    if num is None or den is None:
        return None
    try:
        d = float(den)
        if d == 0:
            return None
        return 100.0 * float(num) / d
    except (TypeError, ValueError):
        return None


def _safe_sum(a: Any, b: Any) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        return float(a) + float(b)
    except (TypeError, ValueError):
        return None


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
    gross_margin_min: Optional[float] = None,
    revenue_growth_yoy_fq_min: Optional[float] = None,
    rule_of_40_min: Optional[float] = None,
    primary_only: bool = False,
    market_cap_usd_min: Optional[float] = None,
    market_cap_usd_max: Optional[float] = None,
    gross_margin_fq_min: Optional[float] = None,
    max_scan_rows: Optional[int] = None,
) -> dict[str, Any]:
    """Screen stocks of one share type for a country market.

    Returns an envelope: total_matches is the TV where count on native-sort
    paths; on post-filter / computed-sort paths it is the passer count among
    scanned rows. Rows are the top-N by sort_by. Post-filters
    (rule_of_40_min, market_cap_usd_*, gross_margin_fq_min) and computed
    sorts page the full TV where-set (page size 1000, up to max_scan_rows)
    in market_cap order, then compute → filter → re-sort → truncate.
    Native-column sorts stay a single TV request of size ``limit``.

    Optional fundamental filters are AND-combined. Units (verified live):
      - market_cap_min/max : raw market_cap_basic (NOT always USD — see
        market_cap_usd / API_LIMITATIONS)
      - market_cap_usd_min/max : computed USD cap (post-filter)
      - revenue_growth_yoy_min : percent TTM (15 = +15% YoY TTM)
      - revenue_growth_yoy_fq_min : percent latest quarter YoY
      - gross_margin_min : gross_margin_ttm percent (TV COGS definition —
        may include D&A; see API_LIMITATIONS)
      - gross_margin_fq_min : computed gross_margin_fq percent (post-filter)
      - rule_of_40_min : rule_of_40_fq (FQ YoY + TTM FCF margin)
      - fcf_min / fcf_positive : free_cash_flow_ttm absolute
      - net_debt_to_ebitda_max : ratio on net_debt_to_ebitda_fq
      - sector_exclude : TradingView sector names (e.g. "Finance")
      - analyst_count_max : recommendation_total (proxy — see API_LIMITATIONS)
      - primary_only : is_primary==True (common stock only; preferred raises)
      - max_scan_rows : hard cap for post-filter / computed-sort paging
        (default 5000, max 10000)

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
    if primary_only and stock_type == "preferred":
        raise ValueError(
            "primary_only=True is not supported with stock_type='preferred' "
            "(preferred shares are almost never primary — scan would return 0)"
        )
    country = (country or "america").strip().lower()
    limit = max(1, min(int(limit), MAX_SCREEN_LIMIT))
    if max_scan_rows is None:
        scan_cap = _DEFAULT_MAX_SCAN_ROWS
    else:
        try:
            scan_cap = int(max_scan_rows)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"max_scan_rows must be an int, got {max_scan_rows!r}"
            ) from exc
        if scan_cap < 1:
            raise ValueError("max_scan_rows must be >= 1")
        scan_cap = min(scan_cap, _MAX_SCAN_ROWS_CAP)
    sort_key = (sort_by or "market_cap").strip().lower()
    computed_sort_col = _COMPUTED_SORT_FIELDS.get(sort_key)
    sort_col = SORT_FIELDS.get(sort_key)
    if sort_col is None and computed_sort_col is None:
        allowed = list(SORT_FIELDS) + list(_COMPUTED_SORT_FIELDS)
        raise ValueError(
            f"sort_by must be one of {allowed}, got {sort_by!r}"
        )

    sectors = _normalize_sector_exclude(sector_exclude)
    legacy_filters = _any_legacy_fundamental_filter(
        market_cap_min,
        market_cap_max,
        revenue_growth_yoy_min,
        fcf_positive,
        fcf_min,
        net_debt_to_ebitda_max,
        sectors,
        analyst_count_max,
    )
    new_filters = _any_new_fundamental_filter(
        gross_margin_min,
        revenue_growth_yoy_fq_min,
        rule_of_40_min,
        primary_only,
        market_cap_usd_min,
        market_cap_usd_max,
        gross_margin_fq_min,
    )
    # Extended payload iff any NEW filter or NEW sort key (incl. rule_of_40).
    extended_payload = new_filters or sort_key in _NEW_SORT_KEYS
    # Legacy audit payload: pre-101 fund filters/sorts, and no new features.
    legacy_payload = (
        not extended_payload
        and (
            legacy_filters
            or sort_key in _LEGACY_FUND_SORT_KEYS
        )
    )
    use_fundamentals = extended_payload or legacy_payload

    needs_post = _needs_post_filter(
        rule_of_40_min,
        market_cap_usd_min,
        market_cap_usd_max,
        computed_sort=computed_sort_col is not None,
        gross_margin_fq_min=gross_margin_fq_min,
    )

    if market_cap_min is not None and market_cap_max is not None:
        if float(market_cap_min) > float(market_cap_max):
            raise ValueError("market_cap_min must be <= market_cap_max")
    if market_cap_usd_min is not None and market_cap_usd_max is not None:
        if float(market_cap_usd_min) > float(market_cap_usd_max):
            raise ValueError("market_cap_usd_min must be <= market_cap_usd_max")
    if fcf_positive is not None and fcf_min is not None and bool(fcf_positive) and float(fcf_min) <= 0:
        # fcf_min is the stricter numeric bound when both are set.
        pass

    filters = [col("type") == "stock", col("typespecs").has([stock_type])]
    if exclude_otc:
        filters.append(col("exchange") != "OTC")

    # On USD-normalized markets market_cap_usd ≈ market_cap_basic, so push
    # USD caps into the TV where (otherwise over-fetch of top raw caps can
    # miss the entire mid-cap USD band). Non-USD markets keep post-filter only.
    tv_cap_min = market_cap_min
    tv_cap_max = market_cap_max
    if country in _USD_NORMALIZED_MARKETS:
        if market_cap_usd_min is not None:
            tv_cap_min = (
                max(float(tv_cap_min), float(market_cap_usd_min))
                if tv_cap_min is not None
                else float(market_cap_usd_min)
            )
        if market_cap_usd_max is not None:
            tv_cap_max = (
                min(float(tv_cap_max), float(market_cap_usd_max))
                if tv_cap_max is not None
                else float(market_cap_usd_max)
            )
        if (
            tv_cap_min is not None
            and tv_cap_max is not None
            and float(tv_cap_min) > float(tv_cap_max)
        ):
            raise ValueError(
                "market_cap_* / market_cap_usd_* intersection is empty "
                f"(effective min={tv_cap_min} > max={tv_cap_max})"
            )

    if tv_cap_min is not None and tv_cap_max is not None:
        filters.append(
            col("market_cap_basic").between(float(tv_cap_min), float(tv_cap_max))
        )
    elif tv_cap_min is not None:
        filters.append(col("market_cap_basic") >= float(tv_cap_min))
    elif tv_cap_max is not None:
        filters.append(col("market_cap_basic") <= float(tv_cap_max))

    if revenue_growth_yoy_min is not None:
        filters.append(
            col("total_revenue_yoy_growth_ttm") >= float(revenue_growth_yoy_min)
        )

    if revenue_growth_yoy_fq_min is not None:
        filters.append(
            col("total_revenue_yoy_growth_fq") >= float(revenue_growth_yoy_fq_min)
        )

    if gross_margin_min is not None:
        filters.append(col("gross_margin_ttm") >= float(gross_margin_min))

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

    if primary_only:
        filters.append(col("is_primary") == True)  # noqa: E712 — TV col API

    select_cols = list(_SCREEN_COLUMNS)
    if extended_payload:
        for c in _FUNDAMENTAL_COLUMNS:
            if c not in select_cols:
                select_cols.append(c)
    elif legacy_payload:
        for c in _LEGACY_FUNDAMENTAL_COLUMNS:
            if c not in select_cols:
                select_cols.append(c)
    if sort_col is not None and sort_col not in select_cols:
        select_cols.append(sort_col)

    # TV order_by: post-filter / computed-sort paths always page the where-set
    # by market_cap_basic (docs/warning assume cap-ordered truncation). Native
    # single-request sorts use the requested TV column.
    if needs_post:
        tv_order_col = "market_cap_basic"
    else:
        tv_order_col = sort_col if sort_col is not None else "market_cap_basic"
    if tv_order_col not in select_cols:
        select_cols.append(tv_order_col)

    page_size = limit if not needs_post else _POST_FILTER_PAGE_SIZE

    applied_filters: dict[str, Any] = {}
    if market_cap_min is not None:
        applied_filters["market_cap_min"] = float(market_cap_min)
    if market_cap_max is not None:
        applied_filters["market_cap_max"] = float(market_cap_max)
    if revenue_growth_yoy_min is not None:
        applied_filters["revenue_growth_yoy_min"] = float(revenue_growth_yoy_min)
    if revenue_growth_yoy_fq_min is not None:
        applied_filters["revenue_growth_yoy_fq_min"] = float(
            revenue_growth_yoy_fq_min
        )
    if gross_margin_min is not None:
        applied_filters["gross_margin_min"] = float(gross_margin_min)
    if gross_margin_fq_min is not None:
        applied_filters["gross_margin_fq_min"] = float(gross_margin_fq_min)
    if rule_of_40_min is not None:
        applied_filters["rule_of_40_min"] = float(rule_of_40_min)
    if market_cap_usd_min is not None:
        applied_filters["market_cap_usd_min"] = float(market_cap_usd_min)
    if market_cap_usd_max is not None:
        applied_filters["market_cap_usd_max"] = float(market_cap_usd_max)
    if primary_only:
        applied_filters["primary_only"] = True
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

    def _row_from_raw(
        r: dict[str, Any],
        fx_rates: dict[str, Optional[float]],
        fx_notes: list[str],
    ) -> dict[str, Any]:
        market_cap = _clean(r.get("market_cap_basic"))
        currency = _clean(r.get("currency"))
        row: dict[str, Any] = {
            "ticker": _clean(r.get("ticker")),
            "symbol": _clean(r.get("name")),
            "description": _clean(r.get("description")),
            "exchange": _clean(r.get("exchange")),
            "price": _clean(r.get("close")),
            "open": _clean(r.get("open")),
            "high": _clean(r.get("high")),
            "low": _clean(r.get("low")),
            "currency": currency,
            "change_percent": _clean(r.get("change")),
            "dividend_yield": _clean(r.get("dividends_yield_current")),
            "market_cap": market_cap,
        }
        if extended_payload:
            rev_ttm = _clean(r.get("total_revenue_yoy_growth_ttm"))
            rev_fq = _clean(r.get("total_revenue_yoy_growth_fq"))
            revenue_ttm = _clean(r.get("total_revenue_ttm"))
            fcf = _clean(r.get("free_cash_flow_ttm"))
            fcf_margin = _safe_div_pct(fcf, revenue_ttm)
            row["revenue_growth_yoy_ttm"] = rev_ttm
            row["revenue_growth_yoy_fq"] = rev_fq
            row["total_revenue_ttm"] = revenue_ttm
            row["gross_margin_ttm"] = _clean(r.get("gross_margin_ttm"))
            row["gross_margin_fq"] = _resolve_gross_margin_fq(r)
            row["free_cash_flow_ttm"] = fcf
            row["price_free_cash_flow_ttm"] = _clean(
                r.get("price_free_cash_flow_ttm")
            )
            row["fcf_margin_ttm"] = fcf_margin
            row["rule_of_40_ttm"] = _safe_sum(rev_ttm, fcf_margin)
            row["rule_of_40_fq"] = _safe_sum(rev_fq, fcf_margin)
            row["net_debt_to_ebitda_fq"] = _clean(r.get("net_debt_to_ebitda_fq"))
            row["sector"] = _clean(r.get("sector"))
            row["industry"] = _clean(r.get("industry"))
            row["is_primary"] = _clean(r.get("is_primary"))
            row["analyst_count"] = _clean(r.get("recommendation_total"))
            mcap_usd, note = _compute_market_cap_usd(
                market_cap, currency, country, fx_rates
            )
            row["market_cap_usd"] = mcap_usd
            if note and note not in fx_notes:
                fx_notes.append(note)
        elif legacy_payload:
            # Pre-101 audit keys only — no new response fields.
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
        return row

    def _passes_post(row: dict[str, Any]) -> bool:
        if rule_of_40_min is not None:
            ro40 = row.get("rule_of_40_fq")
            if ro40 is None or float(ro40) < float(rule_of_40_min):
                return False
        if market_cap_usd_min is not None:
            mu = row.get("market_cap_usd")
            if mu is None or float(mu) < float(market_cap_usd_min):
                return False
        if market_cap_usd_max is not None:
            mu = row.get("market_cap_usd")
            if mu is None or float(mu) > float(market_cap_usd_max):
                return False
        if gross_margin_fq_min is not None:
            gm_fq = row.get("gross_margin_fq")
            if gm_fq is None or float(gm_fq) < float(gross_margin_fq_min):
                return False
        return True

    fx_notes: list[str] = []
    fx_rates: dict[str, Optional[float]] = {"USD": 1.0}
    rows: list[dict[str, Any]] = []
    total = 0
    scanned = 0
    post_filter_incomplete = False
    warning: Optional[str] = None
    passer_count: Optional[int] = None

    offset = 0
    seen: set[str] = set()
    passers: list[dict[str, Any]] = []
    page_idx = 0

    while True:
        # TV range is [start, end); never pass limit=page_size with nonzero
        # offset (that yields [N, N] → data:null / TypeError in the lib).
        remaining = scan_cap - scanned if needs_post else page_size
        if needs_post and remaining <= 0:
            break
        this_page = min(page_size, remaining) if needs_post else page_size

        total, raw_records = _fetch_stock_scanner_page(
            country,
            select_cols,
            filters,
            tv_order_col,
            offset,
            this_page,
        )
        if not raw_records:
            break

        if extended_payload and page_idx == 0:
            currencies = [_clean(r.get("currency")) for r in raw_records]
            need_fx = country not in _USD_NORMALIZED_MARKETS and any(
                c and str(c).upper() != "USD" for c in currencies
            )
            if need_fx:
                fx_rates = _fetch_fx_to_usd(
                    [str(c) for c in currencies if c is not None]
                )
        elif extended_payload and country not in _USD_NORMALIZED_MARKETS:
            page_ccys = [
                str(c)
                for c in (_clean(r.get("currency")) for r in raw_records)
                if c and str(c).upper() != "USD" and str(c).upper() not in fx_rates
            ]
            if page_ccys:
                more = _fetch_fx_to_usd(page_ccys)
                fx_rates.update(more)

        page_rows = [_row_from_raw(r, fx_rates, fx_notes) for r in raw_records]
        scanned += len(page_rows)
        offset += len(raw_records)
        page_idx += 1

        if needs_post:
            for row in page_rows:
                ticker = str(row.get("ticker") or "")
                if ticker and ticker in seen:
                    continue
                if ticker:
                    seen.add(ticker)
                if _passes_post(row):
                    passers.append(row)
            # Exhaust the where-set (or hit scan_cap) — do NOT stop early
            # when passers >= limit (would silently truncate the ranking).
            if scanned >= int(total) or scanned >= scan_cap:
                break
        else:
            rows = page_rows
            break

    if needs_post:
        post_filter_incomplete = _post_filter_incomplete(scanned, total)
        if post_filter_incomplete:
            warning = (
                f"a rangsor csak az első {scanned} (market_cap szerint) "
                f"sorból készült — szűkíts a TV-oldali szűrőkkel "
                f"(tv_total_matches={total}, max_scan_rows={scan_cap})"
            )
        # Re-sort after post-filter (especially for computed sort_by).
        sort_row_key = computed_sort_col
        if sort_row_key is None and sort_key == "gross_margin":
            sort_row_key = "gross_margin_ttm"
        elif sort_row_key is None and sort_key == "gross_margin_fq":
            sort_row_key = "gross_margin_fq"
        elif sort_row_key is None and sort_key == "revenue_growth_fq":
            sort_row_key = "revenue_growth_yoy_fq"
        elif sort_row_key is None and sort_key == "revenue_growth":
            sort_row_key = "revenue_growth_yoy_ttm"
        elif sort_row_key is None and sort_key == "fcf":
            sort_row_key = "free_cash_flow_ttm"
        elif sort_row_key is None and sort_key == "net_debt_ebitda":
            sort_row_key = "net_debt_to_ebitda_fq"
        elif sort_row_key is None and sort_key == "market_cap":
            sort_row_key = "market_cap"
        elif sort_row_key is None and sort_key == "dividend_yield":
            sort_row_key = "dividend_yield"
        elif sort_row_key is None and sort_key == "change":
            sort_row_key = "change_percent"
        elif sort_row_key is None and sort_key == "price":
            sort_row_key = "price"

        if sort_row_key:

            def _sort_val(item: dict[str, Any]) -> float:
                v = item.get(sort_row_key)  # type: ignore[arg-type]
                if v is None:
                    return float("-inf")
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return float("-inf")

            passers.sort(key=_sort_val, reverse=True)

        passer_count = len(passers)
        rows = passers[:limit]
    # When no post-filter, TV already limited to `limit`.

    if compact:
        keep = ["ticker", "symbol", "price", "currency", "change_percent"]
        if extended_payload:
            keep.extend(list(_EXTENDED_FUND_AUDIT_KEYS))
        elif legacy_payload:
            keep.extend(list(_LEGACY_FUND_AUDIT_KEYS))
        rows = [{k: r[k] for k in keep if k in r} for r in rows]

    result: dict[str, Any] = {
        "country": country,
        "stock_type": stock_type,
        "exclude_otc": exclude_otc,
        "sort_by": sort_key,
        "filters_applied": applied_filters,
        "total_matches": passer_count if needs_post else total,
        "returned": len(rows),
        "rows": rows,
    }
    # New envelope keys only on extended / post-filter paths (legacy BC).
    if extended_payload or needs_post:
        result["tv_total_matches"] = total
    if needs_post:
        # fetch_limit kept as alias of max_scan_rows for older clients/tests.
        result["fetch_limit"] = scan_cap
        result["max_scan_rows"] = scan_cap
        result["scanned_rows"] = scanned
        result["post_filter_incomplete"] = post_filter_incomplete
        if warning:
            result["warning"] = warning
    if extended_payload:
        limitations = list(API_LIMITATIONS)
        limitations.extend(fx_notes)
        result["api_limitations"] = limitations
    elif legacy_payload:
        result["api_limitations"] = list(_LEGACY_API_LIMITATIONS)
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
