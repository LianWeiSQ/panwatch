import { fetchAPI } from './client'

export interface StockAgentInfo {
  agent_name: string
  schedule: string
  ai_model_id: number | null
  notify_channel_ids: number[]
}

export interface StockItem {
  id: number
  symbol: string
  name: string
  market: string
  instrument_id?: number | null
  instrument_type?: string
  exchange?: string | null
  underlying_symbol?: string | null
  underlying_name?: string | null
  contract_multiplier?: number | null
  tick_size?: number | null
  expiry_date?: string | null
  is_main_contract?: boolean | null
  sort_order?: number
  agents?: StockAgentInfo[]
}

export interface StockWorkspaceAccount {
  id: number
  name: string
  available_funds: number
  enabled: boolean
}

export interface StockWorkspacePortfolioPosition {
  id: number
  stock_id: number
  instrument_id?: number | null
  instrument_type?: string
  symbol: string
  name: string
  market: string
  exchange?: string | null
  underlying_symbol?: string | null
  underlying_name?: string | null
  contract_multiplier?: number | null
  expiry_date?: string | null
  is_main_contract?: boolean | null
  cost_price: number
  quantity: number
  invested_amount: number | null
  sort_order?: number
  trading_style: string
  current_price: number | null
  current_price_cny?: number | null
  change_pct: number | null
  market_value?: number | null
  market_value_cny?: number | null
  pnl?: number | null
  pnl_pct?: number | null
  exchange_rate?: number | null
}

export interface StockWorkspacePortfolioAccount {
  id: number
  name: string
  available_funds: number
  total_market_value: number
  total_cost: number
  total_pnl: number
  total_pnl_pct: number
  total_assets: number
  positions: StockWorkspacePortfolioPosition[]
}

export interface StockWorkspaceResponse {
  generated_at: string
  market_status: Array<{
    code: string
    name: string
    status: string
    status_text: string
    is_trading: boolean
    sessions: string[]
    local_time: string
    timezone?: string
  }>
  accounts: StockWorkspaceAccount[]
  stocks: StockItem[]
  portfolio: {
    accounts: StockWorkspacePortfolioAccount[]
    total: {
      total_market_value: number
      total_cost: number
      total_pnl: number
      total_pnl_pct: number
      available_funds: number
      total_assets: number
    }
    exchange_rates?: Record<string, number>
    quotes?: Record<string, { current_price: number | null; change_pct: number | null }>
    quotes_by_key?: Record<string, { current_price: number | null; change_pct: number | null }>
  }
  quotes: Record<string, { current_price: number | null; change_pct: number | null }>
  kline_summaries: Record<string, Record<string, any>>
  pool_suggestions: Record<string, Record<string, any>>
  price_alert_summaries: Record<string, { total: number; enabled: number }>
}

export interface StockCreatePayload {
  symbol: string
  name: string
  market: string
}

export interface StockAgentUpdatePayload {
  agents: Array<{
    agent_name: string
    schedule?: string
    ai_model_id?: number | null
    notify_channel_ids?: number[]
  }>
}

export interface TriggerStockAgentOptions {
  bypass_throttle?: boolean
  bypass_market_hours?: boolean
  allow_unbound?: boolean
  wait?: boolean
  symbol?: string
  market?: string
  name?: string
}

export interface TriggerStockAgentResponse {
  result?: Record<string, any>
  code?: number
  success?: boolean
  message: string
  queued?: boolean
}

function withQuery(path: string, params: TriggerStockAgentOptions): string {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return
    const sv = String(v).trim()
    if (!sv) return
    q.set(k, sv)
  })
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

export const stocksApi = {
  list: () => fetchAPI<StockItem[]>('/stocks'),
  workspace: () => fetchAPI<StockWorkspaceResponse>('/stocks/workspace', { timeoutMs: 45000 }),
  create: (payload: StockCreatePayload) =>
    fetchAPI<StockItem>('/stocks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  remove: (id: number) => fetchAPI<{ ok: boolean }>(`/stocks/${id}`, { method: 'DELETE' }),
  updateAgents: (id: number, payload: StockAgentUpdatePayload) =>
    fetchAPI<StockItem>(`/stocks/${id}/agents`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  triggerAgent: (id: number, agentName: string, options: TriggerStockAgentOptions = {}) =>
    fetchAPI<TriggerStockAgentResponse>(
      withQuery(`/stocks/${id}/agents/${encodeURIComponent(agentName)}/trigger`, options),
      { method: 'POST', timeoutMs: 120_000 }
    ),
}
