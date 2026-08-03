const fs = require('node:fs')
const path = require('node:path')

const SETTINGS_SCHEMA = 'desktop-local-research-settings.v7'
const PREVIOUS_PAPER_SETTINGS_SCHEMA = 'desktop-local-research-settings.v6'
const PREVIOUS_EXECUTION_SETTINGS_SCHEMA = 'desktop-local-research-settings.v5'
const RESEARCH_SETTINGS_SCHEMA = 'desktop-local-research-settings.v4'
const LEGACY_SETTINGS_SCHEMA = 'macos-research-settings.v1'
const PREVIOUS_SETTINGS_SCHEMA = 'macos-local-research-settings.v2'
const PRIOR_SETTINGS_SCHEMA = 'macos-local-research-settings.v3'
const MODEL_KEYS = ['question', 'catalyst', 'red_team', 'supervisor']
const ORDER_ACTIONS = new Set(['OpenLong', 'ReduceLong'])

function requiredText(value, label, maxLength = 200) {
  const normalized = String(value || '').trim()
  if (!normalized || normalized.length > maxLength) {
    throw new Error(`${label} 无效`)
  }
  return normalized
}

function normalizeExecutionOrder(order) {
  if (!order || typeof order !== 'object' || Array.isArray(order)) {
    throw new Error('订单内容无效')
  }
  const action = String(order.action || '')
  if (!ORDER_ACTIONS.has(action)) {
    throw new Error('订单操作只允许 OpenLong 或 ReduceLong')
  }
  const symbol = requiredText(order.symbol, '股票代码', 15).toUpperCase()
  if (!/^[A-Z][A-Z0-9.-]{0,14}$/.test(symbol)) {
    throw new Error('股票代码无效')
  }
  const quantity = Number(order.quantity)
  if (!Number.isSafeInteger(quantity) || quantity <= 0) {
    throw new Error('订单数量必须是正整数')
  }
  const limitPrice = Number(order.limit_price)
  if (!Number.isFinite(limitPrice) || limitPrice <= 0) {
    throw new Error('限价必须大于 0')
  }
  if (
    order.security_type !== 'STK'
    || order.exchange !== 'SMART'
    || order.currency !== 'USD'
    || order.order_type !== 'LMT'
    || order.tif !== 'DAY'
  ) {
    throw new Error('客户端只允许 SMART 美股 DAY 限价单')
  }
  const clientOrderId = requiredText(
    order.client_order_id,
    '客户端订单号',
    80,
  )
  if (!/^[A-Za-z0-9_.-]+$/.test(clientOrderId)) {
    throw new Error('客户端订单号格式无效')
  }
  return {
    client_order_id: clientOrderId,
    symbol,
    security_type: 'STK',
    exchange: 'SMART',
    currency: 'USD',
    action,
    quantity,
    limit_price: limitPrice,
    order_type: 'LMT',
    tif: 'DAY',
  }
}

function normalizeExecutionCommand(command) {
  if (!command || typeof command !== 'object' || Array.isArray(command)) {
    throw new Error('执行命令无效')
  }
  const kind = String(command.kind || '')
  if (['connect', 'disconnect', 'recover'].includes(kind)) return { kind }
  if (kind === 'bind_account') {
    return {
      kind,
      confirmation: requiredText(command.confirmation, '账户绑定确认文字'),
    }
  }
  if (kind === 'arm') {
    return {
      kind,
      confirmation: String(command.confirmation || '').trim().slice(0, 200),
    }
  }
  if (kind === 'disarm') return { kind }
  if (kind === 'preview') {
    return { kind, order: normalizeExecutionOrder(command.order) }
  }
  if (kind === 'submit') {
    return {
      kind,
      preview_id: requiredText(command.preview_id, '预览编号'),
      confirmation: requiredText(command.confirmation, '订单确认文字'),
      order: normalizeExecutionOrder(command.order),
    }
  }
  throw new Error('未知执行命令')
}

function normalizePaperAutopilotCommand(command) {
  if (!command || typeof command !== 'object' || Array.isArray(command)) {
    throw new Error('模拟盘自动执行命令无效')
  }
  const kind = String(command.kind || '')
  if (['connect', 'disconnect', 'validate_plan', 'stop'].includes(kind)) {
    return { kind }
  }
  if (kind === 'start') {
    return {
      kind,
      confirmation: requiredText(
        command.confirmation,
        '模拟盘自动执行确认文字',
      ),
    }
  }
  throw new Error('未知模拟盘自动执行命令')
}

