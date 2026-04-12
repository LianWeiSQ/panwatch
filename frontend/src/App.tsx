import { Suspense, lazy, useState, useEffect, useRef } from 'react'
import { Routes, Route, NavLink, useLocation, Navigate } from 'react-router-dom'
import { Moon, Sun, TrendingUp, MoreHorizontal, Github, ScrollText } from 'lucide-react'
import { useTheme } from '@/hooks/use-theme'
import { appApi, fetchAPI, isAuthenticated, logout } from '@panwatch/api'
import LogsModal from '@panwatch/biz-ui/components/logs-modal'
import AmbientBackground from '@panwatch/biz-ui/components/AmbientBackground'
import ChatWidget from '@/components/ChatWidget'
import Sidebar, { navItems } from '@/components/Sidebar'
import { useLocalStorage } from '@/lib/utils'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'

const DashboardPage = lazy(() => import('@/pages/Dashboard'))
const OpportunitiesPage = lazy(() => import('@/pages/Opportunities'))
const StocksPage = lazy(() => import('@/pages/Stocks'))
const AgentsPage = lazy(() => import('@/pages/Agents'))
const SettingsPage = lazy(() => import('@/pages/Settings'))
const DataSourcesPage = lazy(() => import('@/pages/DataSources'))
const HistoryPage = lazy(() => import('@/pages/History'))
const NewsAnalysisPage = lazy(() => import('@/pages/NewsAnalysis'))
const PriceAlertsPage = lazy(() => import('@/pages/PriceAlerts'))
const PaperTradingPage = lazy(() => import('@/pages/PaperTrading'))
const LoginPage = lazy(() => import('@/pages/Login'))

const mobilePrimaryNavItems = [navItems[0], navItems[1], navItems[2], navItems[3]]
const mobileMoreNavItems = [navItems[4], navItems[5], navItems[6], navItems[7], navItems[8], navItems[9]]

