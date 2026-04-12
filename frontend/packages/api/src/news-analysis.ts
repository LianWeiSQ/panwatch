import { fetchAPI } from './client'

type QueryValue = string | number | boolean | null | undefined

function withQuery(path: string, params: Record<string, QueryValue>): string {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null) return
    const stringValue = String(value).trim()
    if (!stringValue) return
    query.set(key, stringValue)
  })
  const qs = query.toString()
  return qs ? `${path}?${qs}` : path
}

export interface NewsAnalysisTopic {
  name: string
  score: number
  sentiment: string
}

export interface NewsAnalysisCoverage {
  total_articles?: number
  related_articles?: number
  configured_sources?: number
  enabled_sources?: number
  languages?: string[]
}

export interface NewsAnalysisArticle {
  id: number
  provider: string
  provider_type: string
  source_name: string
  language: string
  title: string
  summary: string
  cn_summary: string
  content: string
  url: string
  published_at: string
  fetched_at: string
  symbols: string[]
  relevance_score: number
  importance: number
  is_related?: boolean
  payload: Record<string, unknown>
}

export interface NewsAnalysisSourceStatus {
  provider: string
  provider_type: string
  source_id: number
  source_name: string
  enabled: boolean
  status: string
  last_success_at: string
  last_attempt_at: string
  last_error: string
  article_count: number
  meta: Record<string, unknown>
}

export interface NewsAnalysisRuntimeResponse {
  generated_at: string
  hours: number
  summary: string
  topics: NewsAnalysisTopic[]
  sentiment: string
  coverage: NewsAnalysisCoverage
  analysis: {
    id: number | null
    title: string
    content: string
    analysis_date: string
    updated_at: string
  }
  articles: NewsAnalysisArticle[]
  related_articles: NewsAnalysisArticle[]
  source_statuses: NewsAnalysisSourceStatus[]
  watchlist: Array<{ symbol: string; name: string }>
  latest_article_at: string
  latest_snapshot_at: string
}

export interface NewsAnalysisRefreshResponse extends NewsAnalysisRuntimeResponse {
  refresh_result?: {
    timestamp?: string
    articles: number
    related_articles: number
    sentiment: string
  }
}

export interface NewsAnalysisRuntimeParams {
  hours?: number
  related_only?: boolean
  language?: string
  source?: string
  limit?: number
}

export const newsAnalysisApi = {
  runtime: (params?: NewsAnalysisRuntimeParams) =>
    fetchAPI<NewsAnalysisRuntimeResponse>(
      withQuery('/news-analysis/runtime', {
        hours: params?.hours,
        related_only: params?.related_only,
        language: params?.language,
        source: params?.source,
        limit: params?.limit,
      }),
      { timeoutMs: 45000 }
    ),

  refresh: (params?: NewsAnalysisRuntimeParams) =>
    fetchAPI<NewsAnalysisRefreshResponse>(
      withQuery('/news-analysis/refresh', {
        hours: params?.hours,
        related_only: params?.related_only,
        language: params?.language,
        source: params?.source,
        limit: params?.limit,
      }),
      {
        method: 'POST',
        timeoutMs: 120000,
      }
    ),
}