function safeResultText(value, maxLength = 500) {
  if (typeof value !== 'string') return null
  return value
    .slice(0, maxLength)
    .replace(/vq:live:[^:\s]+:/gi, 'vq:live:[redacted]:')
    .replace(/\b(DU|U)\d{4,}\b/gi, '$1***')
}

function sanitizeExecutionCommandResult(commandKind, result) {
  const source = result && typeof result === 'object' && !Array.isArray(result)
    ? result
    : {}
  if (commandKind === 'preview') {
    const intent = source.intent && typeof source.intent === 'object'
      ? source.intent
      : {}
    const whatIf = source.what_if && typeof source.what_if === 'object'
      ? source.what_if
      : {}
    return {
      schema_version: safeResultText(source.schema_version, 80),
      kind: 'execution_preview',
      status: safeResultText(source.status, 80),
      preview_id: safeResultText(source.preview_id, 200),
      mode: source.mode === 'live' ? 'live' : null,
      account_masked: safeResultText(source.account_masked, 80),
      intent: {
        client_order_id: safeResultText(intent.client_order_id, 80),
        symbol: safeResultText(intent.symbol, 15),
        security_type: safeResultText(intent.security_type, 10),
        exchange: safeResultText(intent.exchange, 20),
        currency: safeResultText(intent.currency, 10),
        action: safeResultText(intent.action, 20),
        quantity: Number.isSafeInteger(intent.quantity) ? intent.quantity : null,
        limit_price: safeResultText(intent.limit_price, 40),
        order_type: safeResultText(intent.order_type, 10),
        tif: safeResultText(intent.tif, 10),
        notional: safeResultText(intent.notional, 40),
      },
      what_if: {
        accepted: whatIf.accepted === true,
        estimated_commission: safeResultText(
          whatIf.estimated_commission,
          40,
        ),
        initial_margin_change: safeResultText(
          whatIf.initial_margin_change,
          40,
        ),
        warning: safeResultText(whatIf.warning, 500),
      },
      confirmation_phrase: safeResultText(source.confirmation_phrase, 200),
      warning_confirmation_hash:
        typeof source.warning_confirmation_hash === 'string'
        && /^[A-F0-9]{8}$/.test(source.warning_confirmation_hash)
          ? source.warning_confirmation_hash
          : null,
      expires_at_utc: safeResultText(source.expires_at_utc, 50),
    }
  }
  if (commandKind === 'submit') {
    return {
      schema_version: safeResultText(source.schema_version, 80),
      kind: 'execution_receipt',
      status: safeResultText(source.status, 80),
      client_order_id: safeResultText(source.client_order_id, 80),
      broker_order_id: ['string', 'number'].includes(
        typeof source.broker_order_id,
      ) ? source.broker_order_id : null,
      perm_id: ['string', 'number'].includes(typeof source.perm_id)
        ? source.perm_id
        : null,
      account_masked: safeResultText(source.account_masked, 80),
      last_error_code: safeResultText(source.last_error_code, 200),
      post_submit_snapshot_refreshed:
        typeof source.post_submit_snapshot_refreshed === 'boolean'
          ? source.post_submit_snapshot_refreshed
          : null,
      snapshot_refresh_error: safeResultText(
        source.snapshot_refresh_error,
        200,
      ),
    }
  }
  throw new Error('无法净化未知的执行结果')
}

function parseIbkrProfileText(text) {
  const fields = {}
  const mapping = new Map([
    ['实盘端口', 'livePort'],
  ])
  for (const line of String(text || '').split(/\r?\n/)) {
    const matched = line.match(/^\s*([^=：:]+?)\s*(?:=|：|:)\s*(.*?)\s*$/)
    if (!matched) continue
    const key = matched[1].replace(/\s+/g, '')
    if (key.includes('密码')) continue
    const target = mapping.get(key)
    if (!target) continue
    fields[target] = matched[2].trim()
  }
  const livePort = Number(fields.livePort)
  if (!Number.isInteger(livePort) || livePort < 1 || livePort > 65535) {
    throw new Error('导入文件缺少有效的实盘端口')
  }
  if (livePort !== 4001) {
    throw new Error('实盘端口必须是 4001')
  }
  return { livePort, accountDiscoveryRequired: true }
}

