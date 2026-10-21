import { type GatewayEvent, host } from '@hermes/plugin-sdk'

/**
 * Provenance: which tool call made which Meshy task. Meshy's own task record keeps a text prompt but not
 * an image task's input image, nor the task a remesh / retexture / rig / animation came from, so the
 * plugin records the creating call (from the tool.complete event) and the gallery shows it as "Made from".
 */

const CREATE_TOOLS = new Set([
  'meshy_text_to_3d', 'meshy_text_to_3d_refine', 'meshy_image_to_3d', 'meshy_multi_image_to_3d', 'meshy_remesh',
  'meshy_retexture', 'meshy_rig', 'meshy_animate', 'meshy_resize', 'meshy_convert', 'meshy_uv_unwrap',
  'meshy_repair_printability', 'meshy_auto_split', 'meshy_process_multicolor', 'meshy_text_to_image', 'meshy_image_to_image'
])

export interface Creation {
  task_id: string
  tool: string
  args: Record<string, unknown>
}

const bare = (name: unknown) => String(name ?? '').replace(/^(mcp__meshy__|mcp_meshy_)/, '')

/** The task id a create call returned: structuredContent.task_id, else the "**Task ID**: …" line. */
export function taskIdFrom(result: unknown): null | string {
  let value: unknown = result
  for (let i = 0; i < 3 && typeof value === 'string'; i++) {
    try {
      value = JSON.parse(value)
    } catch {
      break
    }
  }
  if (value && typeof value === 'object') {
    const v = value as { structuredContent?: { task_id?: unknown }; result?: unknown }
    if (typeof v.structuredContent?.task_id === 'string') {
      return v.structuredContent.task_id
    }
    value = v.result ?? JSON.stringify(value)
  }
  const m = /\*\*Task ID\*\*:\s*([0-9a-f][0-9a-f-]{15,63})/.exec(String(value ?? ''))
  return m ? m[1] : null
}

/** Every Meshy task this tool call created: a direct call, or each call a `tool_call` bridge made
 *  (its result carries one entry per call, in order). */
export function creationsFrom(name: unknown, args: unknown, result: unknown): Creation[] {
  const direct = bare(name)
  if (CREATE_TOOLS.has(direct)) {
    const id = taskIdFrom(result)
    return id ? [{ task_id: id, tool: direct, args: (args ?? {}) as Record<string, unknown> }] : []
  }
  if (String(name ?? '') !== 'tool_call' || !args || typeof args !== 'object') {
    return []
  }
  const calls = (Array.isArray((args as { calls?: unknown }).calls) ? (args as { calls: unknown[] }).calls : [args]) as Array<{
    name?: unknown
    arguments?: unknown
  }>
  let results: unknown[] = []
  try {
    const parsed = typeof result === 'string' ? JSON.parse(result) : result
    const r = (parsed as { results?: unknown })?.results
    results = Array.isArray(r) ? r : [parsed]
  } catch {
    results = [result]
  }
  const out: Creation[] = []
  calls.forEach((call, i) => {
    const tool = bare(call?.name)
    if (!CREATE_TOOLS.has(tool)) {
      return
    }
    const one = results[i] as { result?: unknown; content?: unknown } | undefined
    const id = taskIdFrom(one?.result ?? one?.content ?? one)
    if (id) {
      out.push({ task_id: id, tool, args: (call.arguments ?? {}) as Record<string, unknown> })
    }
  })
  return out
}

/**
 * ctx.rest always reaches the ACTIVE profile's backend, but a completion can belong to a background profile
 * (or another connection). A record is written only while its owner is the active one; others wait in memory
 * and flush when the user switches to that owner, so profile A's prompt never lands in profile B's gallery.
 */
export function createProvenanceWriter(post: (body: unknown) => Promise<unknown>) {
  const pending = new Map<string, unknown[]>()
  const norm = (p: null | string | undefined) => (p ?? '').trim() || 'default'  // = core's normalizeProfileKey
  const active = () => `${host.state?.connectionId.get() ?? ''}::${norm(host.state?.profile.get())}`
  const ownerOf = (e: GatewayEvent) =>
    e.profile === undefined && e.connectionId === undefined ? active() : `${e.connectionId ?? host.state?.connectionId.get() ?? ''}::${norm(e.profile)}`
  const send = (body: unknown) => void post(body).catch(() => undefined)
  // Deferred a tick: core re-points ctx.rest from its own subscription to the same atoms, and a switch can
  // land in a batch; flushing before that would post B's rows to A's backend.
  let timer: ReturnType<typeof setTimeout> | undefined
  const flush = () => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      const key = active()
      const rows = pending.get(key)
      if (rows) {
        pending.delete(key)
        rows.forEach(send)
      }
    }, 0)
  }
  const offs = [host.state?.profile.subscribe(flush), host.state?.connectionId.subscribe(flush)]
  return {
    record(event: GatewayEvent, body: unknown) {
      const owner = ownerOf(event)
      if (owner === active()) {
        send(body)
        return
      }
      const rows = pending.get(owner) ?? []
      rows.push(body)
      pending.set(owner, rows.slice(-200))
    },
    dispose() {
      clearTimeout(timer)
      offs.forEach(off => off?.())
      pending.clear()
    }
  }
}
