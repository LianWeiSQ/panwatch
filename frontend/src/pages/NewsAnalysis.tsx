import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import { Newspaper, RefreshCw, Clock3, Languages, Link as LinkIcon, RadioTower, Sparkles } from 'lucide-react'
import { fetchAPI, newsAnalysisApi, type NewsAnalysisArticle, type NewsAnalysisCoverage, type NewsAnalysisRuntimeResponse } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { formatDateTime, useLocalStorage } from '@/lib/utils'
import ScheduleEditorDialog, { formatSchedule } from '@/components/ScheduleEditorDialog'

interface AgentConfig {
  id: number
  name: string
  display_name: string
  enabled: boolean
  schedule: string
  config: Record<string, unknown>
}

const HOURS_OPTIONS = [
  { label: '24 小时', value: 24 },
  { label: '72 小时', value: 72 },
  { label: '7 天', value: 168 },
]

const AUTO_REFRESH_OPTIONS = [
  { label: '30 秒', value: 30 },
  { label: '60 秒', value: 60 },
  { label: '5 分钟', value: 300 },
]

function sentimentLabel(sentiment: string): string {
  if (sentiment === 'positive') return '偏多'
  if (sentiment === 'negative') return '偏空'
  return '中性'
}

function sentimentClass(sentiment: string): string {
  if (sentiment === 'positive') return 'bg-rose-500/10 text-rose-500 border-rose-500/20'
  if (sentiment === 'negative') return 'bg-emerald-500/10 text-emerald-600 border-emerald-500/20'
  return 'bg-accent/40 text-muted-foreground border-border/60'
}

function sourceStatusClass(status: string): string {
  if (status === 'success') return 'bg-emerald-500/10 text-emerald-600 border-emerald-500/20'
  if (status === 'error') return 'bg-rose-500/10 text-rose-500 border-rose-500/20'
  if (status === 'disabled') return 'bg-accent/40 text-muted-foreground border-border/60'
  return 'bg-amber-500/10 text-amber-600 border-amber-500/20'
}

function languageLabel(language: string): string {
  if ((language || '').startsWith('zh')) return '中文'
  if ((language || '').startsWith('en')) return '英文'
  return language || '未知'
}