function normalizeModels(models) {
  if (!models || typeof models !== 'object' || Array.isArray(models)) {
    throw new Error('模型配置无效')
  }
  const normalized = {}
  for (const key of MODEL_KEYS) {
    const value = models[key]
    if (typeof value !== 'string' || !value.trim()) {
      throw new Error(`${key} 模型不能为空`)
    }
    normalized[key] = value.trim()
  }
  return normalized
}

function emptySettings() {
  return {
    schemaVersion: SETTINGS_SCHEMA,
    encryptedOpenRouterApiKey: '',
    encryptedMassiveApiKey: '',
    encryptedMarketDataKey: '',
    encryptedMarketDataSecret: '',
    encryptedSecUserAgent: '',
    encryptedIbkrHost: '',
    encryptedIbkrClientId: '',
    encryptedIbkrLiveAccount: '',
    encryptedIbkrPaperHost: '',
    encryptedIbkrPaperClientId: '',
    encryptedIbkrPaperAccount: '',
    maxOrderNotional: 0,
    models: {},
  }
}

function maskAccount(value) {
  const normalized = String(value || '').trim()
  if (!normalized) return ''
  const prefix = normalized.match(/^[A-Za-z]+/)?.[0] || normalized.slice(0, 1)
  const suffix = normalized.slice(-4)
  return `${prefix}***${suffix}`
}

