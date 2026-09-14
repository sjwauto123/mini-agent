/**
 * Mini Agent 前端主应用（单文件 React 组件）。
 *
 * 文件结构：
 *   1. 类型与常量     —— 与后端接口对齐的数据形状，以及错误码 / 工具 / 状态的中文映射
 *   2. 渲染工具函数   —— 轻量 Markdown 渲染、统一 fetch 封装、运行事件文案
 *   3. TraceGroups    —— “执行日志”视图：把一次会话的多次运行按轮次分组展示
 *   4. App            —— 会话 / 消息 / 运行 / 轨迹的状态机 + SSE 订阅 + 全部交互
 *
 * 约束：不引入路由与全局状态库，所有状态集中由 App 组件持有并向下传递。
 */
import { useEffect, useRef, useState } from 'react'
import {
  AlertCircle, Bot, BrainCircuit, CheckCircle2, ChevronDown, CircleDashed,
  ListTree, Menu, MessageSquare, Plus, Send, Square, Trash2, Wrench, X,
} from 'lucide-react'

// —— 1. 类型与常量 ——
// 以下类型与 backend 的接口返回逐一对应，字段变更需同步后端契约。
type Model = { name: string; model: string; mode: string; context_window: number }
type Session = { id: string; model_name: string; timezone: string; title: string | null; created_at: string }
type Message = { role: string; content?: string; thinking?: string; seq: number; tool_calls?: unknown[]; name?: string }
type Run = { id: string; status: string; answer?: string; input_preview?: string; error?: { code: string }; model_calls?: number; created_at?: string; finished_at?: string }
type TraceItem = { id: number; event_type: string; created_at: string; payload: Record<string, unknown> }
type SessionTraceItem = TraceItem & { run_id: string }
type View = 'chat' | 'trace'

// “已结束”的运行状态集合：运行一旦进入这些状态，SSE 订阅即可关闭，不再等待后续事件。
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'limit_reached', 'interrupted'])
// 后端错误码 → 用户可读文案。前端只展示文案，不直接暴露原始 code。
const ERROR_MESSAGES: Record<string, string> = {
  cancelled: '本次运行已停止',
  context_too_large: '对话内容超过模型上下文限制',
  input_prepare_failed: '消息处理失败',
  message_required: '请输入消息',
  model_api_key_missing: '模型 API 密钥未配置',
  model_call_limit: '本次运行已达到模型调用次数上限',
  model_context_budget_invalid: '模型上下文配置无效',
  model_not_configured: '模型未配置',
  model_protocol_error: '模型返回了无法解析的响应',
  model_unavailable: '模型服务暂时不可用，请检查网络连接后重试',
  run_not_found: '运行记录不存在',
  run_timeout: '本次运行超时',
  service_restarted: '服务重启导致运行中断',
  session_busy: '当前会话正在处理其他消息',
  session_busy_or_missing: '当前会话忙碌或不存在',
  session_not_found: '会话不存在',
  timezone_invalid: '时区配置无效',
  invalid_arguments: '工具参数不符合要求',
  invalid_expression: '算式格式不正确',
  division_by_zero: '除数不能为零',
  expression_too_long: '算式过长，无法计算',
  location_not_supported: '暂不支持该城市的模拟天气',
  date_not_supported: '模拟天气仅支持今天起七天内的日期',
  resource_not_found: '找不到指定资源，或资源不属于当前会话',
  tool_timeout: '工具执行超时',
  tool_error: '工具执行失败',
  unknown_tool: '未找到指定工具',
}
// 工具的英文标识 → 中文展示名（用于执行日志中的“调用工具：xxx”）。
const TOOL_NAMES: Record<string, string> = {
  calculator: '计算器', search: '搜索', weather: '天气', todo: '待办',
  resource_read: '资源读取', resource_search: '资源查找',
}
// 运行状态 → 中文标签（执行日志中的状态徽标）。
const STATUS_NAMES: Record<string, string> = {
  running: '运行中', completed: '已完成', failed: '失败', cancelled: '已停止',
  limit_reached: '达到上限', interrupted: '已中断', cancel_requested: '正在停止',
}