function toCount(value: number | undefined, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function toLanguageList(coverage: NewsAnalysisCoverage | undefined): string[] {
  return Array.isArray(coverage?.languages)
    ? coverage.languages.filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    : []
}

function ArticleCard({ article }: { article: NewsAnalysisArticle }) {
  const isEnglish = (article.language || '').startsWith('en')
  return (
    <div className="rounded-xl border border-border/50 bg-accent/20 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="text-[10px]">{article.source_name}</Badge>
        <Badge variant="outline" className={`text-[10px] ${article.is_related ? 'border-primary/30 text-primary' : ''}`}>
          {article.is_related ? '自选/持仓相关' : '全市场'}
        </Badge>
        <Badge variant="outline" className="text-[10px]">{languageLabel(article.language)}</Badge>
        <span className="text-[11px] text-muted-foreground font-mono">{formatDateTime(article.published_at)}</span>
      </div>

      <div className="mt-3 text-[15px] font-semibold text-foreground leading-relaxed">{article.title}</div>
      {article.symbols?.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {article.symbols.map(symbol => (
            <span key={symbol} className="px-2 py-0.5 rounded-full bg-primary/10 text-primary text-[11px]">
              {symbol}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 space-y-2 text-[13px] leading-6 text-muted-foreground">
        {isEnglish ? (
          <>
            <div className="text-foreground/90">中文摘要：{article.cn_summary || '暂无中文摘要'}</div>
            {article.summary ? <div className="text-muted-foreground/80">原文摘要：{article.summary}</div> : null}
          </>
        ) : (
          <div className="text-foreground/90">{article.cn_summary || article.summary || article.content || '暂无摘要'}</div>
        )}
      </div>

      <div className="mt-3 flex items-center justify-between gap-3 text-[12px]">
        <span className="text-muted-foreground">
          相关度 <span className="font-mono text-foreground/90">{article.relevance_score.toFixed(1)}</span>
        </span>
        {article.url ? (
          <a
            href={article.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-primary hover:underline"
          >
            <LinkIcon className="w-3.5 h-3.5" />
            原文链接
          </a>
        ) : null}
      </div>
    </div>
  )
}

export default function NewsAnalysisPage() {
  const navigate = useNavigate()
  const { toast } = useToast()

  const [runtime, setRuntime] = useState<NewsAnalysisRuntimeResponse | null>(null)
  const [agent, setAgent] = useState<AgentConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [savingSchedule, setSavingSchedule] = useState(false)
  const [updatingAgent, setUpdatingAgent] = useState(false)
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [error, setError] = useState('')

  const [autoRefresh, setAutoRefresh] = useLocalStorage('panwatch_news_analysis_auto_refresh', false)
  const [refreshInterval, setRefreshInterval] = useLocalStorage('panwatch_news_analysis_refresh_interval', 60)
  const [hours, setHours] = useLocalStorage('panwatch_news_analysis_hours', 72)
  const [language, setLanguage] = useLocalStorage('panwatch_news_analysis_language', 'all')
  const [source, setSource] = useLocalStorage('panwatch_news_analysis_source', 'all')
  const [viewMode, setViewMode] = useLocalStorage<'all' | 'related'>('panwatch_news_analysis_view_mode', 'all')

  const params = useMemo(() => ({
    hours,
    related_only: viewMode === 'related',
    language: language === 'all' ? '' : language,
    source: source === 'all' ? '' : source,
    limit: 120,
  }), [hours, language, source, viewMode])

  const loadAgent = async () => {
    const agents = await fetchAPI<AgentConfig[]>('/agents')
    setAgent(agents.find(item => item.name === 'news_digest') || null)
  }

  const loadRuntime = async (showSpinner = false) => {
    if (showSpinner) setLoading(true)
    try {
      const data = await newsAnalysisApi.runtime(params)
      setRuntime(data)
      setError('')
    } catch (err) {
      const message = err instanceof Error ? err.message : '加载失败'
      setError(message)
      toast(message, 'error')
    } finally {
      if (showSpinner) setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setLoading(true)
      try {
        const [data, agents] = await Promise.all([
          newsAnalysisApi.runtime(params),
          fetchAPI<AgentConfig[]>('/agents'),
        ])
        if (cancelled) return
        setRuntime(data)
        setAgent(agents.find(item => item.name === 'news_digest') || null)
        setError('')
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : '加载失败'
        setError(message)
        toast(message, 'error')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [hours, language, params, source, toast, viewMode])

  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => {
      loadRuntime(false).catch(() => {})
    }, Math.max(15, refreshInterval) * 1000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, refreshInterval, params])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const data = await newsAnalysisApi.refresh(params)
      setRuntime(data)
      toast('新闻分析已刷新', 'success')
    } catch (err) {
      toast(err instanceof Error ? err.message : '刷新失败', 'error')
    } finally {
      setRefreshing(false)
    }
  }

  const toggleAgentEnabled = async () => {
    if (!agent) return
    setUpdatingAgent(true)
    try {
      await fetchAPI(`/agents/${agent.name}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: !agent.enabled }),
      })
      await loadAgent()
      toast(agent.enabled ? '后台调度已停用' : '后台调度已启用', 'success')
    } catch (err) {
      toast(err instanceof Error ? err.message : '更新失败', 'error')
    } finally {
      setUpdatingAgent(false)
    }
  }

  const saveSchedule = async (schedule: string) => {
    if (!agent) return
    setSavingSchedule(true)
    try {
      await fetchAPI(`/agents/${agent.name}`, {
        method: 'PUT',
        body: JSON.stringify({ schedule }),
      })
      await loadAgent()
      toast('后台调度策略已更新', 'success')
    } finally {
      setSavingSchedule(false)
    }
  }

  const sourceOptions = useMemo(() => {
    if (!runtime) return []
    return runtime.source_statuses.map(item => ({
      value: item.source_name,
      label: item.source_name,
    }))
  }, [runtime])

  const headlineCoverage = runtime?.coverage
  const totalArticles = toCount(headlineCoverage?.total_articles, runtime?.articles.length ?? 0)
  const relatedArticles = toCount(headlineCoverage?.related_articles, runtime?.related_articles.length ?? 0)
  const enabledSources = toCount(headlineCoverage?.enabled_sources, 0)
  const configuredSources = toCount(headlineCoverage?.configured_sources, runtime?.source_statuses.length ?? 0)
  const coverageLanguages = toLanguageList(headlineCoverage)
  const latestUpdate = runtime?.latest_snapshot_at || runtime?.analysis.updated_at || runtime?.latest_article_at || runtime?.generated_at || ''

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <div className="w-10 h-10 rounded-2xl bg-primary/10 text-primary flex items-center justify-center">
              <Newspaper className="w-5 h-5" />
            </div>
            <div>
              <h1 className="text-[22px] font-bold tracking-tight text-foreground">新闻分析</h1>
              <p className="text-[13px] text-muted-foreground">多源全市场新闻 + 自选/持仓关联分析</p>
            </div>
          </div>
        </div>

        <div className="card p-3 lg:min-w-[560px]">
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={handleRefresh} disabled={refreshing} className="h-8">
                {refreshing ? (
                  <span className="w-3.5 h-3.5 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                ) : (
                  <RefreshCw className="w-3.5 h-3.5" />
                )}
                {refreshing ? '刷新中' : '手动刷新'}
              </Button>

              <div className="flex items-center gap-2 rounded-lg border border-border/60 px-3 py-1.5">
                <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} />
                <span className="text-[12px] text-foreground">自动刷新</span>
              </div>

              <Select value={String(refreshInterval)} onValueChange={value => setRefreshInterval(parseInt(value, 10))}>
                <SelectTrigger className="h-8 w-[110px] text-[12px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AUTO_REFRESH_OPTIONS.map(option => (
                    <SelectItem key={option.value} value={String(option.value)}>{option.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <Button variant="outline" className="h-8" onClick={() => setScheduleOpen(true)} disabled={!agent || savingSchedule}>
                <Clock3 className="w-3.5 h-3.5" />
                {agent ? formatSchedule(agent.schedule) : '后台调度'}
              </Button>

              <Button variant={agent?.enabled ? 'secondary' : 'default'} className="h-8" onClick={toggleAgentEnabled} disabled={!agent || updatingAgent}>
                <RadioTower className="w-3.5 h-3.5" />
                {agent?.enabled ? '停用后台调度' : '启用后台调度'}
              </Button>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
              <span>最近更新时间: <span className="font-mono text-foreground/90">{formatDateTime(latestUpdate) || '--'}</span></span>
              <span className="opacity-50">|</span>
              <span>调度状态: <span className="text-foreground/90">{agent?.enabled ? '已启用' : '未启用'}</span></span>
            </div>
          </div>
        </div>
      </div>

      <div className="card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={viewMode} onValueChange={value => setViewMode(value as 'all' | 'related')}>
            <SelectTrigger className="h-8 w-[150px] text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部文章</SelectItem>
              <SelectItem value="related">仅自选相关</SelectItem>
            </SelectContent>
          </Select>

          <Select value={language} onValueChange={setLanguage}>
            <SelectTrigger className="h-8 w-[120px] text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部语言</SelectItem>
              <SelectItem value="zh">中文</SelectItem>
              <SelectItem value="en">英文</SelectItem>
            </SelectContent>
          </Select>

          <Select value={source} onValueChange={setSource}>
            <SelectTrigger className="h-8 w-[180px] text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部来源</SelectItem>
              {sourceOptions.map(option => (
                <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={String(hours)} onValueChange={value => setHours(parseInt(value, 10))}>
            <SelectTrigger className="h-8 w-[120px] text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {HOURS_OPTIONS.map(option => (
                <SelectItem key={option.value} value={String(option.value)}>{option.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Button variant="ghost" className="h-8 ml-auto text-[12px]" onClick={() => navigate('/history?agent_name=news_digest&kind=workflow')}>
            查看历史
          </Button>
        </div>
      </div>

      {error ? (
        <div className="rounded-xl border border-rose-500/20 bg-rose-500/10 p-4 text-[13px] text-rose-500">
          {error}
        </div>
      ) : null}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <div className="xl:col-span-2 space-y-4">
          <div className="card p-5">
            <div className="flex items-center gap-2">
              <Sparkles className="w-4 h-4 text-primary" />
              <h2 className="text-[15px] font-semibold text-foreground">AI 摘要</h2>
            </div>

            <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="rounded-xl bg-accent/20 p-3">
                <div className="text-[11px] text-muted-foreground">总文章</div>
                <div className="mt-1 text-[20px] font-semibold text-foreground">{totalArticles}</div>
              </div>
              <div className="rounded-xl bg-accent/20 p-3">
                <div className="text-[11px] text-muted-foreground">关联文章</div>
                <div className="mt-1 text-[20px] font-semibold text-foreground">{relatedArticles}</div>
              </div>
              <div className="rounded-xl bg-accent/20 p-3">
                <div className="text-[11px] text-muted-foreground">启用来源</div>
                <div className="mt-1 text-[20px] font-semibold text-foreground">{enabledSources}</div>
              </div>
              <div className="rounded-xl bg-accent/20 p-3">
                <div className="text-[11px] text-muted-foreground">情绪</div>
                <div className="mt-2">
                  <span className={`inline-flex items-center rounded-full border px-2 py-1 text-[12px] ${sentimentClass(runtime?.sentiment || 'neutral')}`}>
                    {sentimentLabel(runtime?.sentiment || 'neutral')}
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-4 space-y-4">
              <div>
                <div className="text-[12px] text-muted-foreground mb-2">总览</div>
                <div className="text-[14px] leading-7 text-foreground">{runtime?.summary || '暂无摘要'}</div>
              </div>

              <div>
                <div className="text-[12px] text-muted-foreground mb-2">主题</div>
                <div className="flex flex-wrap gap-2">
                  {(runtime?.topics || []).length > 0 ? runtime?.topics.map(topic => (
                    <span key={topic.name} className="px-2.5 py-1 rounded-full bg-primary/10 text-primary text-[12px]">
                      {topic.name}
                    </span>
                  )) : <span className="text-[13px] text-muted-foreground">暂无主题</span>}
                </div>
              </div>

              <div>
                <div className="text-[12px] text-muted-foreground mb-2">来源覆盖</div>
                <div className="flex flex-wrap gap-2 text-[12px] text-muted-foreground">
                  <span className="px-2 py-1 rounded-full bg-accent/30">
                    语言：{coverageLanguages.length > 0 ? coverageLanguages.join(' / ') : '暂无'}
                  </span>
                  <span className="px-2 py-1 rounded-full bg-accent/30">
                    已配置来源：{configuredSources}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div className="card p-5">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Languages className="w-4 h-4 text-primary" />
                <h2 className="text-[15px] font-semibold text-foreground">最新分析报告</h2>
              </div>
              <Button variant="ghost" className="h-8 text-[12px]" onClick={() => navigate('/history?agent_name=news_digest&kind=workflow')}>
                查看完整历史
              </Button>
            </div>

            {runtime?.analysis?.content ? (
              <div className="mt-4 prose prose-sm dark:prose-invert max-w-none max-h-[420px] overflow-y-auto">
                <ReactMarkdown>{runtime.analysis.content}</ReactMarkdown>
              </div>
            ) : (
              <div className="mt-4 text-[13px] text-muted-foreground">还没有生成历史分析报告，可以先执行一次手动刷新。</div>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="card p-5">
            <div className="text-[15px] font-semibold text-foreground">自选/持仓关联</div>
            <div className="mt-3 space-y-3 max-h-[520px] overflow-y-auto">
              {(runtime?.related_articles || []).length > 0 ? runtime?.related_articles.map(article => (
                <ArticleCard key={`related-${article.id}`} article={article} />
              )) : (
                <div className="text-[13px] text-muted-foreground">当前筛选条件下暂无关联文章。</div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="card p-5">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[15px] font-semibold text-foreground">全市场文章列表</div>
          <div className="text-[12px] text-muted-foreground">
            当前显示 {(runtime?.articles || []).length} 篇
          </div>
        </div>
        <div className="mt-4 grid grid-cols-1 xl:grid-cols-2 gap-4">
          {(runtime?.articles || []).length > 0 ? runtime?.articles.map(article => (
            <ArticleCard key={article.id} article={article} />
          )) : (
            <div className="text-[13px] text-muted-foreground">当前筛选条件下暂无文章。</div>
          )}
        </div>
      </div>

      <div className="card p-5">
        <div className="text-[15px] font-semibold text-foreground">来源状态</div>
        <div className="mt-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {(runtime?.source_statuses || []).map(sourceStatus => (
            <div key={sourceStatus.provider} className="rounded-xl border border-border/50 bg-accent/20 p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="text-[14px] font-semibold text-foreground">{sourceStatus.source_name}</div>
                <span className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] ${sourceStatusClass(sourceStatus.status)}`}>
                  {sourceStatus.status}
                </span>
              </div>
              <div className="mt-2 text-[12px] text-muted-foreground">
                <div>启用状态：{sourceStatus.enabled ? '已启用' : '已禁用'}</div>
                <div>最近成功：{formatDateTime(sourceStatus.last_success_at) || '--'}</div>
                <div>最近尝试：{formatDateTime(sourceStatus.last_attempt_at) || '--'}</div>
                <div>最近抓取：{sourceStatus.article_count} 篇</div>
                {sourceStatus.last_error ? (
                  <div className="mt-2 rounded-lg bg-rose-500/10 px-2 py-1 text-rose-500">
                    {sourceStatus.last_error}
                  </div>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      </div>

      <ScheduleEditorDialog
        open={scheduleOpen}
        onOpenChange={setScheduleOpen}
        title="后台调度策略"
        description="复用 news_digest 工作流的后台调度配置"
        schedule={agent?.schedule || ''}
        onSave={saveSchedule}
      />
    </div>
  )
}
