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
