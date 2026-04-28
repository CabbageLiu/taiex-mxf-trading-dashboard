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

  status_live: "即時",
  status_reconnecting: "重新連線中",

  side_long: "多",
  side_short: "空",
  side_exit: "平",
  side_flat: "觀望",

  channels_label: "通知頻道",
  resolutions_label: "週期",
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
