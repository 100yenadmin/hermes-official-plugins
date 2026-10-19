/**
 * React surfaces over the three.js stage: an inline transcript card, the workspace gallery, and the
 * status-bar credit chip. Every byte comes from the plugin's own backend (`ctx.rest`) because the
 * renderer cannot read the backend machine's files or call Meshy directly.
 */

import type { PluginContext } from '@hermes/plugin-sdk'
import { Button, Codicon, host } from '@hermes/plugin-sdk'
import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react'

import { createStage, dataUrlToBuffer, type ModelStats, type Stage } from './stage.ts'

export type Rest = PluginContext['rest']
export type Os = PluginContext['os']

export interface FilePayload {
  name: string
  bytes: number
  dataUrl: string
}

export interface Balance {
  configured: boolean
  balance?: number
  key_env?: string
}

export interface RemoteModel {
  task_id: string
  task_type: string
  prompt: string
  created_at: number
  thumbnail_url: null | string
  glb_url: null | string
  credits?: null | number
}

export interface LocalFile {
  path: string
  name: string
  project: string
  bytes: number
  modified: number
}

export type Source = { kind: 'file'; path: string } | { kind: 'remote'; url: string; label: string }

// Inline styles with theme variables: no injected <style> tag (nothing global to leak or clean up),
// and every surface reskins with the active theme.
const V = {
  viewer: { position: 'relative', width: '100%', borderRadius: 8, overflow: 'hidden', background: 'var(--ui-bg-secondary, #1b1d22)', ['--meshy-stage-bg' as string]: 'var(--ui-bg-secondary, #1b1d22)' },
  stage: { position: 'absolute', inset: 0 },
  overlay: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, color: 'var(--ui-text-secondary)', fontSize: 13, pointerEvents: 'none' },
  error: { color: 'var(--ui-danger, #e5484d)' },
  toolbar: { position: 'absolute', top: 6, right: 6, display: 'flex', gap: 2, padding: 2, borderRadius: 6, background: 'color-mix(in srgb, var(--ui-bg, #111) 70%, transparent)' },
  toolOn: { color: 'var(--ui-accent)' },
  stats: { position: 'absolute', left: 6, bottom: 6, padding: '2px 6px', borderRadius: 4, fontSize: 11, color: '#e8e8ea', background: 'rgba(17, 18, 20, 0.62)', fontVariantNumeric: 'tabular-nums', pointerEvents: 'none' },
  card: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 8, margin: '6px 0', maxWidth: 640 },
  cardHead: { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6, fontSize: 13, color: 'var(--ui-text-secondary)' },
  cardTitle: { fontWeight: 600, color: 'var(--ui-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  spacer: { flex: 1 },
  poster: { width: '100%', height: 160, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 6, borderRadius: 8, border: '1px dashed var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-secondary)', cursor: 'pointer', fontSize: 13 },
  gallery: { display: 'flex', height: '100%', minHeight: 0 },
  list: { width: 280, flexShrink: 0, overflowY: 'auto', borderRight: '1px solid var(--ui-stroke-secondary)', padding: 6 },
  listHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '2px 4px 6px', fontWeight: 600, fontSize: 13 },
  section: { padding: '8px 6px 4px', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--ui-text-quaternary)' },
  hint: { padding: '6px', fontSize: 12, color: 'var(--ui-text-tertiary)' },
  item: { display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: 6, border: 0, borderRadius: 6, background: 'transparent', color: 'var(--ui-text)', textAlign: 'left', cursor: 'pointer', fontSize: 13 },
  itemOn: { background: 'var(--ui-bg-tertiary, rgba(127,127,127,.15))' },
  itemText: { display: 'flex', flexDirection: 'column', minWidth: 0, gap: 1 },
  itemSub: { fontSize: 11, color: 'var(--ui-text-tertiary)' },
  thumb: { width: 40, height: 40, objectFit: 'cover', borderRadius: 4, flexShrink: 0, background: 'var(--ui-bg-secondary)' },
  main: { flex: 1, minWidth: 0, padding: 8, display: 'flex' },
  empty: { flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, color: 'var(--ui-text-tertiary)' },
  chip: { display: 'inline-flex', alignItems: 'center', gap: 4, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', cursor: 'pointer', padding: '0 4px' }
} satisfies Record<string, CSSProperties>

