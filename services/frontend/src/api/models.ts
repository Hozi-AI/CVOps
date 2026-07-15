import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'
import { PRESIGNED_URL_GC_MS, PRESIGNED_URL_STALE_MS } from '../lib/presign'

export interface ModelVersion {
  id: string
  project_id: string
  blob_hash: string
  name: string | null
  description: string | null
  trained_on_commit_id: string | null
  base_model: string | null
  hyperparams: Record<string, unknown> | null
  metrics: Record<string, unknown> | null
  code_version: string | null
  mlflow_run_id: string | null
  created_at: string
}

export interface ModelVersionCreate {
  blob_hash: string
  size_bytes: number
  media_type?: string
  name?: string
  description?: string
  base_model?: string
  trained_on_commit_id?: string
  mlflow_run_id?: string
}

export function useModels(projectId: string | undefined) {
  return useQuery<ModelVersion[]>({
    queryKey: ['models', projectId],
    queryFn: async () => {
      const { data } = await client.get<ModelVersion[]>(`/projects/${projectId}/models`)
      return data
    },
    enabled: !!projectId,
  })
}

export function useModel(id: string | undefined) {
  return useQuery<ModelVersion>({
    queryKey: ['model', id],
    queryFn: async () => {
      const { data } = await client.get<ModelVersion>(`/models/${id}`)
      return data
    },
    enabled: !!id,
  })
}

export function useWeightsUrl(id: string | undefined) {
  return useQuery<{ url: string }>({
    queryKey: ['weights-url', id],
    queryFn: async () => {
      const { data } = await client.get<{ url: string }>(`/models/${id}/weights-url`)
      return data
    },
    enabled: !!id,
    staleTime: PRESIGNED_URL_STALE_MS,
    gcTime: PRESIGNED_URL_GC_MS,
  })
}

async function sha256hex(file: File): Promise<string> {
  const buf = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

export function useUploadModel(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (params: {
      file: File
      name?: string
      description?: string
      baseModel?: string
      trainedOnCommitId?: string
      mlflowRunId?: string
    }) => {
      const blobHash = await sha256hex(params.file)

      // Get presigned PUT URL
      const { data: slot } = await client.get<{ upload_url: string }>(
        `/projects/${projectId}/models/upload-url`,
        { params: { blob_hash: blobHash } },
      )

      // Upload directly to MinIO
      await fetch(slot.upload_url, {
        method: 'PUT',
        body: params.file,
        headers: { 'Content-Type': 'application/octet-stream' },
      })

      // Register model version
      const { data } = await client.post<ModelVersion>(`/projects/${projectId}/models`, {
        blob_hash: blobHash,
        size_bytes: params.file.size,
        name: params.name,
        description: params.description,
        base_model: params.baseModel,
        trained_on_commit_id: params.trainedOnCommitId || undefined,
        mlflow_run_id: params.mlflowRunId || undefined,
      } satisfies ModelVersionCreate)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', projectId] }),
  })
}

export function usePatchModel(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (patch: { name?: string; description?: string; mlflow_run_id?: string }) => {
      const { data } = await client.patch<ModelVersion>(`/models/${id}`, patch)
      return data
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['model', id] })
      qc.invalidateQueries({ queryKey: ['models', data.project_id] })
    },
  })
}
