# FinMind - Complete RESTful API & Dataset Reference

> FinMind is an open-source financial data platform providing 75+ Taiwan market datasets and international market data (US, UK, Europe, Japan) via RESTful API. Data is updated daily.

## RESTful API

Base URL: `https://api.finmindtrade.com/api/v4`

### Authentication

Register at finmindtrade.com, get token via login API or website. Pass token as `Authorization: Bearer {token}` header.

Rate limit: 600 requests/hour (with token), 300/hour (without token).

### Endpoints

#### POST /login
Get auth token.
- Params: user_id (str), password (str)
- Response: `{ "token": "..." }`

#### GET /data
Fetch dataset.
- Params: dataset (str, required), data_id (str), start_date (str, YYYY-MM-DD), end_date (str, YYYY-MM-DD)
- Response: `{ "msg": "success", "status": 200, "data": [ { ... }, ... ] }`

#### GET /datalist
List available data_id values for a dataset.
- Params: dataset (str, required)

#### GET /translation
Column name Chinese-English mapping.
- Params: dataset (str, required)

### API Usage Count (API 使用次數)

Check current API usage and quota.

#### GET https://api.web.finmindtrade.com/v2/user_info
- Headers: Authorization: Bearer {token}
- Response fields: `user_count` (int, 使用次數), `api_request_limit` (int, api 使用上限)

```python
# Package
from FinMind.data import FinMindApi
api = FinMindApi()
api.login_by_token(token)
print(api.api_usage_limit)

# Requests
import requests
headers = {"Authorization": f"Bearer {token}"}
url = "https://api.web.finmindtrade.com/v2/user_info"
resp = requests.get(url, headers=headers)
resp.json()["user_count"]  # 使用次數
resp.json()["api_request_limit"]  # api 使用上限
```

#### Rate Limit Exceeded (API 用量超出上限)
When quota is exceeded, API returns HTTP 402:
```
{'msg': 'Requests reach the upper limit. https://finmindtrade.com/', 'status': 402}
```

---

### Example

```
GET https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id=2330&start_date=2020-04-01&end_date=2020-04-12
Authorization: Bearer {token}
```

```python
import requests
url = "https://api.finmindtrade.com/api/v4/data"
headers = {"Authorization": "Bearer your_token"}
params = {
    "dataset": "TaiwanStockPrice",
    "data_id": "2330",
    "start_date": "2020-04-01",
    "end_date": "2020-04-12",
}
resp = requests.get(url, headers=headers, params=params)
data = resp.json()
```

### Async Batch Query (Python SDK)

Use `stock_id_list` with `use_async=True` to query multiple stocks concurrently for faster performance.

```python
from FinMind.data import DataLoader
api = DataLoader()
api.login_by_token(api_token='your_token')
df = api.taiwan_stock_daily(
    stock_id_list=['2330', '2317', '2454', '3008'],
    start_date='2024-01-01',
    end_date='2024-12-31',
    use_async=True,
)
```

Async batch query is supported by datasets that require `data_id` parameter. Not supported by: info, snapshot, tick, total aggregation, and convertible bond datasets.

## Membership Tiers

- **Free**: Basic datasets, 600 req/hour
- **Backer**: More datasets (marked "backer" below), higher limits
- **Sponsor**: Full access to all datasets including real-time data, branch trading data, and minute-level data

---

## Taiwan Market - Technical (20 datasets)

### TaiwanStockInfo (台股總覽)
- Tier: Free
- Params: dataset=TaiwanStockInfo
- Columns: industry_category (str), stock_id (str), stock_name (str), type (str), date (str)

### TaiwanStockInfoWithWarrant (台股總覽含權證)
- Tier: Free
- Params: dataset=TaiwanStockInfoWithWarrant
- Columns: industry_category (str), stock_id (str), stock_name (str), type (str), date (str)

### TaiwanStockInfoWithWarrantSummary (台股權證標的對照表)
- Tier: Sponsor
- Params: dataset=TaiwanStockInfoWithWarrantSummary, data_id=2330, start_date=2020-04-06
- Columns: stock_id (str), date (str), close (float), target_stock_id (str), target_close (float), type (str), fulfillment_method (str), end_date (str), fulfillment_start_date (str), fulfillment_end_date (str), exercise_ratio (float), fulfillment_price (float)

### TaiwanStockTradingDate (台股交易日)
- Tier: Free
- Params: dataset=TaiwanStockTradingDate
- Columns: date (str)

### TaiwanStockPrice (股價日成交資訊)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 1994-10-01 ~ now
- Update: Mon-Fri 17:30
- Params: dataset=TaiwanStockPrice, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockPrice, start_date=2020-04-02 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), Trading_Volume (int64), Trading_money (int64), open (float64), max (float64), min (float64), close (float64), spread (float64), Trading_turnover (float32)

### TaiwanStockPriceAdj (台灣還原股價資料表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 1994-10-01 ~ now
- Update: Mon-Fri 17:30
- Params: dataset=TaiwanStockPriceAdj, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockPriceAdj, start_date=2020-04-06 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), Trading_Volume (int64), Trading_money (int64), open (float64), max (float64), min (float64), close (float64), spread (float64), Trading_turnover (float32)

