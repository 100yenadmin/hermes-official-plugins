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
