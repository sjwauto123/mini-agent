/**
 * Mini Agent 前端主应用（单文件 React 组件）。
 *
 * 文件结构：
 *   1. 类型与常量     —— 与后端接口对齐的数据形状，以及错误码 / 工具 / 状态的中文映射
 *   2. 渲染工具函数   —— 轻量 Markdown 渲染、统一 fetch 封装、运行事件文案
 *   3. TraceTimeline  —— “执行日志”视图：把一次会话的所有问答串成一条带层级的连续链路
 *   4. App            —— 会话 / 消息 / 运行 / 轨迹的状态机 + SSE 订阅 + 全部交互
 *
 * 约束：不引入路由与全局状态库，所有状态集中由 App 组件持有并向下传递。
 */
import { useEffect, useRef, useState } from 'react'
import {
  AlertCircle, Bot, BrainCircuit, CheckCircle2, ChevronDown, CircleDashed,
  ListTree, Menu, MessageSquare, Plus, RefreshCw, Scissors, Send, Square, Trash2, Wrench, X,
} from 'lucide-react'

// —— 1. 类型与常量 ——
// 以下类型与 backend 的接口返回逐一对应，字段变更需同步后端契约。
type Model = { name: string; model: string; mode: string; context_window: number }
type Session = { id: string; model_name: string; timezone: string; title: string | null; created_at: string }
type Message = { role: string; content?: string; thinking?: string; seq: number; tool_calls?: unknown[]; name?: string; run_id?: string }
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
  model_auth_failed: '模型服务拒绝了本次请求，请检查服务端密钥与权限',
  model_request_invalid: '模型服务拒绝了请求参数，请检查模型配置',
  model_call_limit: '本次运行已达到模型调用次数上限',
  model_context_budget_invalid: '模型上下文配置无效',
  model_not_configured: '模型未配置',
  model_protocol_error: '模型返回了无法解析的响应',
  model_unavailable: '模型服务暂时不可用，请检查网络连接后重试',
  run_not_found: '运行记录不存在',
  run_timeout: '本次运行超时',
  service_restarted: '服务重启导致运行中断',
  session_busy: '当前会话正在处理其他消息',
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

/**
 * 判断推理与正文是否在承载同一段文字。
 *
 * 模型对工具轮常把决策说明同时写进 reasoning_content 与 content。流式期间两者是同一段话的
 * 不同进度，且尾部会各自发散（实测公共前缀占较短一方 80% 以上，但不一定严格互为前缀），
 * 所以用"公共前缀占比"判定而不是全等或严格前缀——后两者在流式途中都会漏判。
 * 比较前去掉空白，避免换行差异影响判定。
 */