### TaiwanStockPriceTick (台灣股價歷史逐筆資料表)
- Tier: Backer/Sponsor
- Data range: 2019-01-01 ~ now
- Update: Mon-Fri 15:30
- Params: dataset=TaiwanStockPriceTick, data_id=2330, start_date=2020-01-02
- Columns: date (str), stock_id (str), deal_price (float64), volume (int64), Time (str), TickType (str: 0=unknown, 1=sell, 2=buy)
- Note: Single day per request.

### TaiwanStockPER (個股 PER、PBR 資料表)
- Tier: Free
- Data range: 2005-10-01 ~ now
- Update: Mon-Fri 18:00
- Params: dataset=TaiwanStockPER, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Columns: date (str), stock_id (str), dividend_yield (float64), PER (float64), PBR (float64)

### TaiwanStockStatisticsOfOrderBookAndTrade (每5秒委託成交統計)
- Tier: Free
- Data range: 2005-01-01 ~ now
- Params: dataset=TaiwanStockStatisticsOfOrderBookAndTrade, start_date=2021-01-07
- Columns: Time (str), TotalBuyOrder (str), TotalBuyVolume (int64), TotalSellOrder (int64), TotalSellVolume (int64), TotalDealOrder (int64), TotalDealVolume (int64), TotalDealMoney (int64), date (str)
- Note: Single day per request.

### TaiwanVariousIndicators5Seconds (台股加權指數)
- Tier: Free
- Data range: 2005-01-01 ~ now
- Params: dataset=TaiwanVariousIndicators5Seconds, start_date=2020-07-01
- Columns: date (str), TAIEX (float64)
- Note: Single day per request.

### TaiwanStockDayTrading (當日沖銷交易標的及成交量值)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2014-01-01 ~ now
- Update: Mon-Fri 21:30
- Params: dataset=TaiwanStockDayTrading, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockDayTrading, start_date=2020-04-06 (不帶 data_id，取得該日所有股票資料)
- Columns: stock_id (str), date (str), BuyAfterSale (str), Volume (int64), BuyAmount (int64), SellAmount (int64)

### TaiwanStockTotalReturnIndex (加權、櫃買報酬指數)
- Tier: Free
- Data range: 2003-01-01 ~ now
- Update: Mon-Fri 16:50
- Params: dataset=TaiwanStockTotalReturnIndex, data_id=TAIEX, start_date=2020-04-02, end_date=2020-04-12
- Columns: price (float64), stock_id (str), date (str)
- data_id values: TAIEX, TPEx

### TaiwanStock10Year (台灣個股十年線資料表)
- Tier: Backer/Sponsor
- Data range: 2011-01-24 ~ now
- Update: Mon-Fri 20:00
- Params: dataset=TaiwanStock10Year, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStock10Year, start_date=2020-04-06 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), close (float64)

### TaiwanStockKBar (台股分K資料表)
- Tier: Sponsor
- Data range: 2019-01-01 ~ now
- Update: Mon-Fri 15:50
- Params: dataset=TaiwanStockKBar, data_id=2330, start_date=2023-09-22
- Columns: date (str), minute (str), stock_id (str), open (float64), high (float64), low (float64), close (float64), volume (float32)
- Note: Single day per request.

### TaiwanStockWeekPrice (台股週K資料表)
- Tier: Backer/Sponsor
- Data range: 2000-01-01 ~ now
- Update: Mon-Fri 17:30
- Params: dataset=TaiwanStockWeekPrice, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockWeekPrice, start_date=2020-04-06 (不帶 data_id，取得該日所有股票資料)
- Columns: stock_id (str), yweek (str), max (float64), min (float64), trading_volume (int64), trading_money (int64), trading_turnover (float32), date (str), close (float64), open (float64), spread (float64)

### TaiwanStockMonthPrice (台股月K資料表)
- Tier: Backer/Sponsor
- Data range: 2000-01-01 ~ now
- Update: Mon-Fri 17:30
- Params: dataset=TaiwanStockMonthPrice, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMonthPrice, start_date=2020-04-06 (不帶 data_id，取得該日所有股票資料)
- Columns: stock_id (str), ymonth (str), max (float64), min (float64), trading_volume (int64), trading_money (int64), trading_turnover (float32), date (str), close (float64), open (float64), spread (float64)

### TaiwanStockEvery5SecondsIndex (每5秒指數統計)
- Tier: Backer/Sponsor
- Data range: 2005-01-03 ~ now
- Params: dataset=TaiwanStockEvery5SecondsIndex, start_date=2025-05-09
- Columns: date (str), time (str), stock_id (str), price (float), kind (str)
- Note: Single day per request.

### TaiwanStockSuspended (台股暫停交易公告)
- Tier: Backer/Sponsor
- Data range: 2011-10-06 ~ now
- Params: dataset=TaiwanStockSuspended, start_date=2017-04-01, end_date=2025-01-01
- Columns: stock_id (str), date (str), suspension_time (str), resumption_date (str), resumption_time (str)

### TaiwanStockDayTradingSuspension (暫停先賣後買當沖預告表)
- Tier: Backer/Sponsor
- Data range: 2014-06-01 ~ now
- Params: dataset=TaiwanStockDayTradingSuspension, start_date=2024-12-01, end_date=2025-01-01
- Columns: stock_id (str), date (str), end_date (str), reason (str)

