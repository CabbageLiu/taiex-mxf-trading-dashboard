📋 TAIEX Multi-Timeframe Strategy v1 — Full Summary
🎯 Instrument
Mini TAIEX Futures (MXF1!, continuous contract)
⏱ Timeframes

Signal layer: 30-minute K
Exit assist layer: 3-minute K
Trend reference layer: Daily (display only — does not affect entry)

📈 Direction

Default: Long only
Optional: Short can be enabled (mirrored logic)


🟢 Entry Conditions (30-minute K, all three must be true)
To go long, all three must hold simultaneously:

KD > 20 — Both K and D values above 20 (out of oversold zone)
MACD line > 0 — MACD main line above zero axis (bullish momentum)
+DI > 21 — Positive directional indicator above 21 (buyers in control)


Daily confidence: same three conditions checked on the daily timeframe → if 2/3 or 3/3 met, a "Daily Confidence" badge displays. Reference only, does not block entry.

Short conditions are mirrored (KD < 80, MACD < 0, −DI > 21).

🛑 Exit Conditions (any one triggers exit)

Take profit +220 points — profit target hit
Stop loss −60 points — caps single-trade loss
3-minute −DI > 23 — short-term momentum has flipped, exit early to avoid full SL hit

R:R = 220 : 60 = 3.67 : 1 (breakeven win rate ~21.4%)

⚙️ Discipline Mechanics

No pyramiding: 1 contract per position
Cooldown: 5 K-bars must pass after exit before re-entry (avoids getting whipsawed by the same noise)
Freshness filter: only enters on the bar where conditions just became true (not every bar where conditions persist)
Order processing: orders fill at next bar's open (not on close)


🎨 Visual Aids on Chart

Entry labels showing entry price + the actual KD/MACD/+DI values that triggered it
Exit labels (teal for win, maroon for loss) showing exit price, P&L points, and exit reason (TP / SL / DI flip)
Dashed line connecting each entry to its exit
Triangle markers on every signal candle
Optional condition-met background (toggleable for debugging)
Position-held tint (light green/red across bars while in a trade)
Top-right status box: current position, daily confidence score