const sharesText = (a?: string, b?: string): boolean => {
  const left = (a || '').replace(/\s+/g, '')
  const right = (b || '').replace(/\s+/g, '')
  const shorter = Math.min(left.length, right.length)
  if (shorter < 12) return false
  if (left === right) return true
  let same = 0
  while (same < shorter && left[same] === right[same]) same += 1
  return same >= shorter * .8
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

/**
 * 取出事件里的原始错误详情（后端截 300 字）拼成一句附注。
 *
 * 错误码是归一化后的结论，详情才是“上游到底说了什么”——重试与失败事件都带着它，
 * 只显示错误码会让排障不得不展开原始 JSON。这里做单行限长，避免把执行日志撑成一堵墙；
 * 需要完整原文时仍可在节点的“查看事件数据”里展开。
 */
const detailSnippet = (value: unknown, limit = 120) => {
  const text = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
  return text ? ` 详情：${text.length > limit ? `${text.slice(0, limit)}…` : text}` : ''
}

/**
 * 拼“原因：xxx”片段：只有错误码能映射成具体中文时才输出。
 *
 * 映射不到时（httpx 的 RemoteProtocolError、模型层的 empty_response 等）通用句回答不了
 * “为什么”，而它后面紧跟着详情原文——两句话并存只是白占一行。但若这一次压根没有详情可看，
 * 就退回通用句，至少让用户知道出错了。
 */
const reasonClause = (code: unknown, detail: unknown) => {
  const known = ERROR_MESSAGES[String(code || '')]
  if (known) return ` 原因：${known}`
  return code && !detail ? ` 原因：${detailMessage(code)}` : ''
}

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
  // 裁剪事件才有 token 数值：两个字段都齐全才拼成备注，避免出现"约 undefined / undefined tokens"。
  const tokens = typeof payload.estimated_tokens === 'number' && typeof payload.input_budget === 'number' ? `约 ${payload.estimated_tokens} / ${payload.input_budget} tokens` : ''
  // 模型调用事件的统一元信息：耗时 + 首字延迟 + token 用量。哪个字段缺失就跳过哪个——
  // 上游没回 usage 时（没开 stream_options）就不显示，而不是显示 0。
  const callMeta = (() => {
    const parts = [duration]
    if (typeof payload.ttft_ms === 'number') parts.push(`首字 ${Math.round(payload.ttft_ms)} 毫秒`)
    if (typeof payload.input_tokens === 'number' && typeof payload.output_tokens === 'number') {
      parts.push(`${payload.input_tokens} / ${payload.output_tokens} tokens`)
    }
    return parts.filter(Boolean).join(' · ')
  })()
  // 协议修复事件的响应头：finish_reason 与 content_len 合看才能区分“被上游截断”与“模型真的只回了空白”。
  // 后端专门记了这几个字段，展示层不该丢掉——否则看到 code 也不知道这次修复值不值得追究。
  const invalidHead = (() => {
    const output = (payload.output ?? {}) as { finish_reason?: unknown; content_len?: unknown; reasoning_len?: unknown }
    return [
      output.finish_reason ? `finish_reason=${output.finish_reason}` : '',
      typeof output.content_len === 'number' ? `正文 ${output.content_len} 字` : '',
      typeof output.reasoning_len === 'number' && output.reasoning_len > 0 ? `推理 ${output.reasoning_len} 字` : '',
    ].filter(Boolean).join(' · ')
  })()
  switch (item.event_type) {
    case 'run.started': return { icon: CircleDashed, tone: 'active', title: '开始运行', description: '已接收用户消息，Agent 开始处理。', meta: '' }
    case 'model.started': return { icon: BrainCircuit, tone: 'active', title: payload.phase === 'summary' ? '压缩上下文' : '请求模型', description: `${iteration || '当前轮次'}，模型正在判断直接回答还是调用工具。${payload.tool_choice === 'none' ? '本轮已禁用工具（只剩最后一次调用机会），模型必须直接给出回答。' : ''}`, meta: `第 ${payload.attempt || 1} 次尝试` }
    case 'model.finished': return { icon: CheckCircle2, tone: 'success', title: payload.phase === 'summary' ? '上下文压缩完成' : '模型响应完成', description: '模型已返回可处理的响应。', meta: callMeta }
    case 'model.retry': return { icon: AlertCircle, tone: 'warning', title: '模型请求重试', description: `请求未成功，正在自动重试。${reasonClause(payload.code, payload.detail)}${detailSnippet(payload.detail)}`, meta: [`第 ${payload.attempt || 1} 次尝试`, duration].filter(Boolean).join(' · ') }
    case 'model.failed': return { icon: AlertCircle, tone: 'danger', title: '模型请求失败', description: `模型服务请求失败。${reasonClause(payload.code, payload.detail)}${detailSnippet(payload.detail)}`, meta: duration }
    case 'model.invalid': return { icon: AlertCircle, tone: 'warning', title: '模型响应格式修复', description: `响应格式不符合协议，Agent 将要求模型重新返回。${reasonClause(payload.code, payload.reason)}${detailSnippet(payload.reason)}`, meta: [iteration, invalidHead].filter(Boolean).join(' · ') }
    case 'model.repair': return { icon: RefreshCw, tone: 'warning', title: '重发请求以修复格式', description: payload.minimal_context ? '已改用最小上下文重发：同一上下文会复现相同错误。' : '保持当前上下文原样重发一次。', meta: `第 ${payload.repairs || 1} 次修复` }
    // 字数优先取 payload.chars（最终回答落库后写入的权威长度）；早期版本把正文整段写进
    // payload.content（流式期间分片落多条、complete 可能为 false），这里保留兼容，
    // 否则历史会话的这条事件会完全没有字数可看。
    case 'assistant.delta': return { icon: Send, tone: 'active', title: payload.complete ? '回答输出完成' : '输出回答片段', description: 'Agent 正在把最终回答分片推送给前端。', meta: typeof payload.chars === 'number' ? `共 ${payload.chars} 字` : typeof payload.content === 'string' ? `${payload.content.length} 字` : '' }
    case 'tool.started': return { icon: Wrench, tone: 'active', title: `调用工具：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: '模型选择了工具，正在执行并等待结果。', meta: iteration }
    case 'tool.finished': return { icon: payload.ok ? CheckCircle2 : AlertCircle, tone: payload.ok ? 'success' : 'danger', title: `工具${payload.ok ? '执行完成' : '执行失败'}：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: payload.ok ? '工具结果已写入上下文，Agent 将继续判断下一步。' : `工具返回错误：${detailMessage(payload.error_code)}`, meta: duration }
    case 'tool.reused': return { icon: Wrench, tone: 'success', title: `复用工具结果：${TOOL_NAMES[String(payload.name)] || payload.name}`, description: '检测到相同调用，直接使用已保存的结果。', meta: iteration }
    case 'context.compacted': return { icon: ListTree, tone: 'success', title: '历史上下文已压缩', description: '较早的对话已整理为摘要，为后续问答释放上下文空间。', meta: `压缩至消息 ${payload.covered_through_seq}` }
    case 'context.compaction_failed': return { icon: AlertCircle, tone: 'warning', title: '上下文压缩未完成', description: '本次摘要生成失败，Agent 已使用可用历史继续运行。', meta: String(payload.code || '') }
    case 'context.trimmed': return { icon: Scissors, tone: 'warning', title: '上下文超出上限已裁剪', description: '为装下本次请求，较早且尚未摘要的对话被临时省略，模型可能记不起更早的内容。', meta: tokens }
    case 'message.write_failed': return { icon: AlertCircle, tone: 'danger', title: '回答保存失败', description: '最终回答未能写入消息表，会话记录可能不完整；运行记录里仍保留着完整回答。', meta: `消息 #${payload.seq}` }
    case 'run.finished': {
      const status = String(payload.status || '')
      const success = status === 'completed'
      return { icon: success ? CheckCircle2 : AlertCircle, tone: success ? 'success' : status === 'cancelled' ? 'warning' : 'danger', title: `运行${success ? '完成' : STATUS_NAMES[status] || '结束'}`, description: success ? '最终回答已生成并保存到当前会话。' : errorMessage(String(payload.code || status)), meta: '' }
    }
    default: return { icon: CircleDashed, tone: 'neutral', title: item.event_type, description: 'Agent 记录了一条运行事件。', meta: '' }
  }
}

