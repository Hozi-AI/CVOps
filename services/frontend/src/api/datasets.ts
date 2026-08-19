import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient, useInfiniteQuery } from '@tanstack/react-query'
import { client } from '../lib/client'
import type { CursorPage, Sample } from './samples'
import type { RunOut } from './runs'

export interface TrainCommitRequest {
  git_url: string
  entry_point?: string
  branch?: string | null
  hyperparams?: Record<string, string | number | boolean> | null
  training_container_id?: string
}

export interface Dataset {
  id: string
  project_id: string
  name: string
  created_at: string
}

export interface Commit {
  id: string
  dataset_id: string
  parent_commit_id: string | null
  message: string | null
  stats: Record<string, unknown> | null
  ontology_id: string
  ontology_version: number
  created_at: string
}

export interface CommitDiff {
  added: string[]
  removed: string[]
  changed: string[]
}

export function useDatasets(projectId: string | undefined) {
  return useQuery<Dataset[]>({
    queryKey: ['datasets', projectId],
    queryFn: async () => {
      const { data } = await client.get<Dataset[]>(`/projects/${projectId}/datasets`)
      return data
    },
    enabled: !!projectId,
  })
}

export function useDataset(id: string | undefined) {
  return useQuery<Dataset>({
    queryKey: ['dataset', id],
    queryFn: async () => {
      const { data } = await client.get<Dataset>(`/datasets/${id}`)
      return data
    },
    enabled: !!id,
  })
}

export function useCommits(datasetId: string | undefined) {
  return useInfiniteQuery<CursorPage<Commit>>({
    queryKey: ['commits', datasetId],
    queryFn: async ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '50' })
      if (pageParam) params.set('cursor', pageParam as string)
      const { data } = await client.get<CursorPage<Commit>>(
        `/datasets/${datasetId}/commits?${params}`,
      )
      return data
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!datasetId,
  })
}

export function useCommit(datasetId: string | undefined, commitId: string | undefined) {
  return useQuery<Commit>({
    queryKey: ['commit', datasetId, commitId],
    queryFn: async () => {
      const { data } = await client.get<Commit>(`/datasets/${datasetId}/commits/${commitId}`)
      return data
    },
    enabled: !!datasetId && !!commitId,
    staleTime: Infinity,
  })
}

// The samples frozen into a specific commit (the dataset's curated, labeled
// snapshot), cursor-paginated. Same page shape as useSamples, so it drops
// straight into <SampleGrid>; the grid's box overlay shows each sample's latest
// annotation revision.
export function useCommitSamples(datasetId: string | undefined, commitId: string | null | undefined) {
  return useInfiniteQuery<CursorPage<Sample>>({
    queryKey: ['commit-samples', datasetId, commitId],
    queryFn: async ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '100' })
      if (pageParam) params.set('cursor', pageParam as string)
      const { data } = await client.get<CursorPage<Sample>>(
        `/datasets/${datasetId}/commits/${commitId}/samples?${params}`,
      )
      return data
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!datasetId && !!commitId,
  })
}

/**
 * The diff between two commits — sample ids added/removed/changed going from
 * `fromCommitId` to `toCommitId`. Enabled only when both ids are present, so a
 * first commit (null parent) can be handled by the caller without a request.
 */
export function useCommitDiff(
  datasetId: string | undefined,
  fromCommitId: string | null | undefined,
  toCommitId: string | null | undefined,
) {
  return useQuery<CommitDiff>({
    queryKey: ['commit-diff', datasetId, fromCommitId, toCommitId],
    queryFn: async () => {
      const params = new URLSearchParams({ from: fromCommitId as string, to: toCommitId as string })
      const { data } = await client.get<CommitDiff>(`/datasets/${datasetId}/diff?${params}`)
      return data
    },
    enabled: !!datasetId && !!fromCommitId && !!toCommitId,
    staleTime: Infinity,
  })
}

export function useReviewDataset() {
  return useMutation({
    mutationFn: async (datasetId: string) => {
      const { data } = await client.post<{ run_id: string }>(
        `/datasets/${datasetId}/review`,
      )
      return data
    },
  })
}

