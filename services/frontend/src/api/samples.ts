import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'
import { PRESIGNED_URL_GC_MS, PRESIGNED_URL_STALE_MS } from '../lib/presign'

export interface TagBrief {
  id: string
  name: string
  color: string
}

export interface Sample {
  id: string
  project_id: string
  blob_hash: string
  source_id: string
  modality: string
  width: number | null
  height: number | null
  frame_index: number | null
  perceptual_hash: string | null
  metadata: Record<string, unknown> | null
  review_status: string
  tags: TagBrief[]
  has_annotations: boolean
  latest_revision_id: string | null
  created_at: string
}

export interface CursorPage<T> {
  items: T[]
  next_cursor: string | null
}

export interface SampleFilters {
  source_id?: string
  review_status?: string
  has_annotations?: boolean
  annotation_class?: string
  collection_id?: string
  tag_id?: string
  created_after?: string
  created_before?: string
}

function filterParams(filters: SampleFilters): URLSearchParams {
  const params = new URLSearchParams({ limit: '50' })
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== '') params.set(k, String(v))
  }
  return params
}

export function useSamples(projectId: string | undefined, filters: SampleFilters = {}) {
  return useInfiniteQuery<CursorPage<Sample>>({
    queryKey: ['samples', projectId, filters],
    queryFn: async ({ pageParam }) => {
      const params = filterParams(filters)
      if (pageParam) params.set('cursor', pageParam as string)
      const { data } = await client.get<CursorPage<Sample>>(
        `/projects/${projectId}/samples?${params}`,
      )
      return data
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!projectId,
  })
}

export function useThumbnailUrl(sampleId: string | undefined) {
  return useQuery<{ url: string }>({
    queryKey: ['thumbnail', sampleId],
    queryFn: async () => {
      const { data } = await client.get<{ url: string }>(`/samples/${sampleId}/thumbnail-url`)
      return data
    },
    enabled: !!sampleId,
    staleTime: PRESIGNED_URL_STALE_MS,
    gcTime: PRESIGNED_URL_GC_MS,
  })
}

export function useImageUrl(sampleId: string | undefined) {
  return useQuery<{ url: string }>({
    queryKey: ['image-url', sampleId],
    queryFn: async () => {
      const { data } = await client.get<{ url: string }>(`/samples/${sampleId}/image-url`)
      return data
    },
    enabled: !!sampleId,
    staleTime: PRESIGNED_URL_STALE_MS,
    gcTime: PRESIGNED_URL_GC_MS,
  })
}

export interface SampleUpdate {
  metadata?: Record<string, unknown>
  metadata_mode?: 'merge' | 'replace'
  tag_ids?: string[]
}

export function usePatchSample(projectId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { id: string; body: SampleUpdate }) => {
      const { data } = await client.patch<Sample>(`/samples/${vars.id}`, vars.body)
      return data
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['samples', projectId] })
      qc.invalidateQueries({ queryKey: ['sample', data.id] })
    },
  })
}

export function useDeleteSample(projectId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      await client.delete(`/samples/${id}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['samples', projectId] }),
  })
}

export type BulkAction =
  | 'delete'
  | 'set_review_status'
  | 'add_tags'
  | 'remove_tags'
  | 'add_to_collection'

export interface SampleBulkAction {
  action: BulkAction
  sample_ids: string[]
  review_status?: string
  tag_ids?: string[]
  collection_id?: string
}

export interface BulkResult {
  matched: number
  affected: number
  skipped_ids: string[]
}

export function useBulkSampleAction(projectId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: SampleBulkAction) => {
      const { data } = await client.post<BulkResult>(
        `/projects/${projectId}/samples/bulk`,
        body,
      )
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['samples', projectId] })
      qc.invalidateQueries({ queryKey: ['collections', projectId] })
    },
  })
}