const fmtBytes = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)
const fmtCount = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n}`)

/** `ctx.rest` rejects with `"<status>: <body>"`; show the backend's `detail` sentence, not raw JSON. */
export function errorText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const match = /"detail"\s*:\s*"([^"]+)"/.exec(raw)
  return match ? match[1].charAt(0).toUpperCase() + match[1].slice(1) : raw
}

export function fetchSource(rest: Rest, source: Source): Promise<FilePayload> {
  return source.kind === 'file'
    ? rest<FilePayload>(`file?path=${encodeURIComponent(source.path)}`, { timeoutMs: 120_000 })
    : rest<FilePayload>(`remote?url=${encodeURIComponent(source.url)}`, { timeoutMs: 180_000 })
}

/** One live viewport. Mounting creates a WebGL context; unmounting frees it. */
export function ModelViewer({
  rest,
  os,
  source,
  height,
  compact,
  autoRotate = true
}: {
  rest: Rest
  os: Os
  source: Source
  height: number | string
  compact?: boolean
  autoRotate?: boolean
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<Stage | null>(null)
  const [state, setState] = useState<{ phase: 'error' | 'loading' | 'ready'; message?: string }>({ phase: 'loading' })
  const [stats, setStats] = useState<ModelStats | null>(null)
  const [file, setFile] = useState<{ name: string; bytes: number } | null>(null)
  const [spin, setSpin] = useState(autoRotate)
  const [wire, setWire] = useState(false)
  const [grid, setGrid] = useState(true)
  const key = source.kind === 'file' ? source.path : source.url

  useEffect(() => {
    const el = hostRef.current
    if (!el) {
      return
    }
    const bg = getComputedStyle(el).getPropertyValue('--meshy-stage-bg').trim() || '#1b1d22'
    const stage = createStage(el, bg)
    stageRef.current = stage
    stage.setAutoRotate(spin)
    let live = true
    setState({ phase: 'loading' })
    setStats(null)
    fetchSource(rest, source)
      .then(async payload => {
        if (!live) {
          return
        }
        setFile({ name: payload.name, bytes: payload.bytes })
        const s = await stage.load(dataUrlToBuffer(payload.dataUrl))
        if (live) {
          setStats(s)
          setState({ phase: 'ready' })
        }
      })
      .catch((err: unknown) => {
        if (live) {
          setState({ phase: 'error', message: errorText(err) })
        }
      })
    return () => {
      live = false
      stageRef.current = null
      stage.dispose()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  useEffect(() => stageRef.current?.setAutoRotate(spin), [spin])
  useEffect(() => stageRef.current?.setWireframe(wire), [wire, stats])
  useEffect(() => stageRef.current?.setGrid(grid), [grid])

  const toggle = (on: boolean, set: (v: boolean) => void, icon: string, label: string) => (
    <Button
      aria-label={label}
      aria-pressed={on}
      style={on ? V.toolOn : undefined}
      onClick={() => set(!on)}
      size="icon"
      title={label}
      variant="ghost"
    >
      <Codicon name={icon} />
    </Button>
  )

  return (
    <div data-meshy-phase={state.phase} style={{ ...V.viewer, height }}>
      <div ref={hostRef} style={V.stage} />
      {state.phase === 'loading' && (
        <div style={V.overlay}>
          <Codicon name="loading" spinning /> Loading {source.kind === 'remote' ? source.label : 'model'}…
        </div>
      )}
      {state.phase === 'error' && (
        <div style={{ ...V.overlay, ...V.error }}>
          <Codicon name="error" /> {state.message}
        </div>
      )}
      <div style={V.toolbar}>
        {toggle(spin, setSpin, 'sync', 'Auto-rotate')}
        {toggle(wire, setWire, 'symbol-structure', 'Wireframe')}
        {toggle(grid, setGrid, 'symbol-numeric', 'Grid')}
        <Button aria-label="Reset view" onClick={() => stageRef.current?.resetView()} size="icon" title="Reset view" variant="ghost">
          <Codicon name="screen-normal" />
        </Button>
        {source.kind === 'file' && (
          <Button
            aria-label="Show in folder"
            onClick={() => void os.revealPath(source.path)}
            size="icon"
            title="Show in folder"
            variant="ghost"
          >
            <Codicon name="folder-opened" />
          </Button>
        )}
      </div>
      {stats && (
        <div data-meshy-stats style={V.stats}>
          {file ? `${file.name} · ${fmtBytes(file.bytes)} · ` : ''}
          {fmtCount(stats.triangles)} tris · {fmtCount(stats.vertices)} verts
          {compact ? '' : ` · ${stats.meshes} mesh${stats.meshes === 1 ? '' : 'es'} · ${stats.materials} material${stats.materials === 1 ? '' : 's'}`}
        </div>
      )}
    </div>
  )
}

/** Transcript directive card: `::meshy-model{path="/abs/model.glb"}` or `{url="https://assets.meshy.ai/..."}`.
 *  Shows a click-to-load poster first so a long transcript never opens N WebGL contexts at once. */
export function TranscriptCard({
  rest,
  os,
  attrs,
  streaming,
  openInWorkspace
}: {
  rest: Rest
  os: Os
  attrs: Readonly<Record<string, string>>
  streaming: boolean
  openInWorkspace: (source: Source, title: string) => void
}) {
  const [live, setLive] = useState(false)
  const path = (attrs.path || attrs.file || '').trim()
  const url = (attrs.url || '').trim()
  const title = attrs.title || (path ? path.split(/[\\/]/).pop() : 'Meshy model') || 'Meshy model'
  const source: null | Source = path ? { kind: 'file', path } : url ? { kind: 'remote', url, label: title } : null

  if (!source) {
    return <div style={{ ...V.card, ...V.error }}>Meshy model: give a path="..." or url="..."</div>
  }

  return (
    <div data-meshy-card style={V.card}>
      <div style={V.cardHead}>
        <Codicon name="symbol-misc" />
        <span style={V.cardTitle}>{title}</span>
        <span style={V.spacer} />
        <Button onClick={() => openInWorkspace(source, title)} size="sm" variant="ghost">
          <Codicon name="link-external" /> Open
        </Button>
      </div>
      {live ? (
        <ModelViewer autoRotate={false} compact height={320} os={os} rest={rest} source={source} />
      ) : (
        <button
          style={V.poster}
          disabled={streaming}
          onClick={() => setLive(true)}
          type="button"
        >
          <Codicon name="play-circle" size={28} />
          <span>{streaming ? 'Waiting for the reply to finish…' : 'View in 3D'}</span>
        </button>
      )}
    </div>
  )
}

/** Full workspace: recent Meshy generations + local downloads on the left, the viewer on the right. */
export function Gallery({ rest, os, initial }: { rest: Rest; os: Os; initial: null | Source }) {
  const [selected, setSelected] = useState<null | Source>(initial)
  const [remote, setRemote] = useState<RemoteModel[] | null>(null)
  const [local, setLocal] = useState<LocalFile[] | null>(null)
  const [error, setError] = useState<null | string>(null)
  const [configured, setConfigured] = useState(true)

  const refresh = useCallback(() => {
    setError(null)
    rest<{ configured: boolean; models: RemoteModel[] }>('models?limit=24', { timeoutMs: 60_000 })
      .then(r => {
        setConfigured(r.configured)
        setRemote(r.models)
      })
      .catch((e: unknown) => setError(errorText(e)))
    rest<{ files: LocalFile[] }>('local')
      .then(r => setLocal(r.files))
      .catch(() => setLocal([]))
  }, [rest])

  useEffect(refresh, [refresh])
  useEffect(() => {
    if (initial) {
      setSelected(initial)
    }
  }, [initial])

  const isSel = (s: Source) =>
    selected && selected.kind === s.kind && (s.kind === 'file' ? selected.kind === 'file' && selected.path === s.path : selected.kind === 'remote' && selected.url === s.url)

  return (
    <div data-meshy-gallery style={V.gallery}>
      <aside style={V.list}>
        <div style={V.listHead}>
          <span>Meshy models</span>
          <Button aria-label="Refresh" onClick={refresh} size="icon" title="Refresh" variant="ghost">
            <Codicon name="refresh" />
          </Button>
        </div>
        {!configured && <div style={V.hint}>Set MESHY_API_KEY to list your generations.</div>}
        {error && <div style={{ ...V.hint, ...V.error }}>{error}</div>}
        {local && local.length > 0 && <div style={V.section}>On this machine</div>}
        {local?.map(f => {
          const s: Source = { kind: 'file', path: f.path }
          return (
            <button data-meshy-item style={isSel(s) ? { ...V.item, ...V.itemOn } : V.item} key={f.path} onClick={() => setSelected(s)} type="button">
              <Codicon name="file-binary" />
              <span style={V.itemText}>
                <span>{f.name}</span>
                <small style={V.itemSub}>
                  {f.project} · {fmtBytes(f.bytes)}
                </small>
              </span>
            </button>
          )
        })}
        {remote && remote.length > 0 && <div style={V.section}>Recent generations</div>}
        {remote === null && configured && !error && <div style={V.hint}>Loading…</div>}
        {remote?.map(m => {
          const s: null | Source = m.glb_url ? { kind: 'remote', url: m.glb_url, label: m.prompt || m.task_type } : null
          return (
            <button
              data-meshy-item style={s && isSel(s) ? { ...V.item, ...V.itemOn } : V.item}
              disabled={!s}
              key={m.task_id}
              onClick={() => s && setSelected(s)}
              title={m.task_id}
              type="button"
            >
              {m.thumbnail_url ? <img alt="" style={V.thumb} src={m.thumbnail_url} /> : <Codicon name="symbol-misc" />}
              <span style={V.itemText}>
                <span>{m.prompt || m.task_type}</span>
                <small style={V.itemSub}>
                  {m.task_type}
                  {typeof m.credits === 'number' ? ` · ${m.credits} credits` : ''} · {new Date(m.created_at).toLocaleDateString()}
                </small>
              </span>
            </button>
          )
        })}
      </aside>
      <section style={V.main}>
        {selected ? (
          <ModelViewer height="100%" os={os} rest={rest} source={selected} />
        ) : (
          <div style={V.empty}>
            <Codicon name="symbol-misc" size={32} />
            <span>Pick a model to view it in 3D.</span>
          </div>
        )}
      </section>
    </div>
  )
}

/** Status-bar chip: remaining Meshy credits; refreshed on open and after any Meshy tool finishes. */
export function BalanceChip({ balance, onOpen }: { balance: Balance | null; onOpen: () => void }) {
  if (!balance) {
    return null
  }
  const label = balance.configured ? `${(balance.balance ?? 0).toLocaleString()} cr` : 'no key'
  return (
    <button data-meshy-chip style={V.chip} onClick={onOpen} title="Meshy credits — open the 3D gallery" type="button">
      <Codicon name="symbol-misc" />
      <span>Meshy {label}</span>
    </button>
  )
}

export { host }
