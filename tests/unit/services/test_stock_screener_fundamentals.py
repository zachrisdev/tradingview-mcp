"""Unit tests for TRADINGVIEW_SCREENER_FUNDAMENTALS helpers (no live TV)."""

from tradingview_mcp.core.services.stock_screener_service import (
    _LEGACY_FUND_SORT_KEYS,
    _NEW_RESPONSE_KEYS,
    _NEW_SORT_KEYS,
    _any_legacy_fundamental_filter,
    _any_new_fundamental_filter,
    _compute_market_cap_usd,
    _fx_ccy_from_scanner_row,
    _post_filter_incomplete,
    _resolve_gross_margin_fq,
    _safe_div_pct,
    _safe_sum,
    _tv_range_bounds,
)


def test_payload_gating_helpers():
    assert _any_legacy_fundamental_filter(
        1e8, 5e9, 15, True, None, 2, ["Finance"], None
    )
    assert not _any_new_fundamental_filter(None, None, None, False, None, None)
    assert _any_new_fundamental_filter(40, None, None, False, None, None)
    assert _any_new_fundamental_filter(None, None, None, True, None, None)
    assert _any_new_fundamental_filter(
        None, None, None, False, None, None, gross_margin_fq_min=50
    )
    assert "revenue_growth" in _LEGACY_FUND_SORT_KEYS
    assert "gross_margin" in _NEW_SORT_KEYS
    assert "gross_margin_fq" in _NEW_SORT_KEYS
    assert "rule_of_40" in _NEW_SORT_KEYS
    assert "gross_margin_ttm" in _NEW_RESPONSE_KEYS
    assert "gross_margin_fq" in _NEW_RESPONSE_KEYS
    assert "tv_total_matches" in _NEW_RESPONSE_KEYS


def test_post_filter_incomplete_true_when_unscanned_remain():
    assert _post_filter_incomplete(50, 51) is True
    assert _post_filter_incomplete(2000, 5000) is True


def test_post_filter_incomplete_false_when_fully_scanned():
    assert _post_filter_incomplete(50, 50) is False
    assert _post_filter_incomplete(50, 10) is False
    assert _post_filter_incomplete(2000, 2000) is False


def test_compute_market_cap_usd_normalized_markets_ignore_currency():
    usd, note = _compute_market_cap_usd(1e9, "EUR", "germany", {})
    assert usd == 1e9
    assert note is None


def test_compute_market_cap_usd_missing_currency_non_norm():
    usd, note = _compute_market_cap_usd(1e12, None, "sweden", {"SEK": 0.1})
    assert usd is None
    assert note is not None and "currency missing" in note

    usd2, note2 = _compute_market_cap_usd(1e12, "", "sweden", {"SEK": 0.1})
    assert usd2 is None
    assert note2 is not None


def test_compute_market_cap_usd_sek_fx():
    usd, note = _compute_market_cap_usd(1e12, "SEK", "sweden", {"SEK": 0.1})
    assert usd == 1e11
    assert note is None


def test_compute_market_cap_usd_explicit_usd_on_foreign_market():
    usd, note = _compute_market_cap_usd(5e9, "USD", "sweden", {})
    assert usd == 5e9
    assert note is None


def test_compute_market_cap_usd_fx_missing():
    usd, note = _compute_market_cap_usd(1e12, "SEK", "sweden", {"SEK": None})
    assert usd is None
    assert note is not None and "unavailable" in note


def test_fx_ccy_from_scanner_row_variants():
    assert _fx_ccy_from_scanner_row({"ticker": "FX_IDC:SEKUSD", "name": "SEKUSD"}) == "SEK"
    assert _fx_ccy_from_scanner_row({"ticker": "FX_IDC:EURUSD", "name": "x"}) == "EUR"
    assert _fx_ccy_from_scanner_row({"ticker": "", "name": "CADUSD"}) == "CAD"
    assert _fx_ccy_from_scanner_row({"ticker": "NASDAQ:AAPL", "name": "AAPL"}) is None


def test_safe_math_helpers():
    assert _safe_div_pct(50, 200) == 25.0
    assert _safe_div_pct(1, 0) is None
    assert _safe_div_pct(None, 10) is None
    assert _safe_sum(12.0, 8.3) == 20.3
    assert _safe_sum(12.0, None) is None


def test_resolve_gross_margin_fq_prefers_direct_then_ratio():
    assert _resolve_gross_margin_fq({"gross_margin_fq": 61.5}) == 61.5
    assert _resolve_gross_margin_fq(
        {"gross_margin_fq": None, "gross_profit_fq": 15557194, "total_revenue_fq": 28252071}
    ) == _safe_div_pct(15557194, 28252071)
    assert _resolve_gross_margin_fq({"gross_profit_fq": 10, "total_revenue_fq": None}) is None


def test_tv_range_bounds_is_start_end_not_offset_count():
    # Regression: .offset(15).limit(15) → range [15,15] → data:null crash.
    assert _tv_range_bounds(0, 15) == (0, 15)
    assert _tv_range_bounds(15, 15) == (15, 30)
    assert _tv_range_bounds(30, 15) == (30, 45)
    assert _tv_range_bounds(0, 0) == (0, 0)
