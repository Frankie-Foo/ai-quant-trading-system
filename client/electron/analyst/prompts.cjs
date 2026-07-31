const ROLE_PROMPTS = {
  catalyst: [
    '你是只读催化剂分析 Agent。',
    '只依据提供的冻结选股与复盘证据解释催化剂强度、时效、缺口和延续条件。',
    '不得补造新闻，不得输出确定性收益承诺，不得把历史快照说成今日名单。',
  ].join(''),
  red_team: [
    '你是只读红队 Agent。',
    '主动寻找反例、过期证据、错误因果、拥挤交易、流动性与风险边界。',
    '结论只能是研究意见，不能推翻硬闸、不能授权买卖或修改系统参数。',
  ].join(''),
  supervisor: [
    '你是只读证据审计 Agent。',
    '检查选股、复盘、任务和成熟度证据是否完整、一致、当前且可追溯。',
    '明确区分事实、推断和缺失证据；不能下单、不能修改生产配置。',
  ].join(''),
}

function compactDeskEvidence(desk) {
  const selection = desk?.selection || {}
  const review = desk?.review || {}
  return {
    observed_at_utc: desk?.observed_at_utc || null,
    target_trade_date: desk?.target_trade_date || null,
    stage: desk?.stage || null,
    pipeline_status: desk?.pipeline_status || null,
    orders_authorized: false,
    selection: {
      status: selection.status || null,
      blocker: selection.blocker || null,
      target_trade_date: selection.target_trade_date || null,
      session_date: selection.session_date || null,
      stale: Boolean(selection.stale),
      pass_count: selection.pass_count ?? 0,
      candidates: Array.isArray(selection.candidates)
        ? selection.candidates.slice(0, 20)
        : [],
    },
    review: {
      status: review.status || null,
      session_date: review.session_date || null,
      stale: Boolean(review.stale),
      opportunity_count: review.opportunity_count ?? 0,
      opportunities: Array.isArray(review.opportunities)
        ? review.opportunities.slice(0, 12)
        : [],
    },
    maturity: desk?.maturity || {},
    jobs: Array.isArray(desk?.jobs) ? desk.jobs.slice(0, 12) : [],
  }
}

function assistantMessages(question, desk) {
  const text = String(question || '').trim()
  if (!text || text.length > 4_000) {
    throw new Error('问题必须为 1–4000 个字符')
  }
  return [
    {
      role: 'system',
      content: [
        '你是美股日内量化研究助手，只能解释所给证据。',
        '用中文回答，先说证据日期与是否过期，再回答问题。',
        '不能承诺收益，不能把候选名单当作买入指令，不能声称已经下单。',
        '如果证据不足，明确说“不知道/证据不足”并指出需要什么。',
      ].join(''),
    },
    {
      role: 'user',
      content: `研究证据：${JSON.stringify(compactDeskEvidence(desk))}\n\n用户问题：${text}`,
    },
  ]
}

function agentMessages(role, desk) {
  const rolePrompt = ROLE_PROMPTS[role]
  if (!rolePrompt) throw new Error('未知 Agent 角色')
  return [
    {
      role: 'system',
      content: `${rolePrompt}请用中文给出结构化、简洁、可追溯的结论。`,
    },
    {
      role: 'user',
      content: `请审阅以下研究证据：${JSON.stringify(compactDeskEvidence(desk))}`,
    },
  ]
}

module.exports = {
  ROLE_PROMPTS,
  agentMessages,
  assistantMessages,
  compactDeskEvidence,
}
