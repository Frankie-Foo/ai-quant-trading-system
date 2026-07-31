const fs = require('node:fs')
const path = require('node:path')

const SETTINGS_SCHEMA = 'desktop-local-research-settings.v4'
const LEGACY_SETTINGS_SCHEMA = 'macos-research-settings.v1'
const PREVIOUS_SETTINGS_SCHEMA = 'macos-local-research-settings.v2'
const PRIOR_SETTINGS_SCHEMA = 'macos-local-research-settings.v3'
const MODEL_KEYS = ['question', 'catalyst', 'red_team', 'supervisor']

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
    models: {},
  }
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
  normalizeModels,
}