// 取错误文案：未知 code 一律回落到通用提示，避免把内部标识暴露给用户。
const errorMessage = (code: string) => ERROR_MESSAGES[code] || '请求失败，请稍后重试'

// 同 errorMessage，但用于执行日志“原因：xxx”的场景，容错接受任意类型的 code。
const detailMessage = (code: unknown) => ERROR_MESSAGES[String(code || '')] || '执行过程中出现问题，请稍后重试'

// —— 2. 渲染工具函数 ——
// 用最小正则实现 Markdown 子集渲染，不引入第三方库；
// 安全前提是所有文本先经 escapeHtml 转义，之后拼入标签不会造成 XSS。
// 转义 HTML 元字符，是后续“拼字符串成 HTML”的前提。
const escapeHtml = (value: string) => value.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]!))

// 行内 Markdown：粗体与行内代码。
const inlineMarkdown = (value: string) => escapeHtml(value).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')

// 把一行表格文本按 `|` 拆成单元格，并去掉首尾竖线与空白。
const tableCells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())

// 判断某行是否为 Markdown 表格分隔行（如 | --- | :---: |），用于识别表头。
const isTableDivider = (line: string) => {
  const cells = tableCells(line)
  return cells.length > 0 && cells.every(cell => /^:?-{2,}:?$/.test(cell))
}

// 表格占位符：先塞进文本流，等其它行内替换做完后再回填真实 <table>，避免互相干扰。
const TABLE_TOKEN = '\u0001table:'

// 把“表头行 + 分隔行 + 数据行”渲染成带横向滚动容器的表格。
const renderTable = (lines: string[]) => {
  const cell = (name: 'th' | 'td', text: string) => `<${name}>${inlineMarkdown(text)}</${name}>`
  const head = tableCells(lines[0]).map(value => cell('th', value)).join('')
  const body = lines.slice(2).map(row => `<tr>${tableCells(row).map(value => cell('td', value)).join('')}</tr>`).join('')
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
}

/**
 * 把助手回答渲染为 HTML：支持围栏代码块、标题、无序列表、粗体、行内代码与表格。
 * 流程：先扫描剥离表格 → 整段转义 → 逐条正则替换行内 / 块级语法 → 最后回填表格。
 */
