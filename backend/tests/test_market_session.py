"""`in_market_session` — TAIFEX full-session gate used by the feed watchdog.

Distinct from `in_entry_window` (the narrower entry gate): this covers the
whole day + night session including the overnight wrap and weekday rules.

June 2026 anchors: Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6, Sun=7.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.strategies.base import in_market_session

TPE = ZoneInfo("Asia/Taipei")

_KW = dict(
    day_open=time(8, 45),
    day_close=time(13, 45),
    night_open=time(15, 0),
    night_close=time(5, 0),
)


def _in(dt: datetime) -> bool:
    return in_market_session(dt, TPE, **_KW)


@pytest.mark.parametrize(
    "dt,expected",
    [
        # --- weekday day session (Mon Jun 1) ---
        (datetime(2026, 6, 1, 8, 44, tzinfo=TPE), False),   # before open
        (datetime(2026, 6, 1, 8, 45, tzinfo=TPE), True),    # at open
        (datetime(2026, 6, 1, 10, 0, tzinfo=TPE), True),    # mid day
        (datetime(2026, 6, 1, 13, 45, tzinfo=TPE), False),  # close exclusive
        # --- midday closed gap (Mon) 13:45–15:00 ---
        (datetime(2026, 6, 1, 14, 0, tzinfo=TPE), False),
        # --- weekday night session evening leg (Mon) ---
        (datetime(2026, 6, 1, 15, 0, tzinfo=TPE), True),
        (datetime(2026, 6, 1, 22, 0, tzinfo=TPE), True),
        (datetime(2026, 6, 1, 23, 59, tzinfo=TPE), True),
        # --- overnight tail into Tue (Mon night) ---
        (datetime(2026, 6, 2, 3, 0, tzinfo=TPE), True),
        (datetime(2026, 6, 2, 5, 0, tzinfo=TPE), False),    # close exclusive
        (datetime(2026, 6, 2, 8, 0, tzinfo=TPE), False),    # morning gap
        # --- Monday early morning: no Sunday-night session ---
        (datetime(2026, 6, 1, 3, 0, tzinfo=TPE), False),
        # --- Friday night → Saturday tail ---
        (datetime(2026, 6, 5, 22, 0, tzinfo=TPE), True),    # Fri night
        (datetime(2026, 6, 6, 3, 0, tzinfo=TPE), True),     # Sat tail (Fri night)
        (datetime(2026, 6, 6, 10, 0, tzinfo=TPE), False),   # Sat day: closed
        (datetime(2026, 6, 6, 16, 0, tzinfo=TPE), False),   # Sat night: none
        # --- Sunday fully closed ---
        (datetime(2026, 6, 7, 10, 0, tzinfo=TPE), False),
        (datetime(2026, 6, 7, 22, 0, tzinfo=TPE), False),
    ],
)
def test_in_market_session(dt, expected):
    assert _in(dt) is expected
