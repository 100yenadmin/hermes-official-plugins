// Minimal ambient types for the parts of `@hermes/plugin-sdk` this plugin uses (typecheck only;
// the host supplies the real module at runtime). Shapes mirror apps/desktop/src/sdk + contrib/plugin.ts.
import type { ComponentProps, CSSProperties, ReactNode } from 'react'

export interface PluginRestOptions {
  method?: string
  body?: unknown
  timeoutMs?: number
}
export interface PluginFileDialogOptions {
  defaultPath?: string
  filters?: Array<{ extensions: string[]; name: string }>
  title?: string
}
export interface PluginOs {
  openExternal: (url: string) => Promise<boolean>
  revealPath: (path: string) => Promise<boolean>
  pickOpenPath: (options?: PluginFileDialogOptions) => Promise<null | string>
}
export interface GatewayEvent {
  type: string
  payload?: unknown
  session_id?: string
}
export interface PluginContribution {
  id: string
  area: string
  order?: number
  data?: unknown
  render?: () => ReactNode
}
export interface PluginContext {
  readonly source: string
  registerMany: (cs: PluginContribution[]) => () => void
  onDispose: (fn: () => void) => void
  onEvent: (type: string, listener: (event: GatewayEvent) => void) => () => void
  rest: <T>(path: string, opts?: PluginRestOptions) => Promise<T>
  os: PluginOs
}
export interface HermesPlugin {
  id: string
  name: string
  description?: string
  defaultEnabled?: boolean
  register: (ctx: PluginContext) => void
}
export interface PaletteContribution {
  id: string
  label: string
  keywords?: string[]
  run: () => void
}

export const PALETTE_AREA: string
export const STATUSBAR_AREAS: { left: string; right: string }
export const TRANSCRIPT_DIRECTIVE_AREA: string
export const host: {
  navigate: (path: string) => void
  notify: (input: { kind: 'error' | 'info' | 'success' | 'warning'; message: string }) => string
  openWorkspace?: (
    id: string,
    options: { minWidth?: string; onClose?: () => void; render: () => ReactNode; title?: string }
  ) => () => void
}
export function Button(
  props: ComponentProps<'button'> & { size?: 'default' | 'icon' | 'sm'; variant?: 'default' | 'ghost' | 'outline' }
): ReactNode
export function Codicon(props: { name: string; size?: number; spinning?: boolean; style?: CSSProperties }): ReactNode