### TaiwanStockPriceLimit (每日漲跌停價)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2000-01-01 ~ now
- Update: Mon-Fri 18:00
- Params: dataset=TaiwanStockPriceLimit, data_id=2330, start_date=2023-01-01
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockPriceLimit, start_date=2023-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), reference_price (float64), limit_up (float64), limit_down (float64)
- Note: limit_up/limit_down 為 0 表示無漲跌幅限制（槓桿型 ETF 如 00631L、反向型 ETF 如 00632R、興櫃股票）

---

## Taiwan Market - Chip / Institutional (18 datasets)

### TaiwanStockMarginPurchaseShortSale (個股融資融劵表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2001-01-01 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockMarginPurchaseShortSale, data_id=2330, start_date=2020-04-02, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMarginPurchaseShortSale, start_date=2020-04-02 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, MarginPurchaseBuy, MarginPurchaseCashRepayment, MarginPurchaseLimit, MarginPurchaseSell, MarginPurchaseTodayBalance, MarginPurchaseYesterdayBalance, Note, OffsetLoanAndShort, ShortSaleBuy, ShortSaleCashRepayment, ShortSaleLimit, ShortSaleSell, ShortSaleTodayBalance, ShortSaleYesterdayBalance

### TaiwanStockTotalMarginPurchaseShortSale (整體市場融資融劵表)
- Tier: Free
- Data range: 2001-01-01 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockTotalMarginPurchaseShortSale, start_date=2020-04-01, end_date=2020-04-12
- Columns: TodayBalance, YesBalance, buy, date, name, Return, sell

### TaiwanStockInstitutionalInvestorsBuySell (個股三大法人買賣表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2005-01-01 ~ now
- Update: Mon-Fri 20:00
- Params: dataset=TaiwanStockInstitutionalInvestorsBuySell, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockInstitutionalInvestorsBuySell, start_date=2020-04-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, buy, name, sell

### TaiwanStockTotalInstitutionalInvestors (整體三大市場法人買賣表)
- Tier: Free
- Data range: 2004-04-01 ~ now
- Update: Mon-Fri 15:00
- Params: dataset=TaiwanStockTotalInstitutionalInvestors, start_date=2020-04-01, end_date=2020-04-12
- Columns: buy, date, name, sell

### TaiwanStockShareholding (外資持股表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2004-02-01 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockShareholding, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockShareholding, start_date=2020-04-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, stock_name, InternationalCode, ForeignInvestmentRemainingShares, ForeignInvestmentShares, ForeignInvestmentRemainRatio, ForeignInvestmentSharesRatio, ForeignInvestmentUpperLimitRatio, ChineseInvestmentUpperLimitRatio, NumberOfSharesIssued, RecentlyDeclareDate, note

### TaiwanStockHoldingSharesPer (股權持股分級表)
- Tier: Backer/Sponsor
- Data range: 2010-01-29 ~ now
- Params: dataset=TaiwanStockHoldingSharesPer, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockHoldingSharesPer, start_date=2020-04-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, HoldingSharesLevel, people, percent, unit

### TaiwanStockSecuritiesLending (借券成交明細)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2001-05-01 ~ now
- Update: Mon-Fri 15:00
- Params: dataset=TaiwanStockSecuritiesLending, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockSecuritiesLending, start_date=2020-04-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, transaction_type, volume, fee_rate, close, original_return_date, original_lending_period

### TaiwanStockMarginShortSaleSuspension (暫停融券賣出表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2015-01-01 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockMarginShortSaleSuspension, data_id=0050, start_date=2015-01-01, end_date=2015-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMarginShortSaleSuspension, start_date=2015-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: stock_id, date, end_date, reason

### TaiwanDailyShortSaleBalances (信用額度總量管制餘額表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2005-07-01 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanDailyShortSaleBalances, data_id=2330, start_date=2020-04-01, end_date=2020-04-12
- Params (all stocks, Backer/Sponsor): dataset=TaiwanDailyShortSaleBalances, start_date=2020-04-01 (不帶 data_id，取得該日所有股票資料)
- Columns: stock_id, MarginShortSalesPreviousDayBalance, MarginShortSalesShortSales, MarginShortSalesShortCovering, MarginShortSalesStockRedemption, MarginShortSalesCurrentDayBalance, MarginShortSalesQuota, SBLShortSalesPreviousDayBalance, SBLShortSalesShortSales, SBLShortSalesReturns, SBLShortSalesAdjustments, SBLShortSalesCurrentDayBalance, SBLShortSalesQuota, SBLShortSalesShortCovering, date

### TaiwanSecuritiesTraderInfo (證券商資訊表)
- Tier: Free
- Params: dataset=TaiwanSecuritiesTraderInfo
- Columns: securities_trader_id, securities_trader, date, address, phone

### TaiwanStockTradingDailyReport (台股分點資料表)
- Tier: Sponsor
- Data range: 2021-06-30 ~ now
- Update: Mon-Fri 21:00
- Params (by stock): dataset=TaiwanStockTradingDailyReport, data_id=2330, start_date=2022-06-16
- Params (by broker): dataset=TaiwanStockTradingDailyReport, data_id=1102, start_date=2022-06-16
- Columns: securities_trader, price, buy, sell, securities_trader_id, stock_id, date
- Note: Single day per request.

### TaiwanStockWarrantTradingDailyReport (台股權證分點資料表)
- Tier: Sponsor
- Data range: 2023-06-21 ~ now
- Params (by stock): dataset=TaiwanStockWarrantTradingDailyReport, data_id=084655, start_date=2023-06-21
- Params (by broker): dataset=TaiwanStockWarrantTradingDailyReport, data_id=5920, start_date=2023-06-21
- Columns: securities_trader, price, buy, sell, securities_trader_id, stock_id, date

