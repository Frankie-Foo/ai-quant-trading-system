const crypto = require('node:crypto')
const path = require('node:path')
const { spawn, spawnSync } = require('node:child_process')

function validateRuntimeHandshake(payload) {
  if (
    !payload
    || payload.schema_version !== 'macos_local_research_handshake.v1'
    || payload.local_execution !== true
    || payload.orders_authorized !== false
  ) {
    throw new Error('local runtime handshake is not non-executable')
  }
  let parsed
  try {
    parsed = new URL(payload.url)
  } catch {
    throw new Error('local runtime handshake URL is invalid')
  }
  if (
    parsed.protocol !== 'http:'
    || !['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('local runtime must use an authenticated loopback URL')
  }
  return {
    url: parsed.href.replace(/\/+$/, ''),
    localExecution: true,
  }
}

function createLocalRuntimeClient({
  fetchImpl = fetch,
  timeoutMs = 30_000,
} = {}) {
  let connection = null

  async function request(route, { method = 'GET', body } = {}) {
    if (!connection) throw new Error('本地研究内核尚未连接')
    const headers = {
        Accept: 'application/json',
        Authorization: `Bearer ${connection.token}`,
        'Cache-Control': 'no-store',
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const response = await fetchImpl(`${connection.url}${route}`, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    })
    let payload
    try {
      payload = await response.json()
    } catch {
      throw new Error(`本地研究内核返回了无效响应（HTTP ${response.status}）`)
    }
    if (!response.ok) {
      const safeCode = typeof payload?.error_code === 'string'
        && /^[a-z0-9_]{3,80}$/.test(payload.error_code)
        ? payload.error_code
        : null
      if (safeCode) throw new Error(safeCode)
      throw new Error(`本地研究内核不可用（HTTP ${response.status}）`)
    }
    return payload
  }

  return {
    connect({ url, token }) {
      const handshake = validateRuntimeHandshake({
        schema_version: 'macos_local_research_handshake.v1',
        url,
        local_execution: true,
        orders_authorized: false,
      })
      const bearer = String(token || '').trim()
      if (bearer.length < 24) throw new Error('本地研究内核令牌无效')
      connection = { ...handshake, token: bearer }
    },

    async status() {
      const payload = await request('/v1/health')
      if (
        payload?.schema_version !== 'macos_local_research_runtime.v1'
        || payload.local_execution !== true
        || payload.orders_authorized !== false
      ) {
        throw new Error('本地研究内核状态无效')
      }
      return payload
    },

    async fetchDesk() {
      const payload = await request('/v1/desk')
      if (
        payload?.schema_version !== 'trading_desk_evidence.v1'
        || payload.stage !== 'research_only'
        || payload.orders_authorized !== false
        || payload.runtime?.schema_version !== 'macos_local_research_runtime.v1'
        || payload.runtime?.local_execution !== true
        || !payload.selection
        || !payload.review
        || !Array.isArray(payload.jobs)
        || !Array.isArray(payload.agents)
      ) {
        throw new Error('本地研究内核没有提供可信的 local non-executable 证据')
      }
      return payload
    },

    runDue: () => request('/v1/run-due', { method: 'POST' }),
    workflowStatus: () => request('/v1/workflows'),
    startWorkflow: (action, tradeDate) => request(
      `/v1/workflows/${encodeURIComponent(action)}?trade_date=${encodeURIComponent(tradeDate)}`,
      { method: 'POST' },
    ),
    startMonitor: (tradeDate) => request(
      `/v1/monitor/start?trade_date=${encodeURIComponent(tradeDate)}`,
      { method: 'POST' },
    ),
    stopMonitor: () => request('/v1/monitor/stop', { method: 'POST' }),
    async executionSnapshot() {
      const payload = await request('/v1/execution')
      if (
        !['ibkr.execution.v1', 'execution_desk.v1'].includes(
          payload?.schema_version,
        )
        || payload?.mode !== 'live'
        || payload?.port !== 4001
      ) {
        throw new Error('IBKR live execution status is invalid')
      }
      return payload
    },
    async executionCommand(command) {
      if (!command || typeof command !== 'object' || Array.isArray(command)) {
        throw new Error('IBKR live execution command is invalid')
      }
      return request('/v1/execution/commands', {
        method: 'POST',
        body: command,
      })
    },
    async paperAutopilotSnapshot() {
      const payload = await request('/v1/paper-autopilot')
      if (
        payload?.schema_version !== 'ibkr.paper_autopilot.v1'
        || payload?.mode !== 'paper'
        || payload?.port !== 4002
      ) {
        throw new Error('IBKR Paper autopilot status is invalid')
      }
      return payload
    },
    async paperAutopilotCommand(command) {
      if (!command || typeof command !== 'object' || Array.isArray(command)) {
        throw new Error('IBKR Paper autopilot command is invalid')
      }
      return request('/v1/paper-autopilot/commands', {
        method: 'POST',
        body: command,
      })
    },
  }
}

function runtimeLaunch({
  app,
  projectRoot,
  token,
  marketData = {},
  platform = process.platform,
  resourcesPath = process.resourcesPath,
}) {
  const userData = app.getPath('userData')
  const marketDataKey = String(marketData.key || '').trim()
  const marketDataSecret = String(marketData.secret || '').trim()
  const massiveApiKey = String(marketData.massiveApiKey || '').trim()
  const secUserAgent = String(marketData.secUserAgent || '').trim()
  const realtimeConfigured = Boolean(marketDataKey && marketDataSecret)
  const standaloneConfigured = Boolean(
    realtimeConfigured && massiveApiKey && secUserAgent,
  )
  const ibkr = marketData.ibkr && typeof marketData.ibkr === 'object'
    ? marketData.ibkr
    : {}
  const ibkrHost = String(ibkr.host || '').trim()
  const ibkrClientId = Number(ibkr.clientId)
  const ibkrLiveAccount = String(ibkr.liveAccount || '').trim().toUpperCase()
  const ibkrMaxOrderNotional = Number(ibkr.maxOrderNotional)
  const ibkrConfigured = Boolean(
    ibkrHost
    && Number.isInteger(ibkrClientId)
    && ibkrClientId >= 0
    && Number.isFinite(ibkrMaxOrderNotional)
    && ibkrMaxOrderNotional > 0
    && (!ibkrLiveAccount || /^[A-Z0-9-]{4,32}$/.test(ibkrLiveAccount))
  )
  const ibkrPaper = ibkr.paper && typeof ibkr.paper === 'object'
    ? ibkr.paper
    : {}
  const ibkrPaperHost = String(ibkrPaper.host || '').trim()
  const ibkrPaperClientId = Number(ibkrPaper.clientId)
  const ibkrPaperAccount = String(ibkrPaper.paperAccount || '').trim().toUpperCase()
  const ibkrPaperConfigured = Boolean(
    ibkrPaperHost
    && ibkrPaperHost.length <= 253
    && !/[\s/:]/.test(ibkrPaperHost)
    && Number.isInteger(ibkrPaperClientId)
    && ibkrPaperClientId >= 0
    && /^DU[A-Z0-9-]{4,30}$/.test(ibkrPaperAccount)
  )
  const sharedArgs = [
    '--host',
    '127.0.0.1',
    '--port',
    '0',
    '--data-root',
    path.join(userData, 'research-data'),
    '--runs-root',
    path.join(userData, 'research-runs'),
    '--provider-id',
    standaloneConfigured
      ? 'standalone'
      : realtimeConfigured ? 'alpaca_proxy' : 'unconfigured',
  ]
  if (['win32', 'darwin'].includes(platform)) {
    const bootstrapArchive = app.isPackaged
      ? resourcesPath
        ? path.join(resourcesPath, 'bootstrap', 'research-bootstrap.zip')
        : null
      : path.join(
        projectRoot,
        'client',
        'build',
        'bootstrap',
        'research-bootstrap.zip',
      )
    if (bootstrapArchive) {
      sharedArgs.push('--bootstrap-archive', bootstrapArchive)
    }
  }
  const marketDataEnv = realtimeConfigured
    ? {
        ALPACA_PROXY_KEY: marketDataKey,
        ALPACA_PROXY_SECRET: marketDataSecret,
        ...(standaloneConfigured
          ? {
              MASSIVE_API_KEY: massiveApiKey,
              SEC_USER_AGENT: secUserAgent,
              DESKTOP_MARKET_DATA_PROVIDER: 'alpaca_proxy_rest',
            }
          : {}),
      }
    : {}
  const executionEnv = ibkrConfigured
    ? {
        IBKR_HOST: ibkrHost,
        IBKR_CLIENT_ID: String(ibkrClientId),
        IBKR_MAX_ORDER_NOTIONAL: String(ibkrMaxOrderNotional),
        ...(ibkrLiveAccount
          ? { IBKR_LIVE_ACCOUNT: ibkrLiveAccount }
          : {}),
      }
    : {}
  const paperExecutionEnv = ibkrPaperConfigured
    ? {
        IBKR_PAPER_HOST: ibkrPaperHost,
        IBKR_PAPER_CLIENT_ID: String(ibkrPaperClientId),
        IBKR_PAPER_ACCOUNT: ibkrPaperAccount,
      }
    : {}
  if (app.isPackaged) {
    if (!resourcesPath) throw new Error('打包运行时目录不可用')
    const runtimeBinary = platform === 'win32'
      ? 'windows-research-runtime.exe'
      : 'macos-research-runtime'
    return {
      command: path.join(
        resourcesPath,
        'runtime',
        runtimeBinary,
      ),
      args: sharedArgs,
      cwd: userData,
      token,
      marketDataEnv,
      executionEnv: { ...executionEnv, ...paperExecutionEnv },
    }
  }
  const python = process.platform === 'win32'
    ? path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(projectRoot, '.venv', 'bin', 'python')
  return {
    command: python,
    args: ['-m', 'scripts.serve_macos_research_runtime', ...sharedArgs],
    cwd: projectRoot,
    token,
    marketDataEnv,
    executionEnv: { ...executionEnv, ...paperExecutionEnv },
  }
}

function taskkill(spawnSyncImpl, args) {
  try {
    return spawnSyncImpl('taskkill.exe', args, {
      windowsHide: true,
      stdio: 'ignore',
      timeout: 5_000,
    })
  } catch {
    return { status: null }
  }
}

function cleanupOrphanedWindowsRuntimes({
  platform = process.platform,
  packaged = false,
  spawnSyncImpl = spawnSync,
} = {}) {
  if (platform !== 'win32' || !packaged) return false
  const result = taskkill(spawnSyncImpl, [
    '/IM',
    'windows-research-runtime.exe',
    '/T',
    '/F',
  ])
  return result?.status === 0
}

function terminateRuntimeProcess(child, {
  platform = process.platform,
  spawnSyncImpl = spawnSync,
} = {}) {
  if (!child) return
  if (platform === 'win32' && Number.isInteger(child.pid)) {
    const result = taskkill(spawnSyncImpl, [
      '/PID',
      String(child.pid),
      '/T',
      '/F',
    ])
    if (result?.status === 0) return
  }
  if (!child.killed) child.kill()
}
function createLocalRuntimeProcess({
  app,
  projectRoot,
  spawnImpl = spawn,
  spawnSyncImpl = spawnSync,
  platform = process.platform,
  client = createLocalRuntimeClient(),
  startupTimeoutMs = 45_000,
  marketData = {},
}) {
  let child = null

  return {
    client,

    async start() {
      if (child) return client.status()
      cleanupOrphanedWindowsRuntimes({
        platform,
        packaged: app.isPackaged,
        spawnSyncImpl,
      })
      const token = crypto.randomBytes(32).toString('base64url')
      const launch = runtimeLaunch({
        app,
        projectRoot,
        token,
        marketData,
        platform,
      })
      child = spawnImpl(launch.command, launch.args, {
        cwd: launch.cwd,
        env: {
          ...process.env,
          ...launch.marketDataEnv,
          ...launch.executionEnv,
          MACOS_RESEARCH_RUNTIME_TOKEN: token,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
      const handshake = await new Promise((resolve, reject) => {
        let stdout = ''
        let stderr = ''
        const timer = setTimeout(() => {
          reject(new Error('本地研究内核启动超时'))
        }, startupTimeoutMs)
        const fail = (error) => {
          clearTimeout(timer)
          reject(error)
        }
        child.once('error', fail)
        child.once('exit', (code) => {
          fail(new Error(`本地研究内核提前退出（${code ?? 'unknown'}）`))
        })
        child.stderr.on('data', (chunk) => {
          if (stderr.length < 2_000) stderr += chunk.toString('utf8')
        })
        child.stdout.on('data', (chunk) => {
          stdout += chunk.toString('utf8')
          const newline = stdout.indexOf('\n')
          if (newline < 0) return
          clearTimeout(timer)
          try {
            const parsed = JSON.parse(stdout.slice(0, newline))
            resolve(validateRuntimeHandshake(parsed))
          } catch {
            const detail = stderr.trim() ? '，运行时报告启动错误' : ''
            reject(new Error(`本地研究内核握手失败${detail}`))
          }
        })
      })
      client.connect({ url: handshake.url, token })
      return client.status()
    },

    stop() {
      terminateRuntimeProcess(child, { platform, spawnSyncImpl })
      child = null
    },
  }
}

module.exports = {
  cleanupOrphanedWindowsRuntimes,
  createLocalRuntimeClient,
  createLocalRuntimeProcess,
  runtimeLaunch,
  terminateRuntimeProcess,
  validateRuntimeHandshake,
}