const markdownHtml = (value: string) => {
  const source = value.split('\n')
  const tables: string[] = []
  const kept: string[] = []
  let fenced = false
  for (let index = 0; index < source.length; index += 1) {
    const line = source[index]
    if (line.trim().startsWith('```')) { fenced = !fenced; kept.push(line); continue }
    if (!fenced && line.includes('|') && index + 1 < source.length && isTableDivider(source[index + 1])) {
      const block = [line, source[index + 1]]
      index += 2
      while (index < source.length && source[index].trim() !== '' && source[index].includes('|')) { block.push(source[index]); index += 1 }
      index -= 1
      tables.push(renderTable(block))
      kept.push(`${TABLE_TOKEN}${tables.length - 1}`)
      continue
    }
    kept.push(line)
  }
  return escapeHtml(kept.join('\n'))
    .replace(/```([\s\S]*?)```/g, '<pre>$1</pre>')
    .replace(/^### (.*)$/gm, '<h3>$1</h3>')
    .replace(/^## (.*)$/gm, '<h2>$1</h2>')
    .replace(/^# (.*)$/gm, '<h1>$1</h1>')
    .replace(/^[-*] (.*)$/gm, '<li>$1</li>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br/>')
    .replace(new RegExp(`(?:<br/>)?${TABLE_TOKEN}(\\d+)(?:<br/>)?`, 'g'), (_match: string, id: string) => tables[Number(id)])
}

/**
 * 统一的后端调用封装：默认带 JSON 头，把非 2xx 响应转换成带中文文案的 Error。
 * 调用方只需 try/catch，即可直接拿到可展示的 message。
 */
const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const code = body.detail?.code || `http_${response.status}`
    throw new Error(body.detail?.message || errorMessage(code))
  }
  return response.json()
}

/**
 * 把一条运行事件翻译为“图标 + 语气 + 标题 + 描述 + 备注”的展示模型。
 * event_type 与后端 TraceEvent 一一对应，新增事件类型需在此补分支；
 * tone（active / success / warning / danger / neutral）决定执行日志节点的配色。
 */
const tracePresentation = (item: TraceItem) => {
  const payload = item.payload
  const duration = typeof payload.duration_ms === 'number' ? `${Math.round(payload.duration_ms)} 毫秒` : ''
  const iteration = payload.iteration ? `第 ${payload.iteration} 轮` : ''
  switch (item.event_type) {
    case 'run.started': return { icon: CircleDashed, tone: 'active', title: '开始运行', description: '已接收用户消息，Agent 开始处理。', meta: '' }
    case 'model.started': return { icon: BrainCircuit, tone: 'active', title: payload.phase === 'summary' ? '压缩上下文' : '请求模型', description: `${iteration || '当前轮次'}，模型正在判断直接回答还是调用工具。`, meta: `第 ${payload.attempt || 1} 次尝试` }
    case 'model.finished': return { icon: CheckCircle2, tone: 'success', title: payload.phase === 'summary' ? '上下文压缩完成' : '模型响应完成', description: '模型已返回可处理的响应。', meta: duration }
    case 'model.retry': return { icon: AlertCircle, tone: 'warning', title: '模型请求重试', description: `请求未成功，正在自动重试。${payload.code ? ` 原因：${detailMessage(payload.code)}` : ''}`, meta: duration }
    case 'model.failed': return { icon: AlertCircle, tone: 'danger', title: '模型请求失败', description: `模型服务请求失败。${payload.code ? ` 原因：${detailMessage(payload.code)}` : ''}`, meta: duration }
    case 'model.invalid': return { icon: AlertCircle, tone: 'warning', title: '模型响应格式修复', description: `响应格式不符合协议，Agent 将要求模型重新返回。${payload.code ? ` 原因：${detailMessage(payload.code)}` : ''}`, meta: iteration }
    case 'assistant.delta': return { icon: Send, tone: 'active', title: payload.complete ? '回答输出完成' : '输出回答片段', description: 'Agent 正在把最终回答分片推送给前端。', meta: `${String(payload.content ?? '').length} 字` }
    case 'tool.started': return { icon: Wrench, tone: 'active', title: `调用工具：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: '模型选择了工具，正在执行并等待结果。', meta: iteration }
    case 'tool.finished': return { icon: payload.ok ? CheckCircle2 : AlertCircle, tone: payload.ok ? 'success' : 'danger', title: `工具${payload.ok ? '执行完成' : '执行失败'}：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: payload.ok ? '工具结果已写入上下文，Agent 将继续判断下一步。' : `工具返回错误：${detailMessage(payload.error_code)}`, meta: duration }
    case 'tool.reused': return { icon: Wrench, tone: 'success', title: `复用工具结果：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: '检测到相同调用，直接使用已保存的结果。', meta: iteration }
    case 'context.compacted': return { icon: ListTree, tone: 'success', title: '历史上下文已压缩', description: '较早的对话已整理为摘要，为后续问答释放上下文空间。', meta: `压缩至消息 ${payload.covered_through_seq}` }
    case 'context.compaction_failed': return { icon: AlertCircle, tone: 'warning', title: '上下文压缩未完成', description: '本次摘要生成失败，Agent 已使用可用历史继续运行。', meta: String(payload.code || '') }
    case 'run.finished': {
      const status = String(payload.status || '')
      const success = status === 'completed'
      return { icon: success ? CheckCircle2 : AlertCircle, tone: success ? 'success' : status === 'cancelled' ? 'warning' : 'danger', title: `运行${success ? '完成' : STATUS_NAMES[status] || '结束'}`, description: success ? '最终回答已生成并保存到当前会话。' : errorMessage(String(payload.code || status)), meta: '' }
    }
    default: return { icon: CircleDashed, tone: 'neutral', title: item.event_type, description: 'Agent 记录了一条运行事件。', meta: '' }
  }
}

// —— 3. 执行日志视图 ——
type TraceGroupsProps = {
  trace: SessionTraceItem[]
  runs: Run[]
  expandedRunId: string | null
  onToggle: (runId: string) => void
}

// 按 run_id 把同一次运行的轨迹聚合成可折叠分组；折叠状态由父组件持有，便于切换会话时统一重置。
const TraceGroups = ({ trace, runs, expandedRunId, onToggle }: TraceGroupsProps) => {
  const runIds = [...new Set(trace.map(item => item.run_id))]
  return <div className="trace-groups">{runIds.map(id => {
    const request = runs.find(runItem => runItem.id === id)
    const items = trace.filter(item => item.run_id === id)
    const expanded = expandedRunId === id
    return <section className={`trace-group ${expanded ? 'expanded' : ''}`} key={id}>
      <button className="trace-group-toggle" onClick={() => onToggle(id)} aria-expanded={expanded}>
        <span className="trace-group-main"><span className="trace-group-kicker">{new Date(request?.created_at || items[0].created_at).toLocaleString('zh-CN', { hour12: false })}</span><strong>{request?.input_preview || `运行 ${id.slice(0, 8)}`}</strong></span>
        <span className="trace-group-meta"><span className="trace-step-count">{items.length} 个步骤</span><span className={`run-status ${request?.status || ''}`}>{STATUS_NAMES[request?.status || ''] || '已完成'}</span><ChevronDown size={16}/></span>
      </button>
      {expanded && <ol className="trace-list">{items.map((item, index) => {
        const presentation = tracePresentation(item)
        const Icon = presentation.icon
        return <li key={`${item.run_id}-${item.id}`} className={`trace-step ${presentation.tone}`}><div className="trace-marker"><Icon size={15}/></div><div className="trace-content"><div className="trace-title"><strong>{index + 1}. {presentation.title}</strong><time>{new Date(item.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</time></div><p>{presentation.description}</p>{presentation.meta && <span className="trace-meta">{presentation.meta}</span>}{Object.keys(item.payload).length > 0 && <details><summary>查看事件数据</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></details>}</div></li>
      })}</ol>}
    </section>
  })}</div>
}

// —— 4. 主应用组件 ——
// 状态流：sessions（左侧列表）→ selected（当前会话）→ messages/runs/trace（该会话的数据）；
// busy 由 run.status 推导，用于禁用发送、切换“正在处理 / 正在停止”提示。
export default function App() {
  // —— 4.1 状态 ——
  const [models, setModels] = useState<Model[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [selected, setSelected] = useState(new URLSearchParams(location.search).get('session') || '')
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [run, setRun] = useState<Run | null>(null)
  const [runs, setRuns] = useState<Run[]>([])
  const [trace, setTrace] = useState<SessionTraceItem[]>([])
  const [expandedTraceRun, setExpandedTraceRun] = useState<string | null>(null)
  const [view, setView] = useState<View>('chat')
  const [sidebar, setSidebar] = useState(false)
  const [sessionsCollapsed, setSessionsCollapsed] = useState(() => {
    try { return localStorage.getItem('mini-agent:sessions-collapsed') === '1' } catch { return false }
  })
  const [error, setError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const selectedRef = useRef(selected)
  const eventSourceRef = useRef<EventSource | null>(null)
  // 历史加载的代次：发送消息时递增，作废仍在飞行中的加载，避免它用旧快照覆盖本轮乐观更新的消息。
  const messagesLoadRef = useRef(0)

  // 派生状态：当前会话对象，以及“是否正在处理中”。
  const current = sessions.find(item => item.id === selected)
  const busy = Boolean(run && !TERMINAL_STATUSES.has(run.status))

  // —— 4.2 数据加载与会话操作 ——
  // 切换会话：关闭旧 SSE 订阅、清空所有与会话绑定的状态，并把选中项同步到地址栏（?session=），
  // 以便刷新页面后仍停留在同一个会话。
  const selectSession = (id: string) => {
    eventSourceRef.current?.close(); eventSourceRef.current = null; selectedRef.current = id
    setSelected(id); setSidebar(false); setMessages([]); setRun(null); setRuns([]); setTrace([]); setExpandedTraceRun(null); setView('chat'); setError('')
    const url = new URL(location.href)
    if (id) url.searchParams.set('session', id)
    else url.searchParams.delete('session')
    history.replaceState(null, '', url)
  }
  // 拉取模型列表与会话列表；若当前没有选中会话，则默认选中第一个。
  const loadSessions = async () => {
    const [nextModels, nextSessions] = await Promise.all([api<Model[]>('/api/models'), api<Session[]>('/api/sessions')])
    setModels(nextModels); setSessions(nextSessions)
    if (!selectedRef.current && nextSessions[0]) selectSession(nextSessions[0].id)
  }
  /**
   * 加载指定会话的历史消息。
   * messagesLoadRef 是“加载代次”：每次调用自增；只有代次未被后续调用刷新、且会话未切换时，
   * 才允许用服务端快照覆盖本地状态。这样可避免“发送新消息后，更早发出的历史请求晚到并抹掉新消息”。
   */
  const loadMessages = async (id: string) => {
    if (!id) return
    const generation = ++messagesLoadRef.current
    const nextMessages = await api<Message[]>(`/api/sessions/${id}/messages`)
    // 只有当会话没切换、且期间没有发生新的加载或发送时，才允许用服务端历史覆盖本地状态。
    if (selectedRef.current !== id || generation !== messagesLoadRef.current) return
    setMessages(nextMessages)
  }
  // 加载会话的运行列表，用于执行日志的轮次数与标题。
  const loadRuns = async (id: string) => {
    const nextRuns = await api<Run[]>(`/api/sessions/${id}/runs`)
    if (selectedRef.current === id) setRuns(nextRuns)
  }
  // 加载整会话的轨迹事件（首参保留 runId 以兼容调用方签名，实际按 sessionId 拉取全量）。
  const loadTrace = async (_runId: string, sessionId: string) => {
    const nextTrace = await api<SessionTraceItem[]>(`/api/sessions/${sessionId}/trace`)
    if (selectedRef.current === sessionId) {
      setTrace(nextTrace)
    }
  }
  /**
   * 订阅某次运行的 SSE 事件流：
   *  - snapshot：运行整体状态快照。进入终态后关闭连接，并做一次全量刷新（消息 / 运行 / 轨迹）；
   *  - message ：助手回答按 seq 分片推送。这里严格按 seq 定位消息，绝不“猜测最后一条助手消息”，
   *              否则分片会被错误地写到上一条回答上。
   */
  const watchRun = (runId: string, sessionId: string) => {
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/runs/${runId}/events`)
    eventSourceRef.current = source
    source.addEventListener('snapshot', async event => {
      if (selectedRef.current !== sessionId) { source.close(); return }
      const snapshot = JSON.parse((event as MessageEvent).data) as Run
      setRun(snapshot)
      if (view === 'trace') loadTrace(runId, sessionId).catch(e => setError(e.message))
      if (TERMINAL_STATUSES.has(snapshot.status)) {
        source.close()
        if (eventSourceRef.current === source) eventSourceRef.current = null
        if (snapshot.error?.code && snapshot.status !== 'cancelled') setError(errorMessage(snapshot.error.code))
        try {
          await Promise.all([loadMessages(sessionId), loadRuns(sessionId), loadTrace(runId, sessionId)])
        }
        catch (e) { if (selectedRef.current === sessionId) setError((e as Error).message) }
      }
    })
    source.addEventListener('message', event => {
      if (selectedRef.current !== sessionId) return
      const payload = JSON.parse((event as MessageEvent).data) as { seq?: number; content?: string; thinking?: string }
      const seq = payload.seq
      // 只更新服务端指明的那条消息；不做“最后一条助手消息”的猜测，否则会把上一条回答覆盖掉。
      if (typeof seq !== 'number') return
      const content = payload.content || ''
      const thinking = payload.thinking || ''
      setMessages(current => {
        const index = current.findIndex(item => item.seq === seq)
        if (index < 0) return [...current, { role: 'assistant', content, thinking, seq }]
        return current.map((item, itemIndex) => itemIndex === index ? { ...item, content, thinking: thinking || item.thinking } : item)
      })
    })
  }
  // 刷新页面后恢复运行：若最新运行尚未结束则重新接上 SSE，并回填错误提示与轨迹。
  const restoreRun = async (id: string) => {
    const latest = await api<Run | null>(`/api/sessions/${id}/runs/latest`)
    if (selectedRef.current !== id) return
    setRun(latest)
    if (latest && !TERMINAL_STATUSES.has(latest.status)) watchRun(latest.id, id)
    if (latest) await loadTrace(latest.id, id)
    if (latest?.error?.code && latest.status !== 'cancelled') setError(errorMessage(latest.error.code))
  }

  // —— 4.3 副作用 ——
  // 首次挂载：加载模型与会话列表。
  useEffect(() => { loadSessions().catch(e => setError(e.message)) }, [])
  // 切换会话：并行加载消息、运行列表与运行状态。
  useEffect(() => { if (selected) Promise.all([loadMessages(selected), loadRuns(selected), restoreRun(selected)]).catch(e => setError(e.message)) }, [selected])
  // 问答视图下，消息或运行变化时自动滚到底部。
  useEffect(() => { if (view === 'chat') endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, run, view])
  // 组件卸载时关闭 SSE，避免连接泄漏。
  useEffect(() => () => eventSourceRef.current?.close(), [])
  // 持久化“会话列表折叠”偏好。
  useEffect(() => {
    try { localStorage.setItem('mini-agent:sessions-collapsed', sessionsCollapsed ? '1' : '0') } catch {}
  }, [sessionsCollapsed])

  // —— 4.4 交互动作 ——
  // 新建会话并立即切换过去；未配置模型时给出明确提示。
  const createSession = async () => {
    if (!models[0]) { setError(errorMessage('model_not_configured')); return }
    try {
      const item = await api<Session>('/api/sessions', { method: 'POST', body: JSON.stringify({ model_name: models[0].name }) })
      setSessions(old => [item, ...old]); selectSession(item.id)
    } catch (e) { setError((e as Error).message) }
  }
  // 切换当前会话使用的模型；PATCH 成功后重新拉取会话列表以同步展示。
  const changeModel = async (modelName: string) => {
    if (!selected) return
    try {
      await api(`/api/sessions/${selected}`, { method: 'PATCH', body: JSON.stringify({ model_name: modelName }) })
      await loadSessions()
    } catch (e) { setError((e as Error).message) }
  }
  // 删除会话：二次确认 → 调后端 → 本地移除；若删的是当前会话，则自动切到剩余列表的第一个。
  const deleteSession = async (item: Session) => {
    const label = item.title || '新会话'
    if (!window.confirm(`确定删除会话「${label}」吗？该操作不可恢复。`)) return
    try {
      await api(`/api/sessions/${item.id}`, { method: 'DELETE' })
      const remaining = sessions.filter(s => s.id !== item.id)
      setSessions(remaining)
      if (item.id === selected) selectSession(remaining[0]?.id || '')
    } catch (e) { setError((e as Error).message) }
  }
  /**
   * 发送消息：
   *  1. 先调后端创建运行，拿到 run_id；
   *  2. 自增 messagesLoadRef，作废仍在飞行中的历史加载，避免它用旧快照覆盖本轮乐观更新；
   *  3. 乐观地把用户消息追加到本地，再通过 watchRun 订阅回答分片。
   */
  const send = async () => {
    const message = draft.trim(); if (!selected || !message || busy) return
    const sessionId = selected; setError('')
    try {
      const result = await api<{run_id: string}>(`/api/sessions/${sessionId}/runs`, { method: 'POST', body: JSON.stringify({ message, request_key: crypto.randomUUID() }) })
      if (selectedRef.current !== sessionId) return
      // 作废可能仍在飞行中的历史加载，否则它返回的空快照会把刚发出的这条消息抹掉。
      messagesLoadRef.current += 1
      setDraft(''); setMessages(old => [...old, { role: 'user', content: message, seq: Date.now() }]); setRun({ id: result.run_id, status: 'running' }); watchRun(result.run_id, sessionId)
    } catch (e) { setError((e as Error).message) }
  }
  // 请求停止当前运行（后端异步取消，最终状态仍由 SSE 的终态通知前端）。
  const cancel = async () => { if (run) await api(`/api/runs/${run.id}/cancel`, { method: 'POST' }).catch(e => setError(e.message)) }
  // —— 4.5 渲染 ——
  // 布局：左侧固定侧栏（品牌 / 新建 / 模块切换 / 会话列表）+ 右侧工作区（问答视图 或 执行日志视图）。
  return <div className="shell">
    {/* 左侧栏：品牌、新建会话、模块导航、会话列表（可折叠）、本机模式标识 */}
    <aside className={sidebar ? 'sidebar open' : 'sidebar'}>
      <div className="brand"><span className="brand-mark"><Bot size={17}/></span><strong>Mini Agent</strong><button className="icon mobile" onClick={() => setSidebar(false)} title="关闭"><X size={18}/></button></div>
      <button className="new-session" onClick={createSession}><Plus size={17}/>新建会话</button>
      <nav className="module-nav" aria-label="功能模块">
        <button className={`module-item ${view === 'chat' ? 'active' : ''}`} onClick={() => { setView('chat'); setSidebar(false) }}><MessageSquare size={16}/><span>问答</span></button>
        <button className={`module-item ${view === 'trace' ? 'active' : ''}`} onClick={() => { setView('trace'); setSidebar(false); if (selected) loadTrace('', selected).catch(e => setError(e.message)) }} disabled={!selected}><ListTree size={16}/><span>执行日志</span>{runs.length > 0 && <b className="module-count">{runs.length}</b>}</button>
      </nav>
      <div className={`sidebar-label sessions-label ${sessionsCollapsed ? 'collapsed' : ''}`}>
        <button className="session-toggle" onClick={() => setSessionsCollapsed(c => !c)} aria-expanded={!sessionsCollapsed} aria-controls="session-list" title={sessionsCollapsed ? '展开会话列表' : '收起会话列表'}>
          <ChevronDown size={14}/><span>会话</span><em>{sessions.length}</em>
        </button>
      </div>
      {!sessionsCollapsed && <nav className="session-nav" id="session-list" aria-label="会话列表">
        {sessions.map(item => <div className={`session-item ${item.id === selected ? 'active' : ''}`} key={item.id}>
          <button className="session-select" onClick={() => selectSession(item.id)}>
            <MessageSquare size={16}/><span><b>{item.title || '新会话'}</b><small>{item.model_name}</small></span>
          </button>
          <button className="session-delete" onClick={() => deleteSession(item)} title="删除会话" aria-label="删除会话"><Trash2 size={14}/></button>
        </div>)}
      </nav>}
      <div className="local-badge"><span/>本机模式</div>
    </aside>
    {/* 右侧工作区：页头 + 内容区（问答 / 执行日志）+ 错误条 + 输入区 */}
    <main className={`workspace ${view === 'trace' ? 'trace-workspace' : ''}`}>
      {/* 页头：当前会话标题与视图名称 */}
      <header>
        <button className="icon mobile" onClick={() => setSidebar(true)} title="打开会话"><Menu size={19}/></button>
        <div className="session-heading"><h1>{view === 'trace' ? '执行日志' : current ? (current.title || '新对话') : 'Mini Agent'}</h1><p>{view === 'trace' ? '当前会话的全部执行链路' : current ? `会话 ${current.id.slice(0, 8)}` : '等待创建会话'}</p></div>
      </header>

      {/* 问答视图：消息流（用户气泡 / 助手回答 / 思考过程 / 工具调用标记）+ 运行指示器 */}
      {view === 'chat' && <section className="conversation">
        {!selected && <div className="empty"><span><Bot size={25}/></span><h2>Mini Agent</h2><p>创建一个会话开始。</p><button onClick={createSession}><Plus size={17}/>新建会话</button></div>}
        {selected && (() => {
          const visible = messages.filter(item => item.role !== 'tool' && (item.content || item.thinking || (item.tool_calls && item.tool_calls.length)))
          if (!visible.length) return <div className="empty"><span><MessageSquare size={24}/></span><h2>有什么可以帮你？</h2><p>发送问题，Agent 会自主判断是否调用工具。</p></div>
          return visible.map(message => {
            const toolCalls = message.tool_calls
            const isToolCall = !!(toolCalls && toolCalls.length)
            const thinking = message.thinking || (isToolCall ? (message.content || '') : '')
            const answer = isToolCall ? '' : (message.content || '')
            const toolCount = toolCalls?.length ?? 0
            return <article key={`${message.seq}-${message.role}`} className={`message ${message.role}`}>
              <div className="role">{message.role === 'user' ? '你' : 'Agent'}</div>
              {message.role === 'assistant' && thinking && <details className="thinking"><summary><BrainCircuit size={12}/><span>思考过程</span><em>决策摘要</em></summary><p>{thinking}</p></details>}
              {isToolCall && <div className="tool-calls"><Wrench size={13}/>调用了 {toolCount} 个工具</div>}
              {answer && <div className="bubble" dangerouslySetInnerHTML={message.role === 'assistant' ? {__html: markdownHtml(answer)} : undefined}>{message.role === 'user' ? answer : undefined}</div>}
            </article>
          })
        })()}
        {busy && <div className="running"><span/><span/><span/><em>{run?.status === 'cancel_requested' ? '正在停止' : '正在处理'}</em></div>}
        <div ref={endRef}/>
      </section>}
      {/* 执行日志视图：按运行轮次分组的步骤时间线 */}
      {view === 'trace' && <section className="trace-page">
        <div className="trace-header"><div><h2>执行日志</h2><p>按问题查看 Agent 的完整处理步骤、工具调用和模型响应。</p></div><span className="trace-count">{runs.length}</span></div>
        {!selected && <div className="trace-empty"><ListTree size={26}/><h3>请先选择会话</h3><p>选择左侧会话后查看执行链路。</p></div>}
        {selected && !trace.length && <div className="trace-empty"><ListTree size={26}/><h3>暂无执行记录</h3><p>发送问题后，这里会显示 Agent 的处理步骤。</p></div>}
        {selected && trace.length > 0 && <TraceGroups trace={trace} runs={runs} expandedRunId={expandedTraceRun} onToggle={id => setExpandedTraceRun(current => current === id ? null : id)}/>} 
      </section>}
      {error && <div className="error-bar"><AlertCircle size={16}/><span>{error}</span><button className="icon" onClick={() => setError('')} title="关闭提示"><X size={15}/></button></div>}
      {/* 输入区：Enter 发送 / Shift+Enter 换行；运行中时发送键切换为停止键 */}
      {view === 'chat' && <div className="composer-wrap"><div className="composer">
        <textarea value={draft} onChange={e => setDraft(e.target.value)} placeholder="请输入您想要咨询的问题..." disabled={!selected} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }}/>
        <div className="composer-actions">
          {current && <label className={`composer-model ${models.length === 1 ? 'fixed' : ''}`}><span className="model-glyph"><Bot size={14}/></span><span className="model-copy"><strong>{current.model_name}</strong><small>{models.length === 1 ? '当前模型' : '选择模型'}</small></span>{models.length > 1 ? <><ChevronDown size={14}/><select aria-label="选择模型" value={current.model_name} disabled={busy} onChange={e => changeModel(e.target.value)}>{models.map(model => <option key={model.name} value={model.name}>{model.name}</option>)}</select></> : <span className="model-fixed-dot" aria-hidden="true"/>}</label>}
          {busy ? <button className="stop" onClick={cancel} title="停止"><Square size={14}/></button> : <button className="send" onClick={send} disabled={!selected || !draft.trim()} title="发送"><Send size={17}/></button>}
        </div>
      </div></div>}
    </main>
  </div>
}