### TaiwanstockGovernmentBankBuySell (台股八大行庫買賣表)
- Tier: Sponsor
- Data range: 2021-06-30 ~ now
- Update: Mon-Fri 23:30
- Params: dataset=TaiwanstockGovernmentBankBuySell, start_date=2023-01-17
- Columns: date, stock_id, buy_amount, sell_amount, buy, sell, bank_name

### TaiwanTotalExchangeMarginMaintenance (台灣大盤融資維持率)
- Tier: Backer/Sponsor
- Data range: 2001-01-05 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanTotalExchangeMarginMaintenance, start_date=2024-04-01, end_date=2024-05-01
- Columns: date, TotalExchangeMarginMaintenance

### TaiwanStockTradingDailyReportSecIdAgg (當日卷商分點統計表)
- Tier: Sponsor
- Data range: 2021-06-30 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockTradingDailyReportSecIdAgg, data_id=2330, start_date=2024-07-01, end_date=2024-07-15
- Columns: securities_trader, securities_trader_id, stock_id, date, buy_volume, sell_volume, buy_price, sell_price

### TaiwanStockBlockTradingDailyReport (鉅額交易買賣日報表)
- Tier: Sponsor
- Data range: 2026-04-24 ~ now
- Update: Mon-Fri 21:00
- Params: dataset=TaiwanStockBlockTradingDailyReport, start_date=2026-04-24
- Columns: securities_trader, price, buy, sell, trade_type, securities_trader_id, stock_id, date

### TaiwanStockDispositionSecuritiesPeriod (公布處置有價證券表)
- Tier: Backer/Sponsor
- Data range: 2001-01-01 ~ now
- Params: dataset=TaiwanStockDispositionSecuritiesPeriod, data_id=6477, start_date=2025-01-01, end_date=2025-02-01
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockDispositionSecuritiesPeriod, start_date=2025-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date, stock_id, stock_name, disposition_cnt, condition, measure, period_start, period_end

---

## Taiwan Market - Fundamental (12 datasets)

### TaiwanStockFinancialStatements (綜合損益表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 1990-03-01 ~ now
- Params: dataset=TaiwanStockFinancialStatements, data_id=2330, start_date=2019-01-01
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockFinancialStatements, start_date=2019-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), type (str), value (float64), origin_name (str)

### TaiwanStockBalanceSheet (資產負債表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2011-12-01 ~ now
- Params: dataset=TaiwanStockBalanceSheet, data_id=2330, start_date=2019-03-31
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockBalanceSheet, start_date=2019-03-31 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), type (str), value (float64), origin_name (str)

### TaiwanStockCashFlowsStatement (現金流量表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2008-06-01 ~ now
- Params: dataset=TaiwanStockCashFlowsStatement, data_id=2330, start_date=2019-03-31
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockCashFlowsStatement, start_date=2019-03-31 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), type (str), value (float64), origin_name (str)

### TaiwanStockDividend (股利政策表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2005-05-01 ~ now
- Params: dataset=TaiwanStockDividend, data_id=2330, start_date=2019-03-31
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockDividend, start_date=2019-03-31 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), year (str), StockEarningsDistribution (float64), StockStatutorySurplus (float64), StockExDividendTradingDate (str), TotalEmployeeStockDividend (float64), TotalEmployeeStockDividendAmount (float64), RatioOfEmployeeStockDividendOfTotal (float64), RatioOfEmployeeStockDividend (float64), CashEarningsDistribution (float64), CashStatutorySurplus (float64), CashExDividendTradingDate (str), CashDividendPaymentDate (str), TotalEmployeeCashDividend (float64), TotalNumberOfCashCapitalIncrease (float64), CashIncreaseSubscriptionRate (float64), CashIncreaseSubscriptionpRrice (float64), RemunerationOfDirectorsAndSupervisors (float64), ParticipateDistributionOfTotalShares (float64), AnnouncementDate (str), AnnouncementTime (str)

### TaiwanStockDividendResult (除權除息結果表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2003-05-01 ~ now
- Params: dataset=TaiwanStockDividendResult, data_id=2330, start_date=2019-03-31
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockDividendResult, start_date=2019-03-31 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), before_price (float32), after_price (float32), stock_and_cache_dividend (float32), stock_or_cache_dividend (str), max_price (float32), min_price (float32), open_price (float32), reference_price (float32)

### TaiwanStockMonthRevenue (月營收表)
- Tier: Free (with data_id) / Backer/Sponsor (all stocks by start_date only)
- Data range: 2002-02-01 ~ now
- Params: dataset=TaiwanStockMonthRevenue, data_id=2330, start_date=2019-03-31
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMonthRevenue, start_date=2019-03-31 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), country (str), revenue (int64), revenue_month (int64), revenue_year (int64)

### TaiwanStockCapitalReductionReferencePrice (減資恢復買賣參考價格)
- Tier: Free
- Data range: 2011-01-01 ~ now
- Params: dataset=TaiwanStockCapitalReductionReferencePrice, data_id=2327, start_date=2010-01-01
- Columns: date (str), stock_id (str), ClosingPriceonTheLastTradingDay (float), PostReductionReferencePrice (float), LimitUp (float), LimitDown (float), OpeningReferencePrice (float), ExrightReferencePrice (float), ReasonforCapitalReduction (str)

