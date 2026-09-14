import { useEffect, useRef, useState } from 'react'
import {
  AlertCircle, Bot, BrainCircuit, CheckCircle2, ChevronDown, CircleDashed,
  ListTree, Menu, MessageSquare, Plus, Send, Square, Trash2, Wrench, X,
} from 'lucide-react'

type Model = { name: string; model: string; mode: string; context_window: number }
type Session = { id: string; model_name: string; timezone: string; title: string | null; created_at: string }
type Message = { role: string; content?: string; thinking?: string; seq: number; tool_calls?: unknown[]; name?: string }
type Run = { id: string; status: string; answer?: string; input_preview?: string; error?: { code: string }; model_calls?: number; created_at?: string; finished_at?: string }
type TraceItem = { id: number; event_type: string; created_at: string; payload: Record<string, unknown> }
type SessionTraceItem = TraceItem & { run_id: string }
type View = 'chat' | 'trace'

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'limit_reached', 'interrupted'])
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
const TOOL_NAMES: Record<string, string> = {
  calculator: '计算器', search: '搜索', weather: '天气', todo: '待办',
  resource_read: '资源读取', resource_search: '资源查找',
}
const STATUS_NAMES: Record<string, string> = {
  running: '运行中', completed: '已完成', failed: '失败', cancelled: '已停止',
  limit_reached: '达到上限', interrupted: '已中断', cancel_requested: '正在停止',
}

const errorMessage = (code: string) => ERROR_MESSAGES[code] || '请求失败，请稍后重试'

const detailMessage = (code: unknown) => ERROR_MESSAGES[String(code || '')] || '执行过程中出现问题，请稍后重试'

const escapeHtml = (value: string) => value.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]!))

const inlineMarkdown = (value: string) => escapeHtml(value).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')

const tableCells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())

const isTableDivider = (line: string) => {
  const cells = tableCells(line)
  return cells.length > 0 && cells.every(cell => /^:?-{2,}:?$/.test(cell))
}

const TABLE_TOKEN = '\u0001table:'

const renderTable = (lines: string[]) => {
  const cell = (name: 'th' | 'td', text: string) => `<${name}>${inlineMarkdown(text)}</${name}>`
  const head = tableCells(lines[0]).map(value => cell('th', value)).join('')
  const body = lines.slice(2).map(row => `<tr>${tableCells(row).map(value => cell('td', value)).join('')}</tr>`).join('')
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
}

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

const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const code = body.detail?.code || `http_${response.status}`
    throw new Error(body.detail?.message || errorMessage(code))
  }
  return response.json()
}

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

type TraceGroupsProps = {
  trace: SessionTraceItem[]
  runs: Run[]
  expandedRunId: string | null
  onToggle: (runId: string) => void
}

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