function createSecureSettingsStore({ filePath, safeStorage, fsImpl = fs }) {
  if (!filePath || !safeStorage) {
    throw new Error('安全设置存储缺少必要依赖')
  }

  function assertEncryption() {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('系统安全存储不可用；不会以明文保存 API Key')
    }
  }

  function read() {
    try {
      const parsed = JSON.parse(fsImpl.readFileSync(filePath, 'utf8'))
      if (!parsed || typeof parsed !== 'object') return emptySettings()
      if (parsed.schemaVersion === PRIOR_SETTINGS_SCHEMA) {
        return {
          ...emptySettings(),
          encryptedOpenRouterApiKey: String(parsed.encryptedOpenRouterApiKey || ''),
          encryptedMarketDataKey: String(parsed.encryptedMarketDataKey || ''),
          encryptedMarketDataSecret: String(parsed.encryptedMarketDataSecret || ''),
          models: parsed.models || {},
        }
      }
      if ([LEGACY_SETTINGS_SCHEMA, PREVIOUS_SETTINGS_SCHEMA].includes(
        parsed.schemaVersion,
      )) {
        return {
          ...emptySettings(),
          encryptedOpenRouterApiKey: String(
            parsed.encryptedOpenRouterApiKey || '',
          ),
          models: parsed.models || {},
        }
      }
      if ([RESEARCH_SETTINGS_SCHEMA, PREVIOUS_EXECUTION_SETTINGS_SCHEMA,
        PREVIOUS_PAPER_SETTINGS_SCHEMA].includes(parsed.schemaVersion)) {
        return { ...emptySettings(), ...parsed, schemaVersion: SETTINGS_SCHEMA }
      }
      if (parsed.schemaVersion !== SETTINGS_SCHEMA) return emptySettings()
      return { ...emptySettings(), ...parsed }
    } catch (error) {
      if (error && error.code === 'ENOENT') return emptySettings()
      throw new Error('无法读取本地安全设置')
    }
  }

  function write(settings) {
    fsImpl.mkdirSync(path.dirname(filePath), { recursive: true })
    const temporary = `${filePath}.tmp`
    fsImpl.writeFileSync(temporary, `${JSON.stringify(settings, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
    })
    fsImpl.renameSync(temporary, filePath)
  }

  function encrypt(value) {
    if (!value) return ''
    assertEncryption()
    return safeStorage.encryptString(value).toString('base64')
  }

  function decrypt(value) {
    if (!value) return ''
    assertEncryption()
    try {
      return safeStorage.decryptString(Buffer.from(value, 'base64'))
    } catch {
      throw new Error('本地凭据无法解密，请重新配置')
    }
  }

  return {
    saveConnectionSecrets(values) {
      const openRouterApiKey = String(values?.openRouterApiKey || '').trim()
      const massiveApiKey = String(values?.massiveApiKey || '').trim()
      const marketDataKey = String(values?.marketDataKey || '').trim()
      const marketDataSecret = String(values?.marketDataSecret || '').trim()
      const secUserAgent = String(values?.secUserAgent || '').trim()
      if (openRouterApiKey.length < 8) {
        throw new Error('OpenRouter API Key 无效')
      }
      if (marketDataKey.length < 8 || marketDataSecret.length < 16) {
        throw new Error('Alpaca 代理行情凭据无效')
      }
      if (massiveApiKey.length < 8) {
        throw new Error('Massive API Key 无效')
      }
      if (secUserAgent.length < 8 || !secUserAgent.includes('@')) {
        throw new Error('SEC 联系信息必须包含联系邮箱')
      }
      const current = read()
      write({
        ...current,
        encryptedOpenRouterApiKey: encrypt(openRouterApiKey),
        encryptedMassiveApiKey: encrypt(massiveApiKey),
        encryptedMarketDataKey: encrypt(marketDataKey),
        encryptedMarketDataSecret: encrypt(marketDataSecret),
        encryptedSecUserAgent: encrypt(secUserAgent),
      })
    },

    saveOpenRouterKey(value) {
      const openRouterApiKey = String(value || '').trim()
      if (openRouterApiKey.length < 8) {
        throw new Error('OpenRouter API Key 无效')
      }
      const current = read()
      write({
        ...current,
        encryptedOpenRouterApiKey: encrypt(openRouterApiKey),
      })
    },

    saveModels(models) {
      const current = read()
      if (
        !current.encryptedOpenRouterApiKey
        || !current.encryptedMassiveApiKey
        || !current.encryptedMarketDataKey
        || !current.encryptedMarketDataSecret
        || !current.encryptedSecUserAgent
      ) {
        throw new Error('请先完成模型与行情连接配置')
      }
      write({ ...current, models: normalizeModels(models) })
    },

    saveExecutionSettings(values) {
      const current = read()
      const host = String(values?.host || '').trim()
      const clientIdText = String(values?.clientId ?? '').trim()
      const maxOrderNotional = Number(values?.maxOrderNotional)
      const preserve = (value, encryptedValue) => (
        value ? encrypt(value) : encryptedValue
      )
      if (host && (host.length > 253 || /[\s/:]/.test(host))) {
        throw new Error('IBKR 主机名无效')
      }
      if (clientIdText && (
        !/^\d+$/.test(clientIdText)
        || Number(clientIdText) > 2_147_483_647
      )) {
        throw new Error('IBKR Client ID 必须是非负整数')
      }
      if (!Number.isFinite(maxOrderNotional) || maxOrderNotional <= 0) {
        throw new Error('单笔最大新增开仓金额（仅 OpenLong）必须大于 0')
      }
      const next = {
        ...current,
        encryptedIbkrHost: preserve(host, current.encryptedIbkrHost),
        encryptedIbkrClientId: preserve(
          clientIdText,
          current.encryptedIbkrClientId,
        ),
        encryptedIbkrLiveAccount: current.encryptedIbkrLiveAccount,
        maxOrderNotional,
      }
      if (
        !next.encryptedIbkrHost
        || !next.encryptedIbkrClientId
      ) {
        throw new Error('请完整填写 IBKR 主机和 Client ID')
      }
      write(next)
    },

    saveBoundExecutionAccount(value) {
      const liveAccount = String(value || '').trim().toUpperCase()
      if (!/^[A-Z0-9-]{4,32}$/.test(liveAccount)) {
        throw new Error('IBKR 返回的账户 ID 无效')
      }
      const current = read()
      const existing = decrypt(current.encryptedIbkrLiveAccount).toUpperCase()
      if (existing && existing !== liveAccount) {
        throw new Error('IBKR account is already bound; clear it before rebinding')
      }
      write({
        ...current,
        encryptedIbkrLiveAccount: encrypt(liveAccount),
      })
    },

    clearBoundExecutionAccount() {
      const current = read()
      write({ ...current, encryptedIbkrLiveAccount: '' })
    },

    savePaperExecutionSettings(values) {
      const current = read()
      const host = String(values?.host || '').trim()
      const clientIdText = String(values?.clientId ?? '').trim()
      const paperAccount = String(values?.paperAccount || '').trim().toUpperCase()
      if (!host || host.length > 253 || /[\s/:]/.test(host)) {
        throw new Error('IBKR 模拟盘主机名无效')
      }
      if (!/^\d+$/.test(clientIdText) || Number(clientIdText) > 2_147_483_647) {
        throw new Error('IBKR 模拟盘 Client ID 必须是非负整数')
      }
      if (!/^DU[A-Z0-9-]{4,30}$/.test(paperAccount)) {
        throw new Error('IBKR 模拟盘账户必须是 DU 开头的账户 ID')
      }
      write({
        ...current,
        encryptedIbkrPaperHost: encrypt(host),
        encryptedIbkrPaperClientId: encrypt(clientIdText),
        encryptedIbkrPaperAccount: encrypt(paperAccount),
      })
    },

    loadPublic() {
      const current = read()
      let models = {}
      try {
        models = normalizeModels(current.models)
      } catch {
        models = {}
      }
      return {
        schemaVersion: SETTINGS_SCHEMA,
        configured: Boolean(
          current.encryptedOpenRouterApiKey
          && current.encryptedMassiveApiKey
          && current.encryptedMarketDataKey
          && current.encryptedMarketDataSecret
          && current.encryptedSecUserAgent
          && MODEL_KEYS.every((key) => models[key]),
        ),
        openRouterKeyConfigured: Boolean(current.encryptedOpenRouterApiKey),
        massiveConfigured: Boolean(current.encryptedMassiveApiKey),
        secConfigured: Boolean(current.encryptedSecUserAgent),
        marketDataConfigured: Boolean(
          current.encryptedMarketDataKey
          && current.encryptedMarketDataSecret,
        ),
        marketDataProvider: 'standalone_massive_alpaca',
        execution: {
          configured: Boolean(
            current.encryptedIbkrHost
            && current.encryptedIbkrClientId
            && Number(current.maxOrderNotional) > 0
          ),
          hostConfigured: Boolean(current.encryptedIbkrHost),
          clientIdConfigured: Boolean(current.encryptedIbkrClientId),
          accountBound: Boolean(current.encryptedIbkrLiveAccount),
          liveAccountMasked: maskAccount(
            decrypt(current.encryptedIbkrLiveAccount),
          ),
          maxOrderNotional: Number(current.maxOrderNotional) || 0,
        },
        paperExecution: {
          configured: Boolean(
            current.encryptedIbkrPaperHost
            && current.encryptedIbkrPaperClientId
            && current.encryptedIbkrPaperAccount
          ),
          hostConfigured: Boolean(current.encryptedIbkrPaperHost),
          clientIdConfigured: Boolean(current.encryptedIbkrPaperClientId),
          accountConfigured: Boolean(current.encryptedIbkrPaperAccount),
          paperAccountMasked: maskAccount(
            decrypt(current.encryptedIbkrPaperAccount),
          ),
          port: 4002,
        },
        models,
      }
    },

    loadSecrets() {
      const current = read()
      return {
        openRouterApiKey: decrypt(current.encryptedOpenRouterApiKey),
        massiveApiKey: decrypt(current.encryptedMassiveApiKey),
        marketDataKey: decrypt(current.encryptedMarketDataKey),
        marketDataSecret: decrypt(current.encryptedMarketDataSecret),
        secUserAgent: decrypt(current.encryptedSecUserAgent),
      }
    },

    loadExecutionSecrets() {
      const current = read()
      return {
        host: decrypt(current.encryptedIbkrHost),
        clientId: Number(decrypt(current.encryptedIbkrClientId)) || 0,
        liveAccount: decrypt(current.encryptedIbkrLiveAccount),
        port: 4001,
        maxOrderNotional: Number(current.maxOrderNotional) || 0,
      }
    },

    loadPaperExecutionSecrets() {
      const current = read()
      return {
        host: decrypt(current.encryptedIbkrPaperHost),
        clientId: Number(decrypt(current.encryptedIbkrPaperClientId)) || 0,
        paperAccount: decrypt(current.encryptedIbkrPaperAccount),
        port: 4002,
      }
    },

    clear() {
      try {
        fsImpl.unlinkSync(filePath)
      } catch (error) {
        if (!error || error.code !== 'ENOENT') {
          throw new Error('无法清除本地设置')
        }
      }
    },
  }
}

module.exports = {
  MODEL_KEYS,
  SETTINGS_SCHEMA,
  createSecureSettingsStore,
  normalizeExecutionCommand,
  normalizePaperAutopilotCommand,
  sanitizeExecutionCommandResult,
  normalizeModels,
  parseIbkrProfileText,
  safeResultText,
}