### TaiwanStockMarketValue (台灣股價市值表)
- Tier: Backer/Sponsor
- Data range: 2004-01-01 ~ now
- Update: Mon-Fri 23:30
- Params: dataset=TaiwanStockMarketValue, data_id=2330, start_date=2023-01-01, end_date=2024-01-01
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMarketValue, start_date=2023-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: date (str), stock_id (str), market_value (int64)

### TaiwanStockDelisting (台灣股票下市櫃表)
- Tier: Free
- Data range: 2001-01-01 ~ now
- Params: dataset=TaiwanStockDelisting
- Columns: date (str), stock_id (str), stock_name (str)

### TaiwanStockMarketValueWeight (台股市值比重表)
- Tier: Backer/Sponsor
- Data range: 2024-10-30 ~ now
- Params: dataset=TaiwanStockMarketValueWeight, data_id=2330, start_date=2024-01-01, end_date=2025-01-01
- Params (all stocks, Backer/Sponsor): dataset=TaiwanStockMarketValueWeight, start_date=2024-01-01 (不帶 data_id，取得該日所有股票資料)
- Columns: rank (int64), stock_id (str), stock_name (str), weight_per (float32), date (str), type (str: twse/tpex)

### TaiwanStockSplitPrice (台股分割後參考價)
- Tier: Free
- Params: dataset=TaiwanStockSplitPrice
- Columns: date (str), stock_id (str), type (str: 分割/反分割), before_price (float), after_price (float), max_price (float), min_price (float), open_price (float)

### TaiwanStockParValueChange (台灣股票變更面額恢復買賣參考價格)
- Tier: Free
- Data range: 2020-01-01 ~ now
- Params: dataset=TaiwanStockParValueChange, start_date=2020-01-01
- Columns: date (str), stock_id (str), stock_name (str), before_close (float64), after_ref_close (float64), after_ref_max (float64), after_ref_min (float64), after_ref_open (float64)

---

## Taiwan Market - Derivative (16 datasets)

### TaiwanFutOptDailyInfo (期貨、選擇權日成交資訊總覽)
- Tier: Free
- Params: dataset=TaiwanFutOptDailyInfo
- Columns: code, type, name

### TaiwanFuturesDaily (期貨日成交資訊)
- Tier: Free (with data_id) / Backer/Sponsor (all)
- Data range: 1998-07-01 ~ now
- Params: dataset=TaiwanFuturesDaily, data_id=TX, start_date=2020-04-01, end_date=2020-04-12
- Params (all, Backer/Sponsor): dataset=TaiwanFuturesDaily, start_date=2020-04-01 (不帶 data_id，取得該日所有期貨資料)
- Columns: date, futures_id, contract_date, open, max, min, close, spread, spread_per, volume, settlement_price, open_interest, trading_session

### TaiwanOptionDaily (選擇權日成交資訊)
- Tier: Free (with data_id) / Backer/Sponsor (all)
- Data range: 2001-12-01 ~ now
- Params: dataset=TaiwanOptionDaily, data_id=TXO, start_date=2020-04-01, end_date=2020-04-02
- Params (all, Backer/Sponsor): dataset=TaiwanOptionDaily, start_date=2020-04-01 (不帶 data_id，取得該日所有選擇權資料)
- Columns: date, option_id, contract_date, strike_price, call_put, open, max, min, close, volume, settlement_price, open_interest, trading_session

### TaiwanFuturesTick (期貨交易明細表)
- Tier: Backer/Sponsor
- Data range: 2011-01-03 ~ now
- Params: dataset=TaiwanFuturesTick, data_id=MTX, start_date=2020-04-01
- Columns: contract_date, date, futures_id, price, volume
- Note: Single day per request.

### TaiwanOptionTIck (選擇權交易明細表)
- Tier: Backer/Sponsor
- Data range: 2011-01-03 ~ now
- Params: dataset=TaiwanOptionTIck, data_id=OCO, start_date=2020-04-01
- Columns: ExercisePrice, PutCall, contract_date, date, option_id, price, volume
- Note: Single day per request.

### TaiwanFuturesInstitutionalInvestors (期貨三大法人買賣)
- Tier: Free (with data_id) / Backer/Sponsor (all)
- Data range: 2018-06-05 ~ now
- Params: dataset=TaiwanFuturesInstitutionalInvestors, data_id=TX, start_date=2020-04-01, end_date=2020-04-12
- Params (all, Backer/Sponsor): dataset=TaiwanFuturesInstitutionalInvestors, start_date=2020-04-01 (不帶 data_id，取得該日所有期貨資料)
- Columns: name, date, institutional_investors, long_deal_volume, long_deal_amount, short_deal_volume, short_deal_amount, long_open_interest_balance_volume, long_open_interest_balance_amount, short_open_interest_balance_volume, short_open_interest_balance_amount

### TaiwanOptionInstitutionalInvestors (選擇權三大法人買賣)
- Tier: Free (with data_id) / Backer/Sponsor (all)
- Data range: 2018-06-05 ~ now
- Params: dataset=TaiwanOptionInstitutionalInvestors, data_id=TXO, start_date=2020-04-01, end_date=2020-04-12
- Params (all, Backer/Sponsor): dataset=TaiwanOptionInstitutionalInvestors, start_date=2020-04-01 (不帶 data_id，取得該日所有選擇權資料)
- Columns: name, date, call_put, institutional_investors, long_deal_volume, long_deal_amount, short_deal_volume, short_deal_amount, long_open_interest_balance_volume, long_open_interest_balance_amount, short_open_interest_balance_volume, short_open_interest_balance_amount

