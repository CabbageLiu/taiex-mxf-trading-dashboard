Points below are the things to be changed, all specified must tally to the actual changes

1. Force position closure
If there are positions still opened when trading time (or allowed trading time) is ending, force close the position

2. Downward / Upward trend indication
Based on 15 min interval, use EMA20 and EMA50 to determine trend direction
- EMA20 > EMA50 = upward trend
- EMA20 < EMA50 = downard trend
Then use ADX?DMI to confirm whether trend is strong enough
- Uptrend: +DI > -DI and ADX > 20 or 25
- Downtrend: -DI > +DI and ADX > 20 or 25
- Sideways: ADX below 20 or 25
Determine 15-minute trend using EMA direction plus ADX/DMI confidence.

1. Calculate:
- EMA20
- EMA50
- ADX14
- +DI14
- -DI14

2. Determine direction:
- Bullish direction if EMA20 > EMA50 and +DI > -DI
- Bearish direction if EMA20 < EMA50 and -DI > +DI
- Otherwise classify as sideways / unclear

3. Determine confidence using ADX:
- ADX < 20 = low confidence / sideways
- ADX 20–25 = weak trend
- ADX 25–40 = strong trend
- ADX > 40 = very strong trend, but may be extended

4. Optional trend score:
Direction = +1 for bullish, -1 for bearish, 0 for unclear

Trend Score = Direction × MIN(ADX / 50, 1)

Interpretation:
- +0.70 to +1.00 = strong uptrend
- +0.30 to +0.70 = moderate uptrend
- -0.30 to -0.70 = moderate downtrend
- -0.70 to -1.00 = strong downtrend
- -0.30 to +0.30 = weak / sideways

In short:
EMA20/EMA50 defines trend direction.
DMI confirms whether bullish or bearish pressure is stronger.
ADX acts as the confidence score for how meaningful the trend is.

The trend and trend score needs to be added to the discord message broadcast open/close position in such format:
趨勢：{trend} 趨勢分數：{trend score}
*note that the trend should be in chinese, like everything else

The trend and trend score should be also indicated in the trading and analysis page:
-trading page: along side in the position infomation open/close information
-analysis page: new field of trend and trend score to be added (past trades no need backfill, leave as blank)

*Note this trend and score should be a standalone feature, it shall not affect by any means of the position opening and closing