// —— 3. 执行日志视图 ——
// 把一次会话的全部问答链路串成一条连续的层级时间线：每轮以用户提问开头、按 parent_id 还原
// 父子关系把事件排成两级缩进、以最终回答收尾。默认全展开，单击轮次头折叠该轮。
type TraceTimelineProps = {
  trace: SessionTraceItem[]
  runs: Run[]
  messages: Message[]
  collapsedRuns: Set<string>
  onToggle: (runId: string) => void
}

// 把同一 run 的扁平事件按 parent_id 还原成两级树，展平成带深度的列表。父级缺失/非数字
// 的事件当作顶层（model.started / run.* 都是这种情况）。
const buildTraceTree = (events: SessionTraceItem[]): { item: SessionTraceItem; depth: number }[] => {
  const children = new Map<number, SessionTraceItem[]>()
  const roots: SessionTraceItem[] = []
  events.forEach(event => {
    const pid = (event.payload as { parent_id?: number }).parent_id
    if (typeof pid === 'number') {
      const arr = children.get(pid) ?? []
      arr.push(event)
      children.set(pid, arr)
    } else {
      roots.push(event)
    }
  })
  const out: { item: SessionTraceItem; depth: number }[] = []
  const dfs = (node: SessionTraceItem, depth: number) => {
    out.push({ item: node, depth })
    const kids = children.get(Number(node.id)) ?? []
    kids.sort((a, b) => a.id - b.id).forEach(child => dfs(child, depth + 1))
  }
  roots.sort((a, b) => a.id - b.id).forEach(root => dfs(root, 0))
  return out
}

// 把该 run 里所有 model.finished 的 token 用量相加，得到这一轮的总成本。
const sumModelTokens = (events: SessionTraceItem[]): { input: number; output: number; has: boolean } => {
  let input = 0
  let output = 0
  events.forEach(event => {
    const payload = event.payload as { input_tokens?: number; output_tokens?: number }
    if (typeof payload.input_tokens === 'number') input += payload.input_tokens
    if (typeof payload.output_tokens === 'number') output += payload.output_tokens
  })
  return { input, output, has: input > 0 || output > 0 }
}

const TraceTimeline = ({ trace, runs, messages, collapsedRuns, onToggle }: TraceTimelineProps) => {
  const sortedRuns = [...runs].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
  return <div className="trace-flow">
    {sortedRuns.map((run, index) => {
      const events = trace.filter(event => event.run_id === run.id).sort((a, b) => a.id - b.id)
      // 用户提问：取这一 run 第一条用户消息（按 seq 最小即可，filter 后顺序已稳定）。
      const userQuestion = messages.find(message => message.run_id === run.id && message.role === 'user')?.content
      // 最终回答：取这一 run 最后一条无 tool_calls 的助手消息——tool_calls 轮只是决策说明，不是答案。
      const finalAnswer = [...messages].reverse().find(message => message.run_id === run.id && message.role === 'assistant' && !message.tool_calls)?.content
      const flat = buildTraceTree(events)
      const tokens = sumModelTokens(events)
      const collapsed = collapsedRuns.has(run.id)
      const finishedMs = run.finished_at ? new Date(run.finished_at).getTime() : null
      const createdMs = run.created_at ? new Date(run.created_at).getTime() : null
      const totalMs = finishedMs !== null && createdMs !== null && finishedMs >= createdMs ? finishedMs - createdMs : null
      return <section className={`trace-turn ${collapsed ? 'collapsed' : 'expanded'}`} key={run.id}>
        <button className="trace-turn-toggle" onClick={() => onToggle(run.id)} aria-expanded={!collapsed}>
          <span className="trace-turn-kicker"><strong>第 {index + 1} 轮</strong>{run.created_at && <time>{new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}</time>}<span className={`run-status ${run.status}`}>{STATUS_NAMES[run.status] || '已完成'}</span></span>
          {userQuestion && <span className="trace-turn-question">{userQuestion}</span>}
          <span className="trace-turn-stats"><span>{events.length} 个步骤</span>{tokens.has && <span>{tokens.input} / {tokens.output} tokens</span>}{totalMs !== null && totalMs >= 0 && <span>{Math.round(totalMs)} 毫秒</span>}<ChevronDown size={14}/></span>
        </button>
        {!collapsed && <>
          <ol className="trace-list">
            {flat.map(({ item, depth }) => {
              const presentation = tracePresentation(item)
              const Icon = presentation.icon
              return <li key={item.id} className={`trace-step ${presentation.tone} depth-${depth}`} style={{ marginLeft: depth * 16 }}>
                <div className="trace-marker"><Icon size={15}/></div>
                <div className="trace-content">
                  <div className="trace-title"><strong>{presentation.title}</strong><time>{new Date(item.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</time></div>
                  <p>{presentation.description}</p>
                  {presentation.meta && <span className="trace-meta">{presentation.meta}</span>}
                  {Object.keys(item.payload).length > 0 && <details><summary>查看事件数据</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></details>}
                </div>
              </li>
            })}
          </ol>
          {finalAnswer && <div className="trace-turn-answer">
            <header><strong>回答</strong></header>
            <p>{finalAnswer}</p>
          </div>}
        </>}
      </section>
    })}
  </div>
}

// 思考过程面板：对齐主流产品的默认行为 —— 思考中自动展开并走计时，回答完成后自动折叠成一行。
// 推理是边生成边追加的，所以这里要自动贴底，用户不必手动追着滚。
const ThinkingPanel = ({ thinking, busy, seconds }: { thinking: string; busy: boolean; seconds: number | null }) => {
  const [open, setOpen] = useState(busy)
  const bodyRef = useRef<HTMLParagraphElement>(null)
  // 展开状态跟随"是否在思考"：开始思考时展开、结束后折叠；用户手动折叠后不会被强行再拉开。
  useEffect(() => { setOpen(busy) }, [busy])
  useEffect(() => {
    if (open && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [thinking, open])
  const meta = seconds === null ? '' : busy ? `已用 ${seconds} 秒` : `用时 ${seconds} 秒`
  return <details className={`thinking ${busy ? 'streaming' : ''}`} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><BrainCircuit size={12}/><span>{busy ? '思考中' : '思考过程'}</span>{meta && <em>{meta}</em>}</summary>
    <p ref={bodyRef}>{thinking}</p>
  </details>
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
  // 默认全展开；每轮可单独折叠。Set 比单一 id 更适合"同时多轮折叠、互不打架"的场景。
  const [collapsedRuns, setCollapsedRuns] = useState<Set<string>>(() => new Set())
  const [view, setView] = useState<View>('chat')
  const [sidebar, setSidebar] = useState(false)
  const [sessionsCollapsed, setSessionsCollapsed] = useState(() => {
    try { return localStorage.getItem('mini-agent:sessions-collapsed') === '1' } catch { return false }
  })
  const [error, setError] = useState('')
  // 过程性提示（目前只有"上游无响应，正在重试"）：它不是错误，只解释"为什么一直没有输出"。
  const [notice, setNotice] = useState('')
  const [clock, setClock] = useState(() => Date.now())
  const endRef = useRef<HTMLDivElement>(null)
  const selectedRef = useRef(selected)
  // 视图的实时值：SSE 回调是“发消息那一刻”创建的闭包，直接读 view 会一直停在旧值
  // （发消息只能在问答视图，于是运行期间切到执行日志也会被当成还在问答视图，轨迹不再刷新）。
  const viewRef = useRef(view)
  const eventSourceRef = useRef<EventSource | null>(null)
  // 历史加载的代次：发送消息时递增，作废仍在飞行中的加载，避免它用旧快照覆盖本轮乐观更新的消息。
  const messagesLoadRef = useRef(0)
  // 本轮运行的起点：思考面板的「已用 X 秒」由它和 clock 的差值算出。
  const runStartedAt = useRef<number | null>(null)

  // 派生状态：当前会话对象，以及“是否正在处理中”。
  const current = sessions.find(item => item.id === selected)
  const busy = Boolean(run && !TERMINAL_STATUSES.has(run.status))
  // 思考计时：运行期间每 200ms 走一次表；结束后不再更新，最后一条消息的「用时」便冻结在结束那一刻。
  const elapsedSeconds = runStartedAt.current === null ? null : Math.max(0, Math.round((clock - runStartedAt.current) / 1000))

  // —— 4.2 数据加载与会话操作 ——
  // 切换会话：关闭旧 SSE 订阅、清空所有与会话绑定的状态，并把选中项同步到地址栏（?session=），
  // 以便刷新页面后仍停留在同一个会话。
  const selectSession = (id: string) => {
    eventSourceRef.current?.close(); eventSourceRef.current = null; selectedRef.current = id; runStartedAt.current = null
    setSelected(id); setSidebar(false); setMessages([]); setRun(null); setRuns([]); setTrace([]); setCollapsedRuns(new Set()); setView('chat'); setError(''); setNotice('')
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
      // 把最新运行行并回列表：执行日志是按 runs 逐轮渲染的，这一轮不在列表里就画不出任何步骤
      // （运行期间切过去会看到一张空白页，直到终态整表刷新才出现）。快照与 /runs 列表同源，
      // 都出自后端的 public_run，字段形状一致，因此可以直接按 id 覆盖。
      setRuns(current => {
        const index = current.findIndex(item => item.id === snapshot.id)
        if (index < 0) return [...current, snapshot]
        return current.map(item => (item.id === snapshot.id ? snapshot : item))
      })
      // 运行已经结束（无论成败）就不该再挂着"正在重试"了。
      if (TERMINAL_STATUSES.has(snapshot.status)) setNotice('')
      if (viewRef.current === 'trace') loadTrace(runId, sessionId).catch(e => setError(e.message))
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
      const payload = JSON.parse((event as MessageEvent).data) as { seq?: number; content?: string; thinking?: string; tool_calls?: unknown[] }
      const seq = payload.seq
      // 只更新服务端指明的那条消息；不做“最后一条助手消息”的猜测，否则会把上一条回答覆盖掉。
      if (typeof seq !== 'number') return
      // 又有增量流出，说明上游恢复了：重试提示的使命结束。
      setNotice('')
      const content = payload.content || ''
      const thinking = payload.thinking || ''
      // tool_calls 只在本轮确定为工具调用时才随增量下发：收到就要立刻落进状态，
      // 否则这一轮的决策说明会一直被当成回答渲染（与思考面板内容重复），
      // 且"调用了 N 个工具"要等整轮结束才出现。
      const toolCalls = payload.tool_calls?.length ? payload.tool_calls : undefined
      setMessages(current => {
        const index = current.findIndex(item => item.seq === seq)
        if (index < 0) return [...current, { role: 'assistant', content, thinking, seq, tool_calls: toolCalls }]
        return current.map((item, itemIndex) => itemIndex === index
          ? { ...item, content, thinking: thinking || item.thinking, tool_calls: toolCalls ?? item.tool_calls }
          : item)
      })
    })
    // 过程性提示：上游挂起时数据库没有任何变化，快照与增量都推不出内容，
    // 没有这条提示界面就只是在"转圈"，看不出是在重试还是卡死。
    source.addEventListener('notice', event => {
      if (selectedRef.current !== sessionId) return
      const payload = JSON.parse((event as MessageEvent).data) as { code?: string; attempt?: number; delay_ms?: number }
      if (payload.code !== 'model_retry') return
      const seconds = Math.max(1, Math.round((payload.delay_ms ?? 0) / 1000))
      setNotice(`上游未响应，${seconds} 秒后自动重试（第 ${payload.attempt ?? 2} 次尝试）`)
    })
    source.addEventListener('discard', event => {
      if (selectedRef.current !== sessionId) return
      const payload = JSON.parse((event as MessageEvent).data) as { seq?: number }
      // 后端作废了这一轮的流式输出（协议非法，或重试要从头再流一遍）：把已经流出去的那半句撤掉，
      // 否则界面上会留下一条说了一半的幽灵回答。
      if (typeof payload.seq === 'number') setMessages(current => current.filter(item => item.seq !== payload.seq))
    })
  }
  // 刷新页面后恢复运行：若最新运行尚未结束则重新接上 SSE，并回填错误提示与轨迹。
  const restoreRun = async (id: string) => {
    const latest = await api<Run | null>(`/api/sessions/${id}/runs/latest`)
    if (selectedRef.current !== id) return
    setRun(latest)
    // 刷新页面后接上一个仍在进行的运行：计时从此刻起算（此前的时间拿不回来，只能按可见部分计时）。
    if (latest && !TERMINAL_STATUSES.has(latest.status)) { runStartedAt.current = Date.now(); watchRun(latest.id, id) }
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
  // 视图切换：同步视图的实时值（供 SSE 回调读取），并在进入执行日志视图时拉一次最新轨迹。
  // 拉取只放在这里：按钮只负责切视图，避免“进入视图”这件事有两个入口各拉一次。
  useEffect(() => {
    viewRef.current = view
    if (view === 'trace' && selected) loadTrace('', selected).catch(e => setError(e.message))
  }, [view, selected])
  // 思考计时器：只在运行期间走表，结束后自动停掉，省掉无谓的重渲染。
  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => setClock(Date.now()), 200)
    return () => window.clearInterval(timer)
  }, [busy])
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
      setDraft(''); runStartedAt.current = Date.now(); setMessages(old => [...old, { role: 'user', content: message, seq: Date.now() }]); setRun({ id: result.run_id, status: 'running' }); watchRun(result.run_id, sessionId)
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
        <button className={`module-item ${view === 'trace' ? 'active' : ''}`} onClick={() => { setView('trace'); setSidebar(false) }} disabled={!selected}><ListTree size={16}/><span>执行日志</span>{runs.length > 0 && <b className="module-count">{runs.length}</b>}</button>
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
          // 正在流式生长的那条才可能处于"思考中"；也只有最新一条助手消息才显示计时（历史消息拿不到当时的耗时）。
          const newestAssistant = [...messages].reverse().find(item => item.role === 'assistant')
          const streamingSeq = busy ? newestAssistant?.seq : undefined
          return visible.map(message => {
            const toolCalls = message.tool_calls
            const isToolCall = !!(toolCalls && toolCalls.length)
            const thinking = message.thinking || (isToolCall ? (message.content || '') : '')
            // 模型有时把同一段文字同时写进推理与正文（工具轮的决策说明就是这样）。本轮拿到
            // tool_calls 之前，正文会被当作回答渲染，于是同一段话在思考面板和气泡里各出现一遍。
            // 抑制只在运行进行中生效：运行一结束就先整表刷新（工具轮会带上 tool_calls 变成
            // "调用了 N 个工具"），终答也照常显示——宁可显示得重复，也不能把回答藏起来。
            const duplicated = sharesText(message.content, message.thinking)
            const answer = isToolCall || (duplicated && busy) ? '' : (message.content || '')
            const toolCount = toolCalls?.length ?? 0
            return <article key={`${message.seq}-${message.role}`} className={`message ${message.role}`}>
              <div className="role">{message.role === 'user' ? '你' : 'Agent'}</div>
              {message.role === 'assistant' && thinking && <ThinkingPanel thinking={thinking} busy={message.seq === streamingSeq} seconds={message.seq === newestAssistant?.seq ? elapsedSeconds : null}/>}
              {isToolCall && <div className="tool-calls"><Wrench size={13}/>调用了 {toolCount} 个工具</div>}
              {answer && <div className="bubble" dangerouslySetInnerHTML={message.role === 'assistant' ? {__html: markdownHtml(answer)} : undefined}>{message.role === 'user' ? answer : undefined}</div>}
            </article>
          })
        })()}
        {busy && <div className="running"><span/><span/><span/><em>{run?.status === 'cancel_requested' ? '正在停止' : '正在处理'}</em>{notice && <i className="notice">{notice}</i>}</div>}
        <div ref={endRef}/>
      </section>}
      {/* 执行日志视图：按运行轮次分组的步骤时间线 */}
      {view === 'trace' && <section className="trace-page">
        <div className="trace-header"><div><h2>执行日志</h2><p>按问题查看 Agent 的完整处理步骤、工具调用和模型响应。</p></div><span className="trace-count">{runs.length}</span></div>
        {!selected && <div className="trace-empty"><ListTree size={26}/><h3>请先选择会话</h3><p>选择左侧会话后查看执行链路。</p></div>}
        {selected && !trace.length && <div className="trace-empty"><ListTree size={26}/><h3>暂无执行记录</h3><p>发送问题后，这里会显示 Agent 的处理步骤。</p></div>}
        {selected && trace.length > 0 && <TraceTimeline trace={trace} runs={runs} messages={messages} collapsedRuns={collapsedRuns} onToggle={id => setCollapsedRuns(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })}/>} 
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
