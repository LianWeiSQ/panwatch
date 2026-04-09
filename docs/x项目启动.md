# PanWatch 前后端启动说明

适用环境：`Windows + PowerShell`

本文说明如何在本地分别启动 PanWatch 的后端和前端，并给出首次安装、日常启动、验证方式和常见问题。

## 1. 环境要求

- Python `3.10+`
- Node.js `18+`
- `pnpm`

仓库根目录：`C:\coding\vibe\PanWatch`

## 2. 首次安装

### 2.1 安装后端依赖

在仓库根目录打开 PowerShell：

```powershell
cd C:\coding\vibe\PanWatch
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2.2 安装前端依赖

```powershell
cd C:\coding\vibe\PanWatch\frontend
corepack enable
corepack pnpm install
```

## 3. 启动后端

后端默认监听：`http://127.0.0.1:8000`

### 方式 A：直接命令启动

在仓库根目录执行：

```powershell
cd C:\coding\vibe\PanWatch
$env:PLAYWRIGHT_SKIP_BROWSER_INSTALL="1"
$env:DATA_DIR="$PWD\data"
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
```

说明：

- `PLAYWRIGHT_SKIP_BROWSER_INSTALL=1` 表示跳过 Chromium 首次安装，适合本地先把服务拉起来。
- `DATA_DIR` 指向本地数据目录，默认写入 `data\`。

### 方式 B：使用现成脚本启动

仓库里已经有一个后端启动脚本：

- [scripts/start-local.cmd](C:/coding/vibe/PanWatch/scripts/start-local.cmd)

执行方式：

```powershell
cd C:\coding\vibe\PanWatch
cmd /c scripts\start-local.cmd
```

这个脚本会：

- 切到仓库根目录
- 检查 `.venv`
- 自动创建 `data` 和 `logs` 目录
- 设置 `PLAYWRIGHT_SKIP_BROWSER_INSTALL=1`
- 设置 `DATA_DIR`
- 以 `uvicorn server:app --port 8000` 启动后端
- 把日志写到：
  - `logs\panwatch.out.log`
  - `logs\panwatch.err.log`

## 4. 启动前端

前端开发服务默认监听：`http://localhost:5173`

在新开一个 PowerShell 窗口中执行：

```powershell
cd C:\coding\vibe\PanWatch\frontend
corepack pnpm dev
```

说明：

- 前端是 Vite 开发服务器。
- 开发模式下，前端会把 API 请求代理到后端。
- 所以前端启动前，最好先确认后端已经在 `8000` 端口运行。

## 5. 启动顺序

推荐顺序：

1. 先启动后端
2. 再启动前端
3. 浏览器访问前端地址

访问地址：

- 前端开发页：`http://localhost:5173`
- 后端服务：`http://127.0.0.1:8000`

## 6. 启动后如何验证

### 6.1 验证后端是否正常

浏览器访问：

- `http://127.0.0.1:8000/api/health`

如果返回 `ok`，说明后端正常。

也可以在 PowerShell 执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

### 6.2 验证前端是否正常

浏览器访问：

- `http://localhost:5173`

如果页面能打开登录页或主界面，说明前端正常。

## 7. 日常启动最简流程

如果依赖已经装好，日常只需要两个终端。

终端 1，启动后端：

```powershell
cd C:\coding\vibe\PanWatch
$env:PLAYWRIGHT_SKIP_BROWSER_INSTALL="1"
$env:DATA_DIR="$PWD\data"
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
```

终端 2，启动前端：

```powershell
cd C:\coding\vibe\PanWatch\frontend
corepack pnpm dev
```

## 8. 常见问题

### 8.1 `.venv\Scripts\python.exe` 不存在

说明虚拟环境还没创建，先执行：

```powershell
cd C:\coding\vibe\PanWatch
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 8.2 `pnpm` 不可用

先执行：

```powershell
corepack enable
```

如果前端依赖还没安装，再执行：

```powershell
cd C:\coding\vibe\PanWatch\frontend
corepack pnpm install
```

### 8.3 8000 端口被占用

可以先查占用：

```powershell
netstat -ano | findstr :8000
```

然后结束对应进程，或者改成别的端口启动后端。

### 8.4 前端打开了，但接口报错

优先检查这几项：

- 后端是否已经启动
- 后端是否监听在 `8000`
- `http://127.0.0.1:8000/api/health` 是否正常

### 8.5 后端启动后看不到日志

如果你用的是脚本启动：

- 看 `logs\panwatch.out.log`
- 看 `logs\panwatch.err.log`

如果你用的是直接命令启动：

- 日志会直接输出在当前终端

## 9. 相关文件

- [README.md](C:/coding/vibe/PanWatch/README.md)
- [scripts/start-local.cmd](C:/coding/vibe/PanWatch/scripts/start-local.cmd)
- [server.py](C:/coding/vibe/PanWatch/server.py)
- [frontend/package.json](C:/coding/vibe/PanWatch/frontend/package.json)
