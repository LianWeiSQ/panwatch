import { fetchAPI } from './client'

export interface PaperTradingAccountResponse {
  id: number
  initial_capital: number
  current_capital: number
  total_equity: number
  total_pnl: number
  unrealized_pnl: number
  total_trades: number
  winning_trades: number
  win_rate: number
  max_drawdown_pct: number
  peak_capital: number
  enabled: boolean
  excluded_markets: string[]
  created_at: string
  updated_at: string
}

export interface PaperTradingPositionItem {
  id: number
  stock_symbol: string
  stock_market: string
  stock_name: string
  quantity: number
  entry_price: number
  stop_loss?: number | null
  target_price?: number | null
  current_price?: number | null
  unrealized_pnl: number
  unrealized_pnl_pct: number
  status: string
  signal_run_id?: number | null
  signal_snapshot_date: string
  signal_action: string
  strategy_code: string
  holding_days: number
  opened_at: string
  closed_at: string
  updated_at: string
}

export interface PaperTradingTradeItem {
  id: number
  stock_symbol: string
  stock_market: string
  stock_name: string
  quantity: number
  entry_price: number
  exit_price: number
  pnl: number
  pnl_pct: number
  exit_reason: string
  signal_run_id?: number | null
  signal_snapshot_date: string
  strategy_code: string
  holding_days: number
  opened_at: string
  closed_at: string
}

export interface PaperTradingTradesResponse {
  total: number
  items: PaperTradingTradeItem[]
}

export interface EquityCurvePoint {
  date: string
  equity: number
}

export interface StrategyPerformanceItem {
  strategy_code: string
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl_pct: number
  avg_holding_days: number
  open_positions: number
  unrealized_pnl: number
}

export interface PaperTradingMetricsResponse {
  account: PaperTradingAccountResponse | null
  equity_curve: EquityCurvePoint[]
  open_positions: number
  strategy_performance: StrategyPerformanceItem[]
}

export interface NotifyChannelItem {
  id: number
  name: string
  type: string
  is_default: boolean
}

export interface PaperTradingNotifySettings {
  settings: {
    pt_notify_enabled: string
    pt_notify_channel_ids: string
    pt_notify_realtime: string
    pt_notify_premarket: string
    pt_notify_summary: string
  }
  channels: NotifyChannelItem[]
}

export const paperTradingApi = {
  getAccount: () =>
    fetchAPI<PaperTradingAccountResponse>('/paper-trading/account'),

  listPositions: (status = 'open') =>
    fetchAPI<PaperTradingPositionItem[]>(`/paper-trading/positions?status=${encodeURIComponent(status)}`),

  listTrades: (limit = 50, offset = 0) =>
    fetchAPI<PaperTradingTradesResponse>(
      `/paper-trading/trades?limit=${encodeURIComponent(String(limit))}&offset=${encodeURIComponent(String(offset))}`
    ),

  getMetrics: () =>
    fetchAPI<PaperTradingMetricsResponse>('/paper-trading/metrics'),

  toggleAccount: (enabled: boolean) =>
    fetchAPI<PaperTradingAccountResponse>('/paper-trading/account/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  resetAccount: () =>
    fetchAPI<{ ok: boolean }>('/paper-trading/account/reset', {
      method: 'POST',
    }),

  closePosition: (positionId: number) =>
    fetchAPI<{ ok: boolean }>(`/paper-trading/positions/${encodeURIComponent(String(positionId))}/close`, {
      method: 'POST',
    }),

  updateSettings: (settings: { excluded_markets?: string[] }) =>
    fetchAPI<PaperTradingAccountResponse>('/paper-trading/account/settings', {
      method: 'POST',
      body: JSON.stringify(settings),
    }),

  scan: () =>
    fetchAPI<{ status: string; opened: number; closed: number }>('/paper-trading/scan', {
      method: 'POST',
      timeoutMs: 30000,
    }),

  getNotifySettings: () =>
    fetchAPI<PaperTradingNotifySettings>('/paper-trading/notify-settings'),

  updateNotifySettings: (settings: Record<string, string>) =>
    fetchAPI<PaperTradingNotifySettings>('/paper-trading/notify-settings', {
      method: 'POST',
      body: JSON.stringify(settings),
    }),

  testNotify: () =>
    fetchAPI<{ ok: boolean }>('/paper-trading/notify-test', {
      method: 'POST',
    }),
}