// 认证守卫组件
function RequireAuth({ children }: { children: React.ReactNode }) {
  const [authState, setAuthState] = useState<'checking' | 'authenticated' | 'unauthenticated'>('checking')
  const location = useLocation()

  useEffect(() => {
    // 检查本地 token
    if (isAuthenticated()) {
      setAuthState('authenticated')
      return
    }

    // 没有 token，需要去登录页（设置密码或登录）
    setAuthState('unauthenticated')
  }, [])

  if (authState === 'checking') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  if (authState === 'unauthenticated') {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return <>{children}</>
}

function App() {
  const { theme, toggleTheme } = useTheme()
  const location = useLocation()
  const [version, setVersion] = useState('')
  const [logsOpen, setLogsOpen] = useState(false)
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [upgradeInfo, setUpgradeInfo] = useState<{ latest: string; url: string } | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useLocalStorage<boolean>('panwatch-sidebar-collapsed', false)
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false)
  const checkedUpdateRef = useRef(false)
  const mobileMoreRef = useRef<HTMLDivElement | null>(null)
  const repoUrl = 'https://github.com/TNT-Likely/PanWatch'
  const routeFallback = (
    <div className="card min-h-[240px] flex items-center justify-center">
      <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
    </div>
  )

  useEffect(() => {
    appApi.version()
      .then(data => setVersion(data?.version || ''))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (checkedUpdateRef.current) return
    if (!isAuthenticated()) return
    const current = String(version || '').trim()
    if (!current || current === 'dev') return
    checkedUpdateRef.current = true

    fetchAPI<any>('/settings/update-check')
      .then((res) => {
        const latest = String(res?.latest_version || '').trim()
        const shouldOpen = !!res?.update_available && !!latest
        if (!shouldOpen) return
        const dismissed = localStorage.getItem('panwatch_upgrade_dismissed_version') || ''
        if (dismissed === latest) return
        setUpgradeInfo({ latest, url: String(res?.release_url || 'https://github.com/sunxiao0721/PanWatch/releases') })
        setUpgradeOpen(true)
      })
      .catch(() => {})
  }, [version])

  useEffect(() => {
    const onDocPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (mobileMoreOpen && mobileMoreRef.current && !mobileMoreRef.current.contains(t)) {
        setMobileMoreOpen(false)
      }
    }
    document.addEventListener('pointerdown', onDocPointerDown)
    return () => document.removeEventListener('pointerdown', onDocPointerDown)
  }, [mobileMoreOpen])

  useEffect(() => {
    setMobileMoreOpen(false)
  }, [location.pathname])

  // 登录页面不显示导航
  if (location.pathname === '/login') {
    return (
      <Suspense fallback={routeFallback}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
        </Routes>
      </Suspense>
    )
  }

  return (
    <RequireAuth>
    <div className="min-h-screen pb-16 md:pb-0 relative overflow-x-clip bg-background">
      <AmbientBackground />
      {/* Desktop Sidebar */}
      <Sidebar
        version={version}
        theme={theme}
        collapsed={sidebarCollapsed}
        onToggleTheme={toggleTheme}
        onToggleCollapse={() => setSidebarCollapsed(v => !v)}
        onOpenLogs={() => setLogsOpen(true)}
        onLogout={logout}
      />

      {/* Mobile Top Bar */}
      <div className="sticky top-0 z-50 px-4 pt-[max(0.75rem,env(safe-area-inset-top))] pb-2 md:hidden">
        <header className="card px-4">
          <div className="h-12 flex items-center justify-between">
            <NavLink to="/" className="flex items-center gap-2 group">
              <div className="w-7 h-7 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm">
                <TrendingUp className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="text-[14px] font-bold text-foreground">PanWatch</span>
              {version && <span className="text-[10px] text-muted-foreground/60 font-normal">v{version}</span>}
            </NavLink>
            <div className="flex items-center gap-1.5 px-1.5 py-1 rounded-2xl bg-accent/20 border border-border/40">
              <button
                onClick={() => window.open(repoUrl, '_blank', 'noopener,noreferrer')}
                className="w-8 h-8 rounded-xl flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-background/70 transition-all"
                title="GitHub 项目"
              >
                <Github className="w-4 h-4" />
              </button>
              <button
                onClick={() => setLogsOpen(true)}
                className="w-8 h-8 rounded-xl flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-background/70 transition-all"
                title="查看日志"
              >
                <ScrollText className="w-4 h-4" />
              </button>
              <button
                onClick={toggleTheme}
                className="w-8 h-8 rounded-xl flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-background/70 transition-all"
                title={theme === 'dark' ? '切换到亮色' : '切换到暗色'}
              >
                {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </header>
      </div>

      {/* Mobile Bottom Nav */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 md:hidden bg-card border-t border-border px-2 pb-[env(safe-area-inset-bottom)]" ref={mobileMoreRef}>
        <div className="flex items-center justify-around h-14">
          {mobilePrimaryNavItems.map(({ to, icon: Icon, label }) => {
            const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                className={`flex flex-col items-center justify-center gap-0.5 px-2 py-1.5 rounded-xl transition-all min-w-[56px] ${
                  isActive
                    ? 'text-primary bg-primary/8 ring-1 ring-primary/15'
                    : 'text-muted-foreground hover:bg-accent/30'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="text-[10px] font-medium">{label}</span>
              </NavLink>
            )
          })}
          <button
            onClick={() => setMobileMoreOpen(v => !v)}
            className={`flex flex-col items-center justify-center gap-0.5 px-2 py-1.5 rounded-xl transition-all min-w-[56px] ${
              mobileMoreNavItems.some(item => location.pathname.startsWith(item.to))
                ? 'text-primary bg-primary/8 ring-1 ring-primary/15'
                : 'text-muted-foreground hover:bg-accent/30'
            }`}
          >
            <MoreHorizontal className="w-5 h-5" />
            <span className="text-[10px] font-medium">更多</span>
          </button>
        </div>
        {mobileMoreOpen && (
          <div className="absolute bottom-[58px] right-2 w-40 rounded-xl border border-border/60 bg-card/95 backdrop-blur p-1.5 shadow-xl">
            {mobileMoreNavItems.map(({ to, icon: Icon, label }) => {
              const isActive = location.pathname.startsWith(to)
              return (
                <NavLink
                  key={to}
                  to={to}
                  onClick={() => setMobileMoreOpen(false)}
                  className={`flex items-center gap-2 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                    isActive ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {label}
                </NavLink>
              )
            })}
          </div>
        )}
      </nav>

      {/* Content */}
      <main className={`px-4 py-4 md:py-6 w-full transition-[margin-left] duration-300 ease-in-out ${sidebarCollapsed ? 'md:ml-16' : 'md:ml-60'}`}>
        <Suspense fallback={routeFallback}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/opportunities" element={<OpportunitiesPage />} />
            <Route path="/portfolio" element={<StocksPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/history" element={<HistoryPage />} />
            <Route path="/news-analysis" element={<NewsAnalysisPage />} />
            <Route path="/paper-trading" element={<PaperTradingPage />} />
            <Route path="/alerts" element={<PriceAlertsPage />} />
            <Route path="/datasources" element={<DataSourcesPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </Suspense>
      </main>
      <ChatWidget />
      <LogsModal open={logsOpen} onOpenChange={setLogsOpen} />
      <Dialog open={upgradeOpen} onOpenChange={setUpgradeOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>发现新版本</DialogTitle>
            <DialogDescription>
              当前版本 v{version}，可升级到 v{upgradeInfo?.latest}。
            </DialogDescription>
          </DialogHeader>
          <div className="text-[12px] text-muted-foreground">
            建议升级以获取最新功能和修复。
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                if (upgradeInfo?.latest) localStorage.setItem('panwatch_upgrade_dismissed_version', upgradeInfo.latest)
                setUpgradeOpen(false)
              }}
            >
              稍后提醒
            </Button>
            <Button
              onClick={() => {
                const url = upgradeInfo?.url || 'https://github.com/sunxiao0721/PanWatch/releases'
                window.open(url, '_blank', 'noopener,noreferrer')
              }}
            >
              去升级
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
    </RequireAuth>
  )
}

export default App