### TaiwanFuturesInstitutionalInvestorsAfterHours (期貨夜盤三大法人買賣)
- Tier: Backer/Sponsor
- Data range: 2021-10-12 ~ now
- Params: dataset=TaiwanFuturesInstitutionalInvestorsAfterHours, data_id=TX, start_date=2020-04-01, end_date=2020-04-12
- Params (all, Backer/Sponsor): dataset=TaiwanFuturesInstitutionalInvestorsAfterHours, start_date=2020-04-01 (不帶 data_id，取得該日所有期貨資料)
- Columns: futures_id, date, institutional_investors, long_deal_volume, long_deal_amount, short_deal_volume, short_deal_amount

### TaiwanOptionInstitutionalInvestorsAfterHours (選擇權夜盤三大法人買賣)
- Tier: Backer/Sponsor
- Data range: 2021-10-12 ~ now
- Params: dataset=TaiwanOptionInstitutionalInvestorsAfterHours, data_id=TXO, start_date=2020-04-01, end_date=2020-04-12
- Params (all, Backer/Sponsor): dataset=TaiwanOptionInstitutionalInvestorsAfterHours, start_date=2020-04-01 (不帶 data_id，取得該日所有選擇權資料)
- Columns: option_id, date, call_put, institutional_investors, long_deal_volume, long_deal_amount, short_deal_volume, short_deal_amount

### TaiwanFuturesDealerTradingVolumeDaily (期貨各卷商每日交易)
- Tier: Free
- Data range: 2021-04-01 ~ now
- Params: dataset=TaiwanFuturesDealerTradingVolumeDaily, data_id=TX, start_date=2020-07-01, end_date=2020-07-12
- Params (all, Backer/Sponsor): dataset=TaiwanFuturesDealerTradingVolumeDaily, start_date=2020-07-01 (不帶 data_id，取得該日所有期貨資料)
- Columns: date, dealer_code, dealer_name, futures_id, volume, is_after_hour

### TaiwanOptionDealerTradingVolumeDaily (選擇權各卷商每日交易)
- Tier: Free
- Data range: 2021-04-01 ~ now
- Params: dataset=TaiwanOptionDealerTradingVolumeDaily, data_id=TXO, start_date=2020-07-01, end_date=2020-07-12
- Params (all, Backer/Sponsor): dataset=TaiwanOptionDealerTradingVolumeDaily, start_date=2020-07-01 (不帶 data_id，取得該日所有選擇權資料)
- Columns: date, dealer_code, dealer_name, option_id, volume, is_after_hour

### TaiwanFuturesOpenInterestLargeTraders (期貨大額交易人未沖銷部位)
- Tier: Backer/Sponsor
- Data range: 1998-07-01 ~ now
- Params: dataset=TaiwanFuturesOpenInterestLargeTraders, data_id=TJF, start_date=2024-09-01, end_date=2024-09-02
- Params (all, Backer/Sponsor): dataset=TaiwanFuturesOpenInterestLargeTraders, start_date=2024-09-01 (不帶 data_id，取得該日所有期貨資料)
- Columns: name, contract_type, buy_top5_trader_open_interest, buy_top5_trader_open_interest_per, buy_top10_trader_open_interest, buy_top10_trader_open_interest_per, sell_top5_trader_open_interest, sell_top5_trader_open_interest_per, sell_top10_trader_open_interest, sell_top10_trader_open_interest_per, market_open_interest, buy_top5_specific_open_interest, buy_top5_specific_open_interest_per, buy_top10_specific_open_interest, buy_top10_specific_open_interest_per, sell_top5_specific_open_interest, sell_top5_specific_open_interest_per, sell_top10_specific_open_interest, sell_top10_specific_open_interest_per, date, futures_id

### TaiwanOptionOpenInterestLargeTraders (選擇權大額交易人未沖銷部位)
- Tier: Backer/Sponsor
- Data range: 1998-07-01 ~ now
- Params: dataset=TaiwanOptionOpenInterestLargeTraders, data_id=CA, start_date=2024-09-01, end_date=2024-09-02
- Params (all, Backer/Sponsor): dataset=TaiwanOptionOpenInterestLargeTraders, start_date=2024-09-01 (不帶 data_id，取得該日所有選擇權資料)
- Columns: contract_type, buy_top5_trader_open_interest, buy_top5_trader_open_interest_per, buy_top10_trader_open_interest, buy_top10_trader_open_interest_per, sell_top5_trader_open_interest, sell_top5_trader_open_interest_per, sell_top10_trader_open_interest, sell_top10_trader_open_interest_per, market_open_interest, buy_top5_specific_open_interest, buy_top5_specific_open_interest_per, buy_top10_specific_open_interest, buy_top10_specific_open_interest_per, sell_top5_specific_open_interest, sell_top5_specific_open_interest_per, sell_top10_specific_open_interest, sell_top10_specific_open_interest_per, date, put_call, name, option_id

