from app.adapters.finmind_taiex import FinMindTaiexAdapter


def test_dedupe_yields_only_new_rows():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [
        {"date": "2026-04-28 09:00:00", "close": 40000.0},
        {"date": "2026-04-28 09:00:05", "close": 40001.0},
    ]
    first = a._dedupe(rows)
    assert [t.price for t in first] == [40000.0, 40001.0]

    # Same data + one more row → only the new one returned
    rows2 = rows + [{"date": "2026-04-28 09:00:10", "close": 40002.0}]
    second = a._dedupe(rows2)
    assert [t.price for t in second] == [40002.0]

    # No new rows → empty
    third = a._dedupe(rows2)
    assert third == []


def test_rows_to_ticks_skips_invalid():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [
        {"date": "2026-04-28 09:00:00", "close": "abc"},  # bad price
        {"date": None, "close": 1.0},                      # bad ts
        {"date": "2026-04-28 09:00:05", "close": 40001.0},
    ]
    ticks = a._rows_to_ticks(rows)
    assert len(ticks) == 1
    assert ticks[0].symbol == "MXF"
    assert ticks[0].source == "FINMIND_FUTURES_SNAPSHOT"


def test_rows_to_ticks_falls_back_to_price_field():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [{"date": "2026-04-28 09:00:00", "price": 40000.0}]
    ticks = a._rows_to_ticks(rows)
    assert len(ticks) == 1
    assert ticks[0].price == 40000.0


def test_rows_to_ticks_skips_zero_close():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [{"date": "2026-04-29 09:00:00", "close": 0}]
    ticks = a._rows_to_ticks(rows)
    assert ticks == []


def test_rows_to_ticks_skips_negative_price():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [{"date": "2026-04-29 09:00:00", "close": -1}]
    ticks = a._rows_to_ticks(rows)
    assert ticks == []


def test_rows_to_ticks_skips_zero_price_field():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [{"date": "2026-04-29 09:00:00", "close": None, "price": 0}]
    ticks = a._rows_to_ticks(rows)
    assert ticks == []


def test_rows_to_ticks_keeps_valid():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [{"date": "2026-04-29 09:00:00", "close": 40000.0}]
    ticks = a._rows_to_ticks(rows)
    assert len(ticks) == 1
    assert ticks[0].price == 40000.0
    assert ticks[0].symbol == "MXF"
    assert ticks[0].source == "FINMIND_FUTURES_SNAPSHOT"


# ---------------------------------------------------------------------------
# _pick_front_month — single-contract picker for multi-contract responses
# ---------------------------------------------------------------------------


def test_pick_front_month_prefers_R1_suffix():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [
        {"futures_id": "TXFE6", "total_volume": 6000, "close": 39418.0, "contract_date": "202605"},
        {"futures_id": "TXFR1", "total_volume": 6000, "close": 39418.0, "contract_date": None},
        {"futures_id": "TXFC7", "total_volume": 4, "close": 40550.0, "contract_date": "202703"},
    ]
    picked = a._pick_front_month(rows)
    assert picked is not None
    assert picked["futures_id"] == "TXFR1"


def test_pick_front_month_fallback_highest_volume():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [
        {"futures_id": "TXFE6", "total_volume": 6000, "close": 39418.0, "contract_date": "202605"},
        {"futures_id": "TXFG6", "total_volume": 12, "close": 39470.0, "contract_date": "202607"},
        {"futures_id": "TXFC7", "total_volume": 4, "close": 40550.0, "contract_date": "202703"},
    ]
    picked = a._pick_front_month(rows)
    assert picked is not None
    assert picked["futures_id"] == "TXFE6"


def test_pick_front_month_empty_returns_none():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._pick_front_month([]) is None


def test_pick_front_month_resolves_volume_tie_by_contract_date():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    rows = [
        {"futures_id": "TXFG6", "total_volume": 100, "close": 39600.0, "contract_date": "202607"},
        {"futures_id": "TXFE6", "total_volume": 100, "close": 39418.0, "contract_date": "202605"},
    ]
    picked = a._pick_front_month(rows)
    assert picked is not None
    # Smaller contract_date (202605 < 202607) wins on tie
    assert picked["futures_id"] == "TXFE6"


# ---------------------------------------------------------------------------
# _market_open — day session + TAIFEX after-hours session
# ---------------------------------------------------------------------------


def _at(weekday: int, hour: int, minute: int = 0) -> "datetime":
    """Build a tz-aware Asia/Taipei datetime on a known reference week.

    2026-04-27 is a Monday (weekday=0). Add `weekday` days to land on
    the requested day-of-week, then set the time.
    """
    from datetime import datetime as _dt

    from zoneinfo import ZoneInfo as _ZI

    base = _dt(2026, 4, 27, hour, minute, tzinfo=_ZI("Asia/Taipei"))
    from datetime import timedelta as _td

    return base + _td(days=weekday)


def test_market_open_day_session_open():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(0, 9, 0)) is True  # Mon 09:00


def test_market_open_includes_night_session_evening():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(1, 21, 0)) is True  # Tue 21:00


def test_market_open_includes_night_session_overnight():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(2, 2, 0)) is True  # Wed 02:00 (continuation of Tue night)


def test_market_open_excludes_gap_between_sessions():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(1, 14, 0)) is False  # Tue 14:00 (between 13:45 close and 15:00 night-open)


def test_market_open_excludes_saturday_after_5am():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(5, 17, 0)) is False  # Sat 17:00


def test_market_open_includes_friday_to_saturday_morning():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(4, 22, 0)) is True  # Fri 22:00
    assert a._market_open(_at(5, 4, 0)) is True   # Sat 04:00 (continuing Fri night)


def test_market_open_excludes_sunday():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    assert a._market_open(_at(6, 4, 0)) is False  # Sun 04:00 — Sat off so no continuation
    assert a._market_open(_at(6, 22, 0)) is False  # Sun 22:00


def test_market_open_excludes_monday_before_open():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    # Mon 04:00 — Sunday is closed, so no night-continuation. Day session not yet open.
    assert a._market_open(_at(0, 4, 0)) is False


def test_next_open_returns_future_moment():
    a = FinMindTaiexAdapter(display_symbol="MXF")
    closed = _at(1, 14, 0)  # Tue 14:00 (between sessions)
    nxt = a._next_open(closed)
    # Should resolve to today's 15:00 night session start
    assert nxt > closed
    assert a._market_open(nxt) is True
