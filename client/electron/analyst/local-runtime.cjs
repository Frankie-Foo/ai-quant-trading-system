const crypto = require('node:crypto')
const path = require('node:path')
const { spawn } = require('node:child_process')

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

  async function request(route, { method = 'GET' } = {}) {
    if (!connection) throw new Error('本地研究内核尚未连接')
    const response = await fetchImpl(`${connection.url}${route}`, {
      method,
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${connection.token}`,
        'Cache-Control': 'no-store',
      },
      signal: AbortSignal.timeout(timeoutMs),
    })
    let payload
    try {
      payload = await response.json()
    } catch {
      throw new Error(`本地研究内核返回了无效响应（HTTP ${response.status}）`)
    }
    if (!response.ok) {
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
  }
}

function runtimeLaunch({
  app,
  projectRoot,
  token,
  marketData = {},
  resourcesPath = process.resourcesPath,
}) {
  const userData = app.getPath('userData')
  const marketDataKey = String(marketData.key || '').trim()
  const marketDataSecret = String(marketData.secret || '').trim()
  const marketDataConfigured = Boolean(marketDataKey && marketDataSecret)
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
    marketDataConfigured ? 'alpaca_proxy' : 'unconfigured',
  ]
  const marketDataEnv = marketDataConfigured
    ? {
        ALPACA_PROXY_KEY: marketDataKey,
        ALPACA_PROXY_SECRET: marketDataSecret,
      }
    : {}
  if (app.isPackaged) {
    if (!resourcesPath) throw new Error('打包运行时目录不可用')
    return {
      command: path.join(
        resourcesPath,
        'runtime',
        'macos-research-runtime',
      ),
      args: sharedArgs,
      cwd: userData,
      token,
      marketDataEnv,
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
  }
}

function createLocalRuntimeProcess({
  app,
  projectRoot,
  spawnImpl = spawn,
  client = createLocalRuntimeClient(),
  startupTimeoutMs = 45_000,
  marketData = {},
}) {
  let child = null

  return {
    client,

    async start() {
      if (child) return client.status()
      const token = crypto.randomBytes(32).toString('base64url')
      const launch = runtimeLaunch({ app, projectRoot, token, marketData })
      child = spawnImpl(launch.command, launch.args, {
        cwd: launch.cwd,
        env: {
          ...process.env,
          ...launch.marketDataEnv,
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
      if (child && !child.killed) child.kill()
      child = null
    },
  }
}

module.exports = {
  createLocalRuntimeClient,
  createLocalRuntimeProcess,
  runtimeLaunch,
  validateRuntimeHandshake,
}
