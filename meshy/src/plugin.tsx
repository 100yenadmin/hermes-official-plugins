/**
 * Meshy for Hermes Desktop: view what the Meshy MCP server makes without leaving the app.
 *
 * - `::meshy-model{path="/abs/x.glb"}` (or `url="https://assets.meshy.ai/..."`) renders an inline
 *   3D card in the transcript. The meshy-3d skill tells the agent to emit it after a download.
 * - "Meshy: open 3D gallery" (⌘K) opens a workspace tab listing recent generations and local
 *   downloads beside a full viewer.
 * - A status-bar chip shows remaining credits, refreshed after every Meshy tool call.
 *
 * Bytes and API calls go through this plugin's own backend (`dashboard/plugin_api.py`) via
 * `ctx.rest`; the renderer never reads files or holds the API key.
 */

import type { HermesPlugin, PaletteContribution } from '@hermes/plugin-sdk'
import { host, PALETTE_AREA, STATUSBAR_AREAS, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { type Balance, BalanceChip, Gallery, type Source, TranscriptCard } from './views.tsx'

const WORKSPACE_ID = 'meshy-gallery'

const plugin: HermesPlugin = {
  id: 'meshy',
  name: 'Meshy 3D',
  description: 'View Meshy 3D models inline in chat and in a gallery tab, with your credit balance in the status bar.',
  register(ctx) {
    let balance: Balance | null = null
    const listeners = new Set<(b: Balance | null) => void>()
    let pendingSource: null | Source = null
    const galleryListeners = new Set<(s: null | Source) => void>()
    let closeWorkspace: (() => void) | null = null

    const refreshBalance = () => {
      ctx
        .rest<Balance>('status', { timeoutMs: 20_000 })
        .then(b => {
          balance = b
          listeners.forEach(fn => fn(b))
        })
        .catch(() => undefined) // backend not mounted yet (plugin just enabled) or Meshy down: keep the last value
    }

    function GalleryHost() {
      const [source, setSource] = useState<null | Source>(pendingSource)
      useEffect(() => {
        galleryListeners.add(setSource)
        return () => {
          galleryListeners.delete(setSource)
        }
      }, [])
      return <Gallery initial={source} os={ctx.os} rest={ctx.rest} />
    }

    const openGallery = (source: null | Source = null, title = 'Meshy 3D') => {
      pendingSource = source
      galleryListeners.forEach(fn => fn(source))
      if (typeof host.openWorkspace !== 'function') {
        host.notify({ kind: 'error', message: 'Update Hermes Desktop to open the Meshy gallery.' })
        return
      }
      closeWorkspace = host.openWorkspace(WORKSPACE_ID, {
        title: source ? `Meshy · ${title}` : title,
        minWidth: '28rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <GalleryHost />
      })
    }

    function Chip() {
      const [b, setB] = useState<Balance | null>(balance)
      useEffect(() => {
        listeners.add(setB)
        return () => {
          listeners.delete(setB)
        }
      }, [])
      return <BalanceChip balance={b} onOpen={() => openGallery()} />
    }

    ctx.onEvent('tool.complete', event => {
      const name = String((event.payload as { name?: unknown } | undefined)?.name ?? '')
      if (name.startsWith('mcp__meshy__') || name.startsWith('mcp_meshy_')) {
        refreshBalance()
      }
    })
    ctx.onDispose(() => {
      closeWorkspace?.()
      listeners.clear()
      galleryListeners.clear()
    })
    refreshBalance()

    ctx.registerMany([
      {
        id: 'model-directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'meshy-model',
          render: ({ attrs, streaming }: { attrs: Readonly<Record<string, string>>; streaming: boolean }) => (
            <TranscriptCard
              attrs={attrs}
              openInWorkspace={(source, title) => openGallery(source, title)}
              os={ctx.os}
              rest={ctx.rest}
              streaming={streaming}
            />
          )
        }
      },
      {
        id: 'balance',
        area: STATUSBAR_AREAS.right,
        order: 80,
        render: () => <Chip />
      },
      {
        id: 'open-gallery',
        area: PALETTE_AREA,
        data: {
          id: 'meshy.gallery',
          label: 'Meshy: open 3D gallery',
          keywords: ['meshy', '3d', 'model', 'glb', 'gallery', 'viewer'],
          run: () => openGallery()
        } satisfies PaletteContribution
      },
      {
        id: 'open-file',
        area: PALETTE_AREA,
        data: {
          id: 'meshy.open-file',
          label: 'Meshy: view a .glb file…',
          keywords: ['meshy', '3d', 'glb', 'gltf', 'open', 'file'],
          run: () => {
            void ctx.os
              .pickOpenPath({ title: 'Open a 3D model', filters: [{ name: '3D models', extensions: ['glb', 'gltf'] }] })
              .then(path => {
                if (path) {
                  openGallery({ kind: 'file', path }, path.split(/[\\/]/).pop() || 'model')
                }
              })
          }
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
