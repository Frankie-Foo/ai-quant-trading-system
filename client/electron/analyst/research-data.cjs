const { validateServiceUrl } = require('./settings.cjs')

function createResearchDataClient({ fetchImpl = fetch, timeoutMs = 30_000 } = {}) {
  return {
    async fetchDesk({ baseUrl, accessToken }) {
      const serviceUrl = validateServiceUrl(baseUrl)
      const headers = {
        Accept: 'application/json',
        'Cache-Control': 'no-store',
      }
      const token = String(accessToken || '').trim()
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetchImpl(`${serviceUrl}/v1/desk`, {
        method: 'GET',
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      })
      let payload
      try {
        payload = await response.json()
      } catch {
        throw new Error(`研究数据服务返回了无效响应（HTTP ${response.status}）`)
      }
      if (!response.ok) {
        throw new Error(`研究数据服务不可用（HTTP ${response.status}）`)
      }
      if (
        !payload
        || payload.schema_version !== 'trading_desk_evidence.v1'
        || payload.stage !== 'research_only'
        || payload.orders_authorized !== false
        || !payload.selection
        || !payload.review
        || !Array.isArray(payload.jobs)
        || !Array.isArray(payload.agents)
      ) {
        throw new Error('研究数据服务没有提供可信的 non-executable 证据')
      }
      return payload
    },
  }
}

module.exports = {
  createResearchDataClient,
}
