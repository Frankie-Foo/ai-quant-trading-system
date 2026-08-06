const OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
const APP_REFERER = 'https://local.quant-research.app'
const APP_TITLE = 'AI Quant Research Desk'

function authorizationHeaders(apiKey) {
  const key = String(apiKey || '').trim()
  if (key.length < 8) throw new Error('OpenRouter API Key 未配置')
  return {
    Authorization: `Bearer ${key}`,
    'Content-Type': 'application/json',
    'HTTP-Referer': APP_REFERER,
    'X-Title': APP_TITLE,
  }
}

async function parseResponse(response) {
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error(`OpenRouter 返回了无效响应（HTTP ${response.status}）`)
  }
  if (!response.ok) {
    const typed = payload?.error?.metadata?.error_type
    const code = typed || `http_${response.status}`
    throw new Error(`OpenRouter 请求失败：${code}`)
  }
  return payload
}

function createOpenRouterClient({
  fetchImpl = fetch,
  baseUrl = OPENROUTER_BASE_URL,
  timeoutMs = 45_000,
} = {}) {
  const normalizedBase = String(baseUrl).replace(/\/+$/, '')

  return {
    async listModels(apiKey) {
      const response = await fetchImpl(
        `${normalizedBase}/models?output_modalities=text`,
        {
          method: 'GET',
          headers: authorizationHeaders(apiKey),
          signal: AbortSignal.timeout(timeoutMs),
        },
      )
      const payload = await parseResponse(response)
      if (!Array.isArray(payload.data)) {
        throw new Error('OpenRouter 模型目录格式无效')
      }
      return payload.data
        .filter((model) => model && typeof model.id === 'string' && model.id)
        .map((model) => ({
          id: model.id,
          name: typeof model.name === 'string' && model.name ? model.name : model.id,
          contextLength: Number.isFinite(model.context_length)
            ? model.context_length
            : null,
          promptPrice: model.pricing?.prompt ?? null,
          completionPrice: model.pricing?.completion ?? null,
        }))
    },

    async complete({ apiKey, model, messages, maxTokens = 8192, temperature = 0.2 }) {
      const modelId = String(model || '').trim()
      if (!modelId) throw new Error('尚未选择 OpenRouter 模型')
      if (!Array.isArray(messages) || !messages.length) {
        throw new Error('答疑消息不能为空')
      }
      const normalizedMessages = messages.map((message) => {
        const role = message?.role
        const content = typeof message?.content === 'string'
          ? message.content.trim()
          : ''
        if (!['system', 'user', 'assistant'].includes(role) || !content) {
          throw new Error('答疑消息格式无效')
        }
        return { role, content }
      })
      const response = await fetchImpl(`${normalizedBase}/chat/completions`, {
        method: 'POST',
        headers: authorizationHeaders(apiKey),
        body: JSON.stringify({
          model: modelId,
          messages: normalizedMessages,
          max_tokens: maxTokens,
          temperature,
          stream: false,
        }),
        signal: AbortSignal.timeout(timeoutMs),
      })
      const payload = await parseResponse(response)
      const content = payload?.choices?.[0]?.message?.content
      if (typeof content !== 'string' || !content.trim()) {
        throw new Error('OpenRouter 没有返回可用文本')
      }
      const usage = payload.usage || {}
      return {
        content: content.trim(),
        model: typeof payload.model === 'string' ? payload.model : modelId,
        usage: {
          promptTokens: Number(usage.prompt_tokens || 0),
          completionTokens: Number(usage.completion_tokens || 0),
          totalTokens: Number(usage.total_tokens || 0),
        },
      }
    },
  }
}

module.exports = {
  createOpenRouterClient,
}