### TaiwanFuturesSpreadTrading (期貨價差行情表)
- Tier: Backer/Sponsor
- Data range: 2007-10-08 ~ now
- Params: dataset=TaiwanFuturesSpreadTrading, data_id=TX, start_date=2020-04-01, end_date=2020-04-12
- Columns: date, futures_id, contract_date, open, max, min, close, best_bid, best_ask, historical_max, historical_min, spread_to_spread_volume, spread_to_single_volume, trading_session

### TaiwanFuturesFinalSettlementPrice (期貨最後結算價)
- Tier: Backer/Sponsor
- Data range: 1998-01-01 ~ now
- Params: dataset=TaiwanFuturesFinalSettlementPrice, data_id=TX, start_date=2020-04-01, end_date=2020-04-12
- Columns: date, contract_month, futures_type, futures_id, futures_name, settlement_price, underlying_code, notional_value

### TaiwanOptionFinalSettlementPrice (選擇權最後結算價)
- Tier: Backer/Sponsor
- Data range: 2001-01-01 ~ now
- Params: dataset=TaiwanOptionFinalSettlementPrice, data_id=TXO, start_date=2020-04-01, end_date=2020-04-12
- Columns: date, contract_month, option_type, option_id, option_name, settlement_price, underlying_code, notional_value

---

## Taiwan Market - Real-Time (4 datasets)

### taiwan_stock_tick_snapshot (台股即時資訊)
- Tier: Sponsor
- Params: dataset=taiwan_stock_tick_snapshot, data_id=2330 (or empty for all)
- Columns: amount, average_price, buy_price, buy_volume, change_price, change_rate, close, high, low, open, sell_price, sell_volume, total_amount, total_volume, volume, volume_ratio, yesterday_volume, date, stock_id, TickType

### TaiwanFutOptTickInfo (期貨、選擇權即時報價總覽)
- Tier: Free
- Params: dataset=TaiwanFutOptTickInfo
- Columns: code, callput, date, name, listing_date, expire_price, update_date

### taiwan_futures_snapshot (台股期貨即時資訊)
- Tier: Sponsor
- Params: dataset=taiwan_futures_snapshot, data_id=TXF (or empty for all)
- Columns: open, high, low, close, change_price, change_rate, average_price, volume, total_volume, amount, total_amount, yesterday_volume, buy_price, buy_volume, sell_price, sell_volume, volume_ratio, date, futures_id, TickType

### taiwan_options_snapshot (台股選擇權即時資訊)
- Tier: Sponsor
- Params: dataset=taiwan_options_snapshot, data_id=TXO (or empty for all)
- Columns: open, high, low, close, change_price, change_rate, average_price, volume, total_volume, amount, total_amount, yesterday_volume, buy_price, buy_volume, sell_price, sell_volume, volume_ratio, date, options_id, TickType

---

## Taiwan Market - Convertible Bond (4 datasets)

### TaiwanStockConvertibleBondInfo (可轉債總覽)
- Tier: Backer/Sponsor
- Params: dataset=TaiwanStockConvertibleBondInfo
- Columns: cb_id, cb_name, InitialDateOfConversion, DueDateOfConversion, IssuanceAmount

### TaiwanStockConvertibleBondDaily (可轉債日成交資訊)
- Tier: Backer/Sponsor
- Params: dataset=TaiwanStockConvertibleBondDaily, data_id=15131, start_date=2020-04-01, end_date=2020-04-10
- Params (all, Backer/Sponsor): dataset=TaiwanStockConvertibleBondDaily, start_date=2020-04-01 (不帶 data_id，取得該日所有可轉債資料)
- Columns: cb_id, cb_name, transaction_type, close, change, open, max, min, no_of_transactions, unit, trading_value, avg_price, next_ref_price, next_max_limit, next_min_limit, date

### TaiwanStockConvertibleBondInstitutionalInvestors (可轉債三大法人日交易資訊)
- Tier: Backer/Sponsor
- Params: dataset=TaiwanStockConvertibleBondInstitutionalInvestors, data_id=15131, start_date=2020-04-01, end_date=2020-04-10
- Params (all, Backer/Sponsor): dataset=TaiwanStockConvertibleBondInstitutionalInvestors, start_date=2020-04-01 (不帶 data_id，取得該日所有可轉債資料)
- Columns: Foreign_Investor_Buy, Foreign_Investor_Sell, Foreign_Investor_Overbuy, Investment_Trust_Buy, Investment_Trust_Sell, Investment_Trust_Overbuy, Dealer_self_Buy, Dealer_self_Sell, Dealer_self_Overbuy, Total_Overbuy, cb_id, cb_name, date

### TaiwanStockConvertibleBondDailyOverview (可轉債每日總覽資訊)
- Tier: Backer/Sponsor
- Params: dataset=TaiwanStockConvertibleBondDailyOverview, data_id=15131, start_date=2020-04-01, end_date=2020-04-10
- Params (all, Backer/Sponsor): dataset=TaiwanStockConvertibleBondDailyOverview, start_date=2020-04-01 (不帶 data_id，取得該日所有可轉債資料)
- Columns: cb_id, cb_name, date, InitialDateOfConversion, DueDateOfConversion, InitialDateOfStopConversion, DueDateOfStopConversion, ConversionPrice, NextEffectiveDateOfConversionPrice, LatestInitialDateOfPut, LatestDueDateOfPut, LatestPutPrice, InitialDateOfEarlyRedemption, DueDateOfEarlyRedemption, EarlyRedemptionPrice, DateOfDelisted, IssuanceAmount, OutstandingAmount, ReferencePrice, PriceOfUnderlyingStock, InitialDateOfSuspension, DueDateOfSuspension, CouponRate

