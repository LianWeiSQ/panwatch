import { NavLink, useLocation } from 'react-router-dom'
import {
  Moon, Sun, TrendingUp, Bot, ScrollText, Settings, List, Database, Clock,
  LayoutDashboard, LogOut, Github, BellRing, Sparkles, Activity, Newspaper,
  ChevronsLeft, ChevronsRight
} from 'lucide-react'
import { isAuthenticated } from '@panwatch/api'
import { cn } from '@/lib/utils'

export const navItems = [
  { to: '/', icon: LayoutDashboard, label: '首页' },
  { to: '/portfolio', icon: List, label: '持仓' },
  { to: '/opportunities', icon: Sparkles, label: '机会' },
  { to: '/alerts', icon: BellRing, label: '提醒' },
  { to: '/paper-trading', icon: Activity, label: '模拟盘' },
  { to: '/agents', icon: Bot, label: 'Agent' },
  { to: '/history', icon: Clock, label: '历史' },
  { to: '/news-analysis', icon: Newspaper, label: '新闻分析' },
  { to: '/datasources', icon: Database, label: '数据源' },
  { to: '/settings', icon: Settings, label: '设置' },
]

interface SidebarProps {
  version: string
  theme: 'light' | 'dark'
  collapsed: boolean
  onToggleTheme: () => void
  onToggleCollapse: () => void
  onOpenLogs: () => void
  onLogout: () => void
}

export default function Sidebar({
  version, theme, collapsed, onToggleTheme, onToggleCollapse, onOpenLogs, onLogout
}: SidebarProps) {
  const location = useLocation()
  const repoUrl = 'https://github.com/LianWeiSQ/panwatch'

  return (
    <aside
      className={cn(
        'fixed left-0 top-0 h-full z-40 hidden md:flex flex-col',
        'bg-card border-r border-border/60',
        'transition-[width] duration-300 ease-in-out overflow-hidden',
        collapsed ? 'w-16' : 'w-60'
      )}
    >
      {/* Logo */}
      <div className={cn('px-3 pt-4 pb-2', collapsed ? 'flex justify-center' : '')}>
        <NavLink to="/" className="flex items-center gap-2.5 group">
          <div className="w-8 h-8 shrink-0 rounded-2xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm">
            <TrendingUp className="w-4 h-4 text-white" />
          </div>
          {!collapsed && (
            <>
              <span className="text-[15px] font-bold text-foreground">PanWatch</span>
              {version && <span className="text-[11px] text-muted-foreground/60 font-normal">v{version}</span>}
            </>
          )}
        </NavLink>
      </div>

      {/* Nav Items */}
      <nav className="flex-1 overflow-y-auto scrollbar scrollbar-none px-2 py-2 flex flex-col gap-0.5">
        {navItems.map(({ to, icon: Icon, label }) => {
          const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
          return (
            <NavLink
              key={to}
              to={to}
              title={collapsed ? label : undefined}
              className={cn(
                'relative rounded-xl transition-all',
                collapsed
                  ? 'flex items-center justify-center px-0 py-2.5'
                  : 'flex items-center gap-3 px-3 py-2.5'
              )}
            >
              {isActive && (
                <span
                  className="absolute inset-0 rounded-xl bg-[linear-gradient(135deg,hsl(var(--primary)/0.14),hsl(var(--primary)/0.04),hsl(var(--success)/0.06))] ring-1 ring-primary/20 shadow-[0_8px_24px_-18px_hsl(var(--primary)/0.55)]"
                />
              )}
              <span
                className={cn(
                  'relative flex items-center',
                  collapsed ? '' : 'gap-3',
                  isActive
                    ? 'text-foreground'
                    : 'text-muted-foreground hover:text-foreground'
                )}
              >
                <Icon className={cn('w-4 h-4 shrink-0', isActive ? 'text-primary' : '')} />
                {!collapsed && (
                  <span className="text-[13px] font-medium truncate">{label}</span>
                )}
              </span>
            </NavLink>
          )
        })}
      </nav>

      {/* Utility Buttons */}
      <div className={cn(
        'px-2 pb-2 flex flex-col gap-0.5 border-t border-border/40 pt-2',
        collapsed ? 'items-center' : ''
      )}>
        <button
          onClick={() => window.open(repoUrl, '_blank', 'noopener,noreferrer')}
          className={cn(
            'rounded-xl flex items-center text-muted-foreground hover:text-foreground hover:bg-accent transition-all',
            collapsed ? 'w-10 h-10 justify-center' : 'w-full gap-2 px-3 py-2'
          )}
          title="GitHub 项目"
        >
          <Github className="w-4 h-4 shrink-0" />
          {!collapsed && <span className="text-[12px]">GitHub</span>}
        </button>
        <button
          onClick={onOpenLogs}
          className={cn(
            'rounded-xl flex items-center text-muted-foreground hover:text-foreground hover:bg-accent transition-all',
            collapsed ? 'w-10 h-10 justify-center' : 'w-full gap-2 px-3 py-2'
          )}
          title="查看日志"
        >
          <ScrollText className="w-4 h-4 shrink-0" />
          {!collapsed && <span className="text-[12px]">日志</span>}
        </button>
        <button
          onClick={onToggleTheme}
          className={cn(
            'rounded-xl flex items-center text-muted-foreground hover:text-foreground hover:bg-accent transition-all',
            collapsed ? 'w-10 h-10 justify-center' : 'w-full gap-2 px-3 py-2'
          )}
          title={theme === 'dark' ? '切换到亮色' : '切换到暗色'}
        >
          {theme === 'dark' ? <Sun className="w-4 h-4 shrink-0" /> : <Moon className="w-4 h-4 shrink-0" />}
          {!collapsed && <span className="text-[12px]">{theme === 'dark' ? '亮色' : '暗色'}</span>}
        </button>
        {isAuthenticated() && (
          <button
            onClick={onLogout}
            className={cn(
              'rounded-xl flex items-center text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-all',
              collapsed ? 'w-10 h-10 justify-center' : 'w-full gap-2 px-3 py-2'
            )}
            title="退出登录"
          >
            <LogOut className="w-4 h-4 shrink-0" />
            {!collapsed && <span className="text-[12px]">退出</span>}
          </button>
        )}
      </div>

      {/* Collapse Toggle */}
      <div className={cn('px-2 pb-3', collapsed ? 'flex justify-center' : '')}>
        <button
          onClick={onToggleCollapse}
          className={cn(
            'rounded-xl flex items-center text-muted-foreground hover:text-foreground hover:bg-accent transition-all',
            collapsed ? 'w-10 h-10 justify-center' : 'w-full gap-2 px-3 py-2'
          )}
          title={collapsed ? '展开侧边栏' : '折叠侧边栏'}
        >
          {collapsed ? <ChevronsRight className="w-4 h-4" /> : <ChevronsLeft className="w-4 h-4" />}
          {!collapsed && <span className="text-[12px]">折叠</span>}
        </button>
      </div>
    </aside>
  )
}
