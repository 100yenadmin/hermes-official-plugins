/** Gallery decisions shared by the list and provenance navigation. No network or rendering. */
export interface RemoteModel {
  task_id: string
  task_type: string
  prompt: string
  created_at: number
  thumbnail_url: null | string
  glb_url: null | string
  kind: 'image' | 'model'
  image_urls: string[]
  expired: boolean
  local_path?: null | string
  credits?: null | number
}

export type Source =
  | { kind: 'file'; path: string; task?: string; media?: 'image' | 'model'; images?: string[]; label?: string }
  | { kind: 'remote'; url: string; label: string; media?: 'image' | 'model'; images?: string[]; expired?: boolean }

export function sourceForAsset(asset: RemoteModel): Source | null {
  if (asset.kind === 'model' && asset.local_path) {
    return { kind: 'file', path: asset.local_path, task: asset.task_id }
  }
  const url = asset.kind === 'image' ? asset.image_urls[0] : asset.glb_url
  return url ? {
    kind: 'remote', url, label: asset.prompt || asset.task_type,
    media: asset.kind, images: asset.kind === 'image' ? asset.image_urls : undefined, expired: asset.expired
  } : null
}

export function matchesKind(filter: 'all' | 'image' | 'model', kind: 'image' | 'model'): boolean {
  return filter === 'all' || filter === kind
}

export interface StorageStatus {
  bytes: number
  files: number
  directory: string
}