---

## Taiwan Market - Others (3 datasets)

### TaiwanStockNews (相關新聞)
- Tier: Free
- Params: dataset=TaiwanStockNews, data_id=2330, start_date=2020-04-01
- Columns: date, stock_id, description, link, source, title
- Note: Single day per request.

### TaiwanBusinessIndicator (台灣每月景氣對策信號表)
- Tier: Backer/Sponsor
- Data range: 1982-01-01 ~ now
- Params: dataset=TaiwanBusinessIndicator, start_date=2020-01-01, end_date=2020-04-01
- Columns: date, leading, leading_notrend, coincident, coincident_notrend, lagging, lagging_notrend, monitoring, monitoring_color

### TaiwanStockIndustryChain (個體公司所屬產業鏈)
- Tier: Backer/Sponsor
- Params: dataset=TaiwanStockIndustryChain
- Columns: stock_id, industry, sub_industry, date

---

## International Markets

### USStockInfo (美股總覽)
- Tier: Free
- Params: dataset=USStockInfo
- Columns: date, stock_id, Country, IPOYear, MarketCap, Subsector, stock_name

### USStockPrice (美股股價 daily)
- Tier: Free
- Params: dataset=USStockPrice, data_id=^DJI, start_date=2020-06-16, end_date=2021-06-16
- Columns: date, stock_id, Adj_Close, Close, High, Low, Open, Volume

### USStockPriceMinute (美股股價 minute)
- Tier: Backer/Sponsor
- Params: dataset=USStockPriceMinute, data_id=^DJI, start_date=2022-06-16
- Columns: date, stock_id, close, high, low, open, volume

### UKStockInfo (英股總覽)
- Tier: Free
- Params: dataset=UKStockInfo
- Columns: date, stock_id, Country, stock_name

### UKStockPrice (英股股價)
- Tier: Free
- Params: dataset=UKStockPrice, data_id=BP.L, start_date=2020-06-16, end_date=2021-06-16
- Columns: date, stock_id, Adj_Close, Close, High, Low, Open, Volume

### EuropeStockInfo (歐股總覽)
- Tier: Free
- Params: dataset=EuropeStockInfo
- Columns: date, stock_id, Market, stock_name

### EuropeStockPrice (歐股股價)
- Tier: Free
- Params: dataset=EuropeStockPrice, data_id=AALB.AS, start_date=2020-06-16, end_date=2021-06-16
- Columns: date, stock_id, Adj_Close, Close, High, Low, Open, Volume

### JapanStockInfo (日股總覽)
- Tier: Free
- Params: dataset=JapanStockInfo
- Columns: date, stock_id, Exchange, Sector, stock_name

### JapanStockPrice (日股股價)
- Tier: Free
- Params: dataset=JapanStockPrice, data_id=7203.T, start_date=2020-06-16, end_date=2021-06-16
- Columns: date, stock_id, Adj_Close, Close, High, Low, Open, Volume

---

## Global Economic Data

### TaiwanExchangeRate (外幣對台幣匯率)
- Tier: Free
- Params: dataset=TaiwanExchangeRate, data_id=USD, start_date=2006-01-01
- data_id values: USD, EUR, JPY, GBP, CNY, HKD, AUD, CAD, CHF, IDR, KRW, MYR, NZD, PHP, SEK, SGD, THB, VND, ZAR
- Columns: date, currency, cash_buy, cash_sell, spot_buy, spot_sell

### InterestRate (央行利率)
- Tier: Free
- Params: dataset=InterestRate, data_id=FED, start_date=1982-01-01
- data_id values: FED, BOE, RBA, PBOC, BOC, ECB, RBNZ, RBI, CBR, BCB, BOJ, SNB
- Columns: country, date, full_country_name, interest_rate

### GoldPrice (黃金價格)
- Tier: Free
- Params: dataset=GoldPrice, start_date=2020-01-01, end_date=2020-01-02
- Columns: Price, date

### CrudeOilPrices (原油價格)
- Tier: Free
- Params: dataset=CrudeOilPrices, data_id=WTI, start_date=2020-01-01, end_date=2020-01-08
- data_id values: WTI, Brent
- Columns: date, name, price

### GovernmentBondsYield (美國國債殖利率)
- Tier: Free
- Params: dataset=GovernmentBondsYield, data_id=United States 10-Year, start_date=2020-01-01
- data_id values: "United States 1-Month", "United States 2-Month", "United States 3-Month", "United States 6-Month", "United States 1-Year", "United States 2-Year", "United States 3-Year", "United States 5-Year", "United States 7-Year", "United States 10-Year", "United States 20-Year", "United States 30-Year"
- Columns: date, name, value

### CnnFearGreedIndex (CNN 恐懼與貪婪指數)
- Tier: Backer/Sponsor
- Data range: 2011-01-03 ~ now
- Params: dataset=CnnFearGreedIndex, start_date=2021-01-01, end_date=2021-01-10
- Columns: date, fear_greed, fear_greed_emotion

---

## Links

- Documentation: https://finmind.github.io/
- GitHub: https://github.com/FinMind/FinMind
- Official Website: https://finmindtrade.com/
- API Schema: https://finmindtrade.com/analysis/#/data/document