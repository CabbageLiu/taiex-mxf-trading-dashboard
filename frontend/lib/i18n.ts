// 繁體中文 dictionary. Single locale; technical-indicator names stay English
// (MACD/DMI/KD/RSI/MA) and are referenced directly, never through `t()`.

export const dict = {
  app_title: "台指期儀表板",
  app_subtitle: "資料來源：FinMind 加權指數 5 秒 · 作為小台代理",

  panel_indicators: "技術指標",
  panel_strategies: "策略",
  panel_live_signals: "即時訊號",
  panel_alert_delivery: "通知遞送",

  label_period: "週期",
  label_rsi_period: "RSI 週期",

  state_loading: "載入中…",
  state_failed_prefix: "載入失敗：",
  state_none_strategies: "尚未註冊任何策略。",
  state_none: "尚無資料。",

  btn_on: "啟用",
  btn_off: "關閉",
  btn_save: "儲存",
  btn_cancel: "取消",

  status_live: "即時",
  status_reconnecting: "重新連線中",

  side_long: "多",
  side_short: "空",
  side_exit: "平",
  side_flat: "觀望",

  channels_label: "通知頻道",
  resolutions_label: "週期",

  // V2 — navigation
  "nav.trading": "交易",
  "nav.analysis": "分析",
  "nav.backtest": "回測",

  // Backtest
  "bt.title": "策略回測",
  "bt.strategy": "策略",
  "bt.start": "起始日",
  "bt.end": "結束日",
  "bt.run": "執行回測",
  "bt.running": "執行中…",
  "bt.empty": "選擇策略與區間後點擊執行。",
  "bt.error": "回測失敗：",
  "bt.equity": "淨值曲線",
  "bt.tradesTitle": "交易明細",
  "bt.cols.entry": "進場",
  "bt.cols.exit": "出場",
  "bt.cols.bars": "K 棒數",
  "bt.cols.reason": "原因",
  "kpi.profitFactor": "獲利因子",
  "kpi.avgBars": "平均持倉 K 棒",
  "kpi.largestWin": "最大單筆獲利",
  "kpi.largestLoss": "最大單筆虧損",

  // V2 — status pill
  "status.ok": "連線正常",
  "status.lag": "資料延遲",
  "status.error": "連線錯誤",
  "status.lastTick": "最後 Tick",
  "status.lagSec": "延遲秒數",
  "status.db": "資料庫",
  "status.notifiers": "通知通道",

  // V2 — KPI strip
  "kpi.winRate": "勝率",
  "kpi.trades": "交易筆數",
  "kpi.pnl": "累積損益",
  "kpi.drawdown": "最大回撤",
  "kpi.unit.points": "點",
  "kpi.unit.trades": "筆",

  // V2 — filters
  "filter.all": "全部",
  "filter.win": "獲利",
  "filter.loss": "虧損",
  "filter.dateRange": "日期區間",

  // V2 — trades table
  "trades.col.date": "日期",
  "trades.col.side": "方向",
  "trades.col.entry": "進場",
  "trades.col.exit": "出場",
  "trades.col.hold": "持倉時長",
  "trades.col.pnl": "損益",
  "trades.empty": "此區間無交易紀錄",
  "trades.loading": "讀取中…",

  // V2 — side labels
  "side.long": "多",
  "side.short": "空",

  // V2 — insight panel
  "insight.title": "AI 建議",
  "insight.generate": "生成洞察",
  "insight.empty": "點擊上方按鈕，由 Sonnet 產生本期洞察。",
  "insight.cached": "已快取",
  "insight.loading": "Sonnet 思考中…",
  "insight.error": "生成失敗，請稍後重試",

  // V2 — patterns block
  "patterns.title": "模式分析",

  // V2 — chart crosshair
  "crosshair.time": "時間",
  "crosshair.ohlc": "OHLC",
} as const;

export type DictKey = keyof typeof dict;

export function t(key: DictKey): string {
  return dict[key];
}

const SIDE_MAP: Record<string, string> = {
  LONG: dict.side_long,
  SHORT: dict.side_short,
  EXIT: dict.side_exit,
  FLAT: dict.side_flat,
};

export function tSide(side: string): string {
  return SIDE_MAP[side?.toUpperCase()] ?? side;
}