export function useTrainCommit(datasetId: string | undefined) {
  return useMutation({
    mutationFn: async (body: { commitId: string } & TrainCommitRequest) => {
      const { commitId, ...payload } = body
      const { data } = await client.post<RunOut>(
        `/datasets/${datasetId}/commits/${commitId}/train`,
        payload,
      )
      return data
    },
  })
}

/** Dispatch an export_yolo run, poll until done, then trigger a browser download. */
export function useDownloadCommit(datasetId: string | undefined, commitId: string | undefined) {
  const [runId, setRunId] = useState<string | null>(null)
  const [dispatching, setDispatching] = useState(false)

  // Check for a cached export on mount so the button can show "Download" immediately.
  // 404 is expected when no export exists yet — suppress the global error toast.
  const cached = useQuery<{ url: string; blob_hash: string }>({
    queryKey: ['commit-export-url', datasetId, commitId],
    queryFn: async () => {
      const { data } = await client.get(`/datasets/${datasetId}/commits/${commitId}/export-url`)
      return data
    },
    enabled: !!datasetId && !!commitId,
    retry: false,
    meta: { silent: true },
  })

  const poll = useQuery({
    queryKey: ['export-poll', runId],
    queryFn: async () => {
      const { data } = await client.get<{ run: RunOut }>(`/runs/${runId}`)
      return data
    },
    enabled: !!runId,
    refetchInterval: (q) => {
      const s = q.state.data?.run?.status
      return !s || s === 'pending' || s === 'running' ? 2000 : false
    },
  })

  function _download(url: string) {
    const a = document.createElement('a')
    a.href = url
    a.download = `commit-${commitId!.slice(0, 8)}.tar.gz`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  }

  // When the polled run finishes, fetch the presigned URL and trigger download.
  useEffect(() => {
    const s = poll.data?.run?.status
    if (!runId || !s) return
    if (s === 'succeeded') {
      let cancelled = false
      client
        .get<{ url: string }>(`/datasets/${datasetId}/commits/${commitId}/export-url`)
        .then(({ data }) => {
          if (cancelled) return
          _download(data.url)
          cached.refetch()
          setRunId(null)
        })
      return () => { cancelled = true }
    }
    if (s === 'failed' || s === 'cancelled') setRunId(null)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poll.data?.run?.status, runId])

  async function trigger() {
    if (!datasetId || !commitId) return
    if (cached.data) { _download(cached.data.url); return }
    setDispatching(true)
    try {
      const { data: run } = await client.post<RunOut>(`/datasets/${datasetId}/commits/${commitId}/export`)
      setRunId(run.id)
    } finally {
      setDispatching(false)
    }
  }

  const busy = dispatching || !!runId
  const label = busy ? 'Exporting…' : cached.data ? 'Download YOLO' : 'Export YOLO'
  return { trigger, busy, label }
}

export function useDeleteDataset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (dataset: Dataset) => {
      await client.delete(`/datasets/${dataset.id}`)
      return dataset
    },
    onSuccess: (dataset) => qc.invalidateQueries({ queryKey: ['datasets', dataset.project_id] }),
  })
}

export function useCreateDataset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { projectId: string; name: string }) => {
      const { data } = await client.post<Dataset>(
        `/projects/${body.projectId}/datasets`,
        { name: body.name },
      )
      return data
    },
    onSuccess: (data) => qc.invalidateQueries({ queryKey: ['datasets', data.project_id] }),
  })
}

export interface CommitFromSamplesResult {
  commit_id: string
  committed_count: number
  skipped_count: number
}

export function useCommitFromSamples() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: {
      datasetId: string
      message?: string
      sample_ids: string[]
      branch_name?: string
      split_strategy?: { train_ratio?: number; val_ratio?: number }
      ontology_id?: string
    }) => {
      const { datasetId, ...body } = vars
      const { data } = await client.post<CommitFromSamplesResult>(
        `/datasets/${datasetId}/commits/from-samples`,
        body,
      )
      return data
    },
    onSuccess: (_d, vars) => qc.invalidateQueries({ queryKey: ['commits', vars.datasetId] }),
  })
}
