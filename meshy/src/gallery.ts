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
  | { kind: 'file'; path: string; task?: string; media?: 'image' | 'model'; images?: string[]; label?: string; incomplete?: boolean }
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


export function imageNextActions(task: string, source: Source, imageIndex = 0) {
  const images = source.images?.length ? source.images : [source.kind === 'file' ? source.path : source.url]
  const index = Math.max(0, Math.min(imageIndex, images.length - 1))
  const reference = source.kind === 'file'
    ? `Use only the saved local image ${JSON.stringify(images[index])}; the task ID is for reference.`
    : images.length === 1 ? `Use input_task_id ${JSON.stringify(task)}.`
    : `Use only image ${index + 1} of ${images.length} from this task. Retrieve its image_urls and use that image URL, not the whole task's image set.`
  const rows = [
    { id: 'model', label: 'Make a 3D model', message: 'Make a 3D model from the displayed image with meshy_image_to_3d. Ask about intended use and mesh detail first.' },
    { id: 'edit', label: 'Edit image', message: 'Edit the displayed image with meshy_image_to_image. Ask what changes I want first.' },
    { id: 'views', label: 'Generate multiple views', message: 'Generate front, side and back views using meshy_image_to_image with generate_multi_view true. Ask about any desired changes first.' }
  ].map(row => ({ ...row, reference }))
  if (images.length > 1) rows.push({ id: 'all-model', label: 'Make 3D from all views', message: 'Make a model with meshy_multi_image_to_3d. Ask about intended use and select up to four suitable views first.',
    reference: source.kind === 'file' ? `Use the saved image files ${JSON.stringify(images)}.` : `Use input_task_id ${JSON.stringify(task)} for the image set.` })
  return rows.map(row => ({ ...row, message: `Meshy image task ${task}: ${row.message} ${row.reference} Quote the credit cost for the chosen settings and ask for my approval before generating.` }))
}