export default function App() {
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

  const current = sessions.find(item => item.id === selected)
  const busy = Boolean(run && !TERMINAL_STATUSES.has(run.status))

  const selectSession = (id: string) => {
    eventSourceRef.current?.close(); eventSourceRef.current = null; selectedRef.current = id
    setSelected(id); setSidebar(false); setMessages([]); setRun(null); setRuns([]); setTrace([]); setExpandedTraceRun(null); setView('chat'); setError('')
    const url = new URL(location.href)
    if (id) url.searchParams.set('session', id)
    else url.searchParams.delete('session')
    history.replaceState(null, '', url)
  }
  const loadSessions = async () => {
    const [nextModels, nextSessions] = await Promise.all([api<Model[]>('/api/models'), api<Session[]>('/api/sessions')])
    setModels(nextModels); setSessions(nextSessions)
    if (!selectedRef.current && nextSessions[0]) selectSession(nextSessions[0].id)
  }
  const loadMessages = async (id: string) => {
    if (!id) return
    const generation = ++messagesLoadRef.current
    const nextMessages = await api<Message[]>(`/api/sessions/${id}/messages`)
    // 只有当会话没切换、且期间没有发生新的加载或发送时，才允许用服务端历史覆盖本地状态。
    if (selectedRef.current !== id || generation !== messagesLoadRef.current) return
    setMessages(nextMessages)
  }
  const loadRuns = async (id: string) => {
    const nextRuns = await api<Run[]>(`/api/sessions/${id}/runs`)
    if (selectedRef.current === id) setRuns(nextRuns)
  }
  const loadTrace = async (_runId: string, sessionId: string) => {
    const nextTrace = await api<SessionTraceItem[]>(`/api/sessions/${sessionId}/trace`)
    if (selectedRef.current === sessionId) {
      setTrace(nextTrace)
    }
  }
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
  const restoreRun = async (id: string) => {
    const latest = await api<Run | null>(`/api/sessions/${id}/runs/latest`)
    if (selectedRef.current !== id) return
    setRun(latest)
    if (latest && !TERMINAL_STATUSES.has(latest.status)) watchRun(latest.id, id)
    if (latest) await loadTrace(latest.id, id)
    if (latest?.error?.code && latest.status !== 'cancelled') setError(errorMessage(latest.error.code))
  }

  useEffect(() => { loadSessions().catch(e => setError(e.message)) }, [])
  useEffect(() => { if (selected) Promise.all([loadMessages(selected), loadRuns(selected), restoreRun(selected)]).catch(e => setError(e.message)) }, [selected])
  useEffect(() => { if (view === 'chat') endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, run, view])
  useEffect(() => () => eventSourceRef.current?.close(), [])
  useEffect(() => {
    try { localStorage.setItem('mini-agent:sessions-collapsed', sessionsCollapsed ? '1' : '0') } catch {}
  }, [sessionsCollapsed])

  const createSession = async () => {
    if (!models[0]) { setError(errorMessage('model_not_configured')); return }
    try {
      const item = await api<Session>('/api/sessions', { method: 'POST', body: JSON.stringify({ model_name: models[0].name }) })
      setSessions(old => [item, ...old]); selectSession(item.id)
    } catch (e) { setError((e as Error).message) }
  }
  const changeModel = async (modelName: string) => {
    if (!selected) return
    try {
      await api(`/api/sessions/${selected}`, { method: 'PATCH', body: JSON.stringify({ model_name: modelName }) })
      await loadSessions()
    } catch (e) { setError((e as Error).message) }
  }
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
  const cancel = async () => { if (run) await api(`/api/runs/${run.id}/cancel`, { method: 'POST' }).catch(e => setError(e.message)) }
  return <div className="shell">
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
    <main className={`workspace ${view === 'trace' ? 'trace-workspace' : ''}`}>
      <header>
        <button className="icon mobile" onClick={() => setSidebar(true)} title="打开会话"><Menu size={19}/></button>
        <div className="session-heading"><h1>{view === 'trace' ? '执行日志' : current ? (current.title || '新对话') : 'Mini Agent'}</h1><p>{view === 'trace' ? '当前会话的全部执行链路' : current ? `会话 ${current.id.slice(0, 8)}` : '等待创建会话'}</p></div>
      </header>

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
      {view === 'trace' && <section className="trace-page">
        <div className="trace-header"><div><h2>执行日志</h2><p>按问题查看 Agent 的完整处理步骤、工具调用和模型响应。</p></div><span className="trace-count">{runs.length}</span></div>
        {!selected && <div className="trace-empty"><ListTree size={26}/><h3>请先选择会话</h3><p>选择左侧会话后查看执行链路。</p></div>}
        {selected && !trace.length && <div className="trace-empty"><ListTree size={26}/><h3>暂无执行记录</h3><p>发送问题后，这里会显示 Agent 的处理步骤。</p></div>}
        {selected && trace.length > 0 && <TraceGroups trace={trace} runs={runs} expandedRunId={expandedTraceRun} onToggle={id => setExpandedTraceRun(current => current === id ? null : id)}/>} 
      </section>}
      {error && <div className="error-bar"><AlertCircle size={16}/><span>{error}</span><button className="icon" onClick={() => setError('')} title="关闭提示"><X size={15}/></button></div>}
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
