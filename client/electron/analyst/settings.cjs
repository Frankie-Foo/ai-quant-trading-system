const fs = require('node:fs')
const path = require('node:path')

const SETTINGS_SCHEMA = 'macos-research-settings.v1'
const MODEL_KEYS = ['question', 'catalyst', 'red_team', 'supervisor']

function validateServiceUrl(value) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('研究数据服务地址不能为空')
  }
  let parsed
  try {
    parsed = new URL(value.trim())
  } catch {
    throw new Error('研究数据服务地址无效')
  }
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname)
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && loopback)) {
    throw new Error('远程研究数据服务必须使用 HTTPS')
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('研究数据服务地址不能包含凭据、查询参数或片段')
  }
  return parsed.href.replace(/\/+$/, '')
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
    dataServiceUrl: '',
    encryptedDataAccessToken: '',
    encryptedOpenRouterApiKey: '',
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
      if (!parsed || parsed.schemaVersion !== SETTINGS_SCHEMA) return emptySettings()
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
    saveConnection(connection) {
      const dataServiceUrl = validateServiceUrl(connection.dataServiceUrl)
      const openRouterApiKey = String(connection.openRouterApiKey || '').trim()
      if (openRouterApiKey.length < 8) {
        throw new Error('OpenRouter API Key 无效')
      }
      const current = read()
      write({
        ...current,
        dataServiceUrl,
        encryptedDataAccessToken: encrypt(
          String(connection.dataAccessToken || '').trim(),
        ),
        encryptedOpenRouterApiKey: encrypt(openRouterApiKey),
      })
    },

    saveModels(models) {
      const current = read()
      if (!current.dataServiceUrl || !current.encryptedOpenRouterApiKey) {
        throw new Error('请先完成数据服务和 OpenRouter 配置')
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
          current.dataServiceUrl
          && current.encryptedOpenRouterApiKey
          && MODEL_KEYS.every((key) => models[key]),
        ),
        dataServiceUrl: current.dataServiceUrl,
        dataAccessTokenConfigured: Boolean(current.encryptedDataAccessToken),
        openRouterKeyConfigured: Boolean(current.encryptedOpenRouterApiKey),
        models,
      }
    },

    loadSecrets() {
      const current = read()
      return {
        dataAccessToken: decrypt(current.encryptedDataAccessToken),
        openRouterApiKey: decrypt(current.encryptedOpenRouterApiKey),
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
  validateServiceUrl,
}
