import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'
import { sha256Hex } from '../lib/hash'

export interface DataSource {
  id: string
  project_id: string
  type: string
  status: string
  blob_hash: string | null
  external_uri: string | null
  metadata: Record<string, unknown> | null
  sample_count: number | null
  latest_run_id: string | null
  created_at: string
}

export interface UploadResponse {
  data_source: DataSource
  presigned_put_url: string | null
}

export interface DataSourceMatch {
  data_source_id: string
  project_id: string
  project_name: string
  type: string
}

export interface DataSourceCheckResponse {
  exists: boolean
  in_current_project: boolean
  matches: DataSourceMatch[]
}

/**
 * Pre-upload dedup probe: ask whether this exact content already exists
 * anywhere in the user's org, so we can skip pushing a duplicate over the wire.
 * Imperative (called inline in the upload flow), not a hook.
 */
export async function checkDuplicate(
  projectId: string,
  blobHash: string,
): Promise<DataSourceCheckResponse> {
  const { data } = await client.post<DataSourceCheckResponse>(
    `/projects/${projectId}/data-sources/check`,
    { blob_hash: blobHash },
  )
  return data
}

// Lifecycle: pending → uploaded → ingesting → ingested | failed.
const TERMINAL_STATUSES = new Set(['ingested', 'failed'])

/** Whether a source is still moving through ingest (drives list polling). */
export function isProcessing(ds: DataSource): boolean {
  if (TERMINAL_STATUSES.has(ds.status)) return false
  // Images aren't frame-extracted; once uploaded they're done.
  if (ds.type === 'image' && ds.status === 'uploaded') return false
  return true
}

export function useDataSources(projectId: string | undefined) {
  return useQuery<DataSource[]>({
    queryKey: ['data-sources', projectId],
    queryFn: async () => {
      const { data } = await client.get<DataSource[]>(`/projects/${projectId}/data-sources`)
      return data
    },
    enabled: !!projectId,
    // Keep refreshing while anything is still ingesting so extracted frames
    // appear on their own without a manual reload.
    refetchInterval: (query) => {
      const data = query.state.data
      return data && data.some(isProcessing) ? 4000 : false
    },
  })
}

export function useDataSourceUrl(id: string | undefined, enabled = true) {
  return useQuery<{ url: string }>({
    queryKey: ['data-source-url', id],
    queryFn: async () => {
      const { data } = await client.get<{ url: string }>(`/data-sources/${id}/url`)
      return data
    },
    enabled: enabled && !!id,
    staleTime: 50 * 60 * 1000,
    retry: false,
  })
}

export function useDeleteDataSource(projectId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      await client.delete(`/data-sources/${id}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['data-sources', projectId] }),
  })
}

// ── Direct image upload (single / group / folder → samples) ──────────────────

interface PresignItem {
  filename: string
  blob_hash: string
  put_url: string
}

interface ImageUploadResult {
  source_id: string
  created: number
  sample_ids: string[]
}

async function readImageSize(file: File): Promise<{ width: number; height: number }> {
  const url = URL.createObjectURL(file)
  try {
    const img = new Image()
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve()
      img.onerror = () => reject(new Error(`Could not read image ${file.name}`))
      img.src = url
    })
    return { width: img.naturalWidth, height: img.naturalHeight }
  } finally {
    URL.revokeObjectURL(url)
  }
}

/**
 * Upload images directly into the project's shared "Uploads" folder and create
 * samples immediately: hash + measure each file client-side, presign, PUT to
 * storage, then confirm. No ingest workflow involved.
 */
export function useUploadImages(projectId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { files: File[]; group?: string }): Promise<ImageUploadResult> => {
      const metas = await Promise.all(
        vars.files.map(async (file) => ({
          file,
          blob_hash: `sha256:${await sha256Hex(file)}`,
          ...(await readImageSize(file)),
        })),
      )
      const byHash = new Map(metas.map((m) => [m.blob_hash, m.file]))

      const { data: presign } = await client.post<{ items: PresignItem[] }>(
        `/projects/${projectId}/image-uploads/presign`,
        {
          items: metas.map((m) => ({
            filename: m.file.name,
            content_type: m.file.type || 'image/jpeg',
            sha256: m.blob_hash,
          })),
        },
      )

      await Promise.all(
        presign.items.map(async (it) => {
          const file = byHash.get(it.blob_hash)
          if (!file) return
          const put = await fetch(it.put_url, { method: 'PUT', body: file })
          if (!put.ok) throw new Error(`Upload failed: ${put.status}`)
        }),
      )

      const { data } = await client.post<ImageUploadResult>(
        `/projects/${projectId}/image-uploads/confirm`,
        {
          group: vars.group,
          items: metas.map((m) => ({
            blob_hash: m.blob_hash,
            width: m.width,
            height: m.height,
            content_type: m.file.type || 'image/jpeg',
            size_bytes: m.file.size,
          })),
        },
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['data-sources', projectId] })
      qc.invalidateQueries({ queryKey: ['samples', projectId] })
      qc.invalidateQueries({ queryKey: ['events'] })
    },
  })
}
