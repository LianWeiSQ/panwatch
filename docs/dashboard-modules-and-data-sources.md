# Dashboard 模块与数据源分析

分析基准日期：`2026-04-09`

## 关键结论

当前 Dashboard 页面不是由后端单个 `/dashboard/overview` 聚合接口驱动，而是前端页面 [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx) 在初始化和刷新时并行调用多组 API 后，在页面层完成状态拼装与部分二次计算。

- 前端初始化入口：`loadIndices()`、`loadMarketStatus()`、`loadPortfolio()`、`loadWatchlist()`、`loadAIInsights()`、`loadDiscovery('boards')`、`loadDiscovery('stocks', { silent: true })`
- 代码位置：[frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
- 虽然后端已经提供 `/dashboard/overview`，前端 SDK 里也已经提供 `dashboardApi.overview()`，但当前 Dashboard 页面没有调用该接口。

## 页面数据流总览

| 页面模块 | 前端入口 | API | 后端实现 | 实际数据源 |
| --- | --- | --- | --- | --- |
| 顶部市场状态与刷新 | `loadMarketStatus()` / `handleRefresh()` / 自动刷新定时器 | `/stocks/markets/status`、`/quotes/batch` | `stocks.py`、`quotes.py` | 本地市场时段定义、腾讯行情 |
| 资产总览卡片 | `loadPortfolio()`、`refreshQuotes()`、`mergePortfolioQuotes()` | `/portfolio/summary?include_quotes=false`、`/quotes/batch` | `accounts.py`、`quotes.py` | 本地数据库、腾讯行情、新浪汇率 |
| 大盘指数 | `loadIndices()` | `/market/indices` | `market.py` | 腾讯行情 |
| 机会发现 | `loadDiscovery()`、`openBoard()` | `/discovery/stocks`、`/discovery/boards`、`/discovery/boards/{code}/stocks` | `discovery.py` | 东方财富，失败时回退本地 `MarketScanSnapshot` |
| 行动中心 | `scanAlerts()` | `/agents/intraday/scan` | `agents.py`、`intraday_monitor.py` | 本地自选/Agent 绑定、腾讯行情、K 线数据、AI 分析 |
| AI 宏观摘要 | `loadAIInsights()` | `/history` | `history.py` | 本地 `analysis_history` 历史结果 |

## 1. 顶部市场状态与刷新机制

### 页面模块

- 顶部状态条展示 A 股、港股、美股当前状态。
- 页面支持手动刷新。
- 页面支持“仅行情自动刷新”，自动刷新定时器只反复调用 `refreshQuotes()`，不会自动重新加载全部模块。

### 前端入口与渲染位置

- 入口函数：`loadMarketStatus()`、`handleRefresh()`、自动刷新 `useEffect`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `loadMarketStatus()`
  - `handleRefresh()`
  - 顶部状态条渲染

### 实际调用的 API

- `GET /stocks/markets/status`
- `POST /quotes/batch`（自动刷新和手动刷新时用于更新行情）

对应前端 SDK：

- [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)

### 后端实现

- 市场状态接口：`src/web/api/stocks.py`
- 行情批量接口：`src/web/api/quotes.py`

关键实现特征：

- `/stocks/markets/status` 遍历 `MARKETS` 常量，根据市场时区和交易时段判断 `trading / pre_market / break / after_hours / closed`。
- `/quotes/batch` 将按市场分组后的证券代码转换成腾讯格式，再调用 `_fetch_tencent_quotes()` 获取实时价格。

### 实际数据源

- 市场状态：本地静态市场时段定义 `MARKETS`
- 行情刷新：腾讯行情 `_fetch_tencent_quotes`

### 备注

- 这里的市场状态不依赖交易所官方节假日日历，只依赖本地定义的交易时间窗口。
- 所以它能正确区分“交易中 / 午间休市 / 盘前 / 已收盘”，但不能天然识别法定节假日或临时休市。

## 2. 资产总览卡片

### 页面模块

资产总览卡片包括：

- 总资产
- 总盈亏
- 持仓市值
- 可用资金
- 当日盈亏
- 最大拖累 / 涨幅

### 前端入口与渲染位置

- 入口函数：`loadPortfolio()`、`refreshQuotes()`、`mergePortfolioQuotes()`
- 辅助计算：`portfolioDayPnl`、`dayMovers`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `loadPortfolio()`
  - `refreshQuotes()`
  - `mergePortfolioQuotes()`
  - “Portfolio Summary Cards” 区块

### 实际调用的 API

- `GET /portfolio/summary?include_quotes=false`
- `POST /quotes/batch`
- `GET /stocks`（用于拼自选股和持仓统一报价列表）

对应前端 SDK：

- [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)

### 后端实现

- 账户与持仓汇总：`src/web/api/accounts.py`
- 批量行情：`src/web/api/quotes.py`

关键实现特征：

- `loadPortfolio()` 调用 `/portfolio/summary?include_quotes=false`，先只取账户、持仓、汇率和基础汇总。
- 页面随后自行调用 `/quotes/batch` 拉实时价，并在前端 `mergePortfolioQuotes()` 中重新计算资产、盈亏和市值。
- 当日盈亏、最大拖累 / 涨幅不是后端直接返回，而是前端根据实时价、涨跌幅、持仓数量和汇率再次推导。

### 实际数据源

- 账户、持仓、自选范围：本地数据库
- 实时行情：腾讯行情 `_fetch_tencent_quotes`
- 港股 / 美股换算汇率：新浪 `hq.sinajs.cn`

### 汇率链路说明

- 港股汇率：`fx_shkdcny`
- 美元汇率：`fx_susdcny`
- 两个汇率接口都带 1 小时缓存。
- 当新浪接口失败时，系统会回退到默认值：
  - `HKD_CNY = 0.92`
  - `USD_CNY = 7.25`

### 备注

- 当前 Dashboard 没有直接使用 `/portfolio/summary?include_quotes=true` 的全量估值结果，而是采取“后端给结构 + 前端补行情 + 前端重算”的方式。
- 因此该模块的数据准确性取决于两部分：
  - 本地数据库里的账户 / 持仓是否正确
  - 腾讯实时价和新浪汇率是否可用

## 3. 大盘指数

### 页面模块

- Dashboard 中部的“大盘指数”卡片组，展示 A 股、港股、美股主要指数的当前点位、涨跌幅和涨跌额。

### 前端入口与渲染位置

- 入口函数：`loadIndices()`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `loadIndices()`
  - “Market Indices” 区块

### 实际调用的 API

- `GET /market/indices`

对应前端 SDK：

- [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)

### 后端实现

- `src/web/api/market.py`

关键实现特征：

- 后端内置指数映射表，维护指数展示名、市场和腾讯使用的代码。
- 调用 `_fetch_tencent_quotes()` 一次性获取指数行情。
- 再根据预设映射关系将腾讯返回结构转换成前端所需结构。

### 实际数据源

- 腾讯行情 `_fetch_tencent_quotes`

### 备注

- 这里不是通过 `DataSources` 页里的“quote”配置动态决定数据源，而是直接在 `market.py` 中写死导入腾讯行情 helper。

## 4. 机会发现

### 页面模块

“机会发现”区块包含两类视图：

- 热门板块
- 热门股票

支持市场切换：

- A 股
- 港股
- 美股

支持榜单模式切换：

- 板块：涨幅榜 / 成交额榜
- 股票：`For You` / 成交额榜 / 涨幅榜

支持点击板块后查看板块成分股。

### 前端入口与渲染位置

- 入口函数：`loadDiscovery()`、`openBoard()`
- `For You` 排序：`personalizedHotStocks`、`visibleHotStocks`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `loadDiscovery()`
  - `openBoard()`
  - “Discover” 区块

### 实际调用的 API

- `GET /discovery/boards`
- `GET /discovery/stocks`
- `GET /discovery/boards/{board_code}/stocks`

对应前端 SDK：

- [frontend/packages/api/src/discovery.ts](../frontend/packages/api/src/discovery.ts)

### 后端实现

- `src/web/api/discovery.py`
- 采集器：`src/collectors/discovery_collector.py`

关键实现特征：

- 热门股票：优先通过 `EastMoneyDiscoveryCollector` 实时获取。
- 如果实时拉取失败，则回退到本地 `MarketScanSnapshot` 最新快照。
- 热门板块：
  - A 股优先拉东方财富真实板块数据。
  - 港股 / 美股没有真实板块榜时，会基于热门股票池构造 synthetic board。
- 板块成分股：
  - 若板块编码是合成板块，则从热门股票池里按规则再筛一遍。
  - 若是 A 股真实板块，则继续走东方财富采集器。

### 实际数据源

- 主链路：东方财富 `EastMoneyDiscoveryCollector`
- 回退链路：本地数据库 `MarketScanSnapshot`
- `For You` 额外使用的页面内信息：
  - 本地持仓
  - 本地自选股
  - 行动中心监控结果
  - 风格偏好

### 备注

- `For You` 不是新的外部数据源，而是前端把已有热点股票结果拿回来后，在页面内按“持仓 / 自选 / 监控信号 / 风格偏好”加权排序。
- 当前 Discovery 的稳定性显著依赖外网连通性和东方财富接口可用性，因此比腾讯主行情链路更脆弱。

## 5. 行动中心

### 页面模块

行动中心由两部分组成：

- 待处理信号
- AI 宏观摘要

其中“待处理信号”来自盘中扫描结果，不是静态卡片。

### 前端入口与渲染位置

- 入口函数：`scanAlerts()`
- 信号排序：`actionableSignals`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `scanAlerts()`
  - “Action Center” 区块

### 实际调用的 API

- `POST /agents/intraday/scan`

对应前端 SDK：

- [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)

### 后端实现

- 扫描接口：`src/web/api/agents.py`
- AI 分析：`src/agents/intraday_monitor.py`
- 行情采集：`src/collectors/akshare_collector.py`
- K 线摘要：`src/collectors/kline_collector.py`

关键实现特征：

- `scanAlerts()` 分两阶段执行：
  - 第一阶段：`analyze=false`，先做快速扫描并立即渲染。
  - 第二阶段：`analyze=true`，后台补充 AI 建议，再把结果合并回页面。
- 后端扫描时只处理绑定了 `intraday_monitor` 的股票。
- 实时价通过 `AkshareCollector.get_stock_data()` 获取，但其底层实际仍然调用 `_fetch_tencent_quotes()`。
- 每只股票还会额外拉取 K 线摘要，并在需要时交给 `IntradayMonitorAgent` 生成结构化建议。

### 实际数据源

- 股票池：本地数据库中已绑定 `intraday_monitor` 的自选股
- 实时行情：腾讯行情
- K 线摘要：K 线 collector
- AI 建议：`intraday_monitor` agent 分析结果

### 备注

- 这部分不只是“展示历史结果”，而是会实时发起盘中扫描。
- 当前实现对外部依赖比 AI 摘要更高，因为需要同时拉实时价、K 线并可能调用 AI 模型。

## 6. AI 宏观摘要

### 页面模块

Dashboard 右侧摘要卡片当前来自三类历史结果：

- 收盘复盘 `daily_report`
- 盘前分析 `premarket_outlook`
- 新闻速递 `news_digest`

### 前端入口与渲染位置

- 入口函数：`loadAIInsights()`
- 摘要卡片：`insightCards`
- 主要代码位置：
  - [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
  - `loadAIInsights()`
  - `insightCards`
  - “AI 宏观摘要”区块

### 实际调用的 API

- `GET /history?agent_name=daily_report&limit=1`
- `GET /history?agent_name=premarket_outlook&limit=1`
- `GET /history?agent_name=news_digest&kind=all&limit=1`

对应前端 SDK：

- [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)

### 后端实现

- `src/web/api/history.py`

关键实现特征：

- 页面只取最近一条历史分析记录。
- 页面展示的是已经生成好的历史内容，不会在打开 Dashboard 时重新触发 Agent 计算。
- 点击卡片后只是打开完整 Markdown 预览，再跳转到历史页面查看明细。

### 实际数据源

- 本地数据库 `analysis_history`

### 备注

- 这部分本质上是“历史结果预览”，不是实时分析面板。

## 当前 Dashboard 实际数据源清单

按当前代码实现，Dashboard 实际依赖的数据源可以归类如下。

### 1. 本地数据库

- 自选股：`stocks`
- 账户与持仓：`accounts`、`positions`
- AI 历史分析：`analysis_history`
- 发现类回退快照：`MarketScanSnapshot`

### 2. 腾讯行情

主链路包括：

- 大盘指数
- Dashboard 批量实时行情
- 持仓估值
- 盘中扫描实时价

相关代码直接导入并调用 `_fetch_tencent_quotes`：

- [src/web/api/market.py](../src/web/api/market.py)
- [src/web/api/quotes.py](../src/web/api/quotes.py)
- [src/web/api/accounts.py](../src/web/api/accounts.py)
- [src/web/api/stocks.py](../src/web/api/stocks.py)

### 3. 东方财富

主链路包括：

- 热门股票
- 热门板块
- 板块成分股

相关代码直接实例化 `EastMoneyDiscoveryCollector`：

- [src/web/api/discovery.py](../src/web/api/discovery.py)
- [src/collectors/discovery_collector.py](../src/collectors/discovery_collector.py)

### 4. 新浪汇率

主链路包括：

- `HKD_CNY`
- `USD_CNY`

实现位置：

- [src/web/api/accounts.py](../src/web/api/accounts.py)

特点：

- 带缓存
- 失败回退默认值

### 5. 本地静态市场时段

主链路包括：

- 顶部市场开闭状态判断

实现位置：

- [src/models/market.py](../src/models/market.py)
- [src/web/api/stocks.py](../src/web/api/stocks.py)

### 6. 历史分析结果

主链路包括：

- 收盘复盘
- 盘前分析
- 新闻速递

实现位置：

- [src/web/api/history.py](../src/web/api/history.py)

## 当前实现注意点 / 风险提示

### 1. `/dashboard/overview` 已存在，但当前 Dashboard 页未使用

- 后端已有 `/dashboard/overview` 聚合接口：[src/web/api/dashboard.py](../src/web/api/dashboard.py)
- 前端 SDK 已有 `dashboardApi.overview()`：[frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)
- 但当前页面 [frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx) 仍然采用多接口并行调用的方式，没有接入这个聚合接口。

### 2. `DataSources` 配置页并没有完全控制 Dashboard 的所有运行时链路

- Dashboard 的指数、批量行情、持仓估值等核心路径在代码中直接导入 `_fetch_tencent_quotes()`。
- Discovery 在代码中直接实例化 `EastMoneyDiscoveryCollector`。
- 这说明 `DataSources` 页面对 Dashboard 的运行时数据链路并不是完整的统一控制面。

### 3. 发现类数据比主行情链路更依赖外网和源站稳定性

- 腾讯主行情链路当前更稳定。
- Discovery 依赖东方财富发现页接口，网络波动、超时、源站结构变化都会直接影响结果。
- 虽然存在 `MarketScanSnapshot` 快照回退，但回退结果的实时性取决于本地最近一次成功快照时间。

### 4. 资产相关卡片受 FX 获取成功与否影响

- 港股 / 美股资产估值依赖新浪汇率接口。
- 如果汇率获取失败，系统会使用默认缓存值或默认常量值。
- 因此资产总览在极端情况下可能“方向正确但换算不够精确”。

### 5. 市场状态不是节假日感知模型

- 顶部市场状态目前依赖本地时区 + 交易时段，不是交易所日历。
- 因此会存在“时间段逻辑正确，但节假日状态不够准确”的风险。

## 相关代码索引

- 前端页面：[frontend/src/pages/Dashboard.tsx](../frontend/src/pages/Dashboard.tsx)
- 前端 API：
  - [frontend/packages/api/src/dashboard.ts](../frontend/packages/api/src/dashboard.ts)
  - [frontend/packages/api/src/discovery.ts](../frontend/packages/api/src/discovery.ts)
- 后端 API：
  - [src/web/api/dashboard.py](../src/web/api/dashboard.py)
  - [src/web/api/market.py](../src/web/api/market.py)
  - [src/web/api/stocks.py](../src/web/api/stocks.py)
  - [src/web/api/accounts.py](../src/web/api/accounts.py)
  - [src/web/api/quotes.py](../src/web/api/quotes.py)
  - [src/web/api/discovery.py](../src/web/api/discovery.py)
  - [src/web/api/agents.py](../src/web/api/agents.py)
  - [src/web/api/history.py](../src/web/api/history.py)
- 采集器与模型：
  - [src/collectors/akshare_collector.py](../src/collectors/akshare_collector.py)
  - [src/collectors/discovery_collector.py](../src/collectors/discovery_collector.py)
  - [src/collectors/kline_collector.py](../src/collectors/kline_collector.py)
  - [src/models/market.py](../src/models/market.py)
  - [src/agents/intraday_monitor.py](../src/agents/intraday_monitor.py)
