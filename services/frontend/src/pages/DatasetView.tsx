import { useEffect, useMemo, useState } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useDataset, useCommits, useReviewDataset } from '../api/datasets'
import { usePinProject } from '../lib/useActiveProject'
import { CommitGraph } from '../components/dataset/CommitGraph'
import { CommitContents } from '../components/dataset/CommitContents'
import { ImportDatasetDialog } from '../components/dataset/ImportDatasetDialog'
import { Breadcrumbs, Button, ErrorState, SkeletonList } from '../components/ui'

export default function DatasetView() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { data: dataset, isLoading, isError, refetch } = useDataset(id)
  const commitsQuery = useCommits(id)
  const reviewDataset = useReviewDataset()
  const [reviewError, setReviewError] = useState<string | null>(null)
  const [addingData, setAddingData] = useState(false)
  usePinProject(dataset?.project_id)

  const commits = useMemo(
    () => commitsQuery.data?.pages.flatMap((p) => p.items) ?? [],
    [commitsQuery.data],
  )
  const selectedId = params.get('commit')

  // Default the selection to the first returned commit once the list arrives.
  // The commits endpoint returns newest-first, so the first item is the newest.
  // The choice is kept in the URL so a specific commit view is shareable.
  useEffect(() => {
    const first = commits[0]
    if (!selectedId && first) {
      const next = new URLSearchParams(params)
      next.set('commit', first.id)
      setParams(next, { replace: true })
    }
  }, [selectedId, commits, params, setParams])

  function selectCommit(commitId: string) {
    const next = new URLSearchParams(params)
    next.set('commit', commitId)
    setParams(next, { replace: true })
  }

  if (isLoading) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <SkeletonList rows={4} />
      </div>
    )
  }

  if (isError || !dataset) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <ErrorState description="Could not load this dataset." onRetry={() => refetch()} />
      </div>
    )
  }

  const startReview = () => {
    if (!id) return
    setReviewError(null)
    reviewDataset.mutate(id, {
      onSuccess: ({ run_id }) => navigate(`/runs/${run_id}`),
      onError: (err: unknown) => {
        const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        setReviewError(msg ?? 'Could not start review')
      },
    })
  }

  const selectedCommit = commits.find((c) => c.id === selectedId)

  return (
    <div className="mx-auto max-w-7xl p-6">
      <Breadcrumbs
        items={[
          { label: 'Datasets', to: `/projects/${dataset.project_id}/datasets` },
          { label: dataset.name },
        ]}
      />

      <ImportDatasetDialog
        projectId={dataset.project_id}
        open={addingData}
        onClose={() => setAddingData(false)}
        lockedDatasetName={dataset.name}
      />

      <div className="mb-4 flex items-start justify-between">
        <h2 className="text-xl font-bold text-text-primary">{dataset.name}</h2>
        <div className="flex flex-col items-end gap-1">
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => setAddingData(true)}>Add Data</Button>
            <Button onClick={startReview} loading={reviewDataset.isPending}>
              Review in CVAT
            </Button>
          </div>
          {reviewError && <p className="text-xs text-error">{reviewError}</p>}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]">
        {/* Left rail — commit history */}
        <aside className="lg:sticky lg:top-6 lg:max-h-[calc(100vh-7rem)] lg:self-start lg:overflow-y-auto">
          <p className="mb-2 px-1 text-xs font-bold uppercase tracking-wider text-text-muted">History</p>
          <CommitGraph commits={commits} selectedId={selectedId} onSelect={selectCommit} />
          {commitsQuery.hasNextPage && (
            <Button
              variant="secondary"
              className="mt-3 w-full"
              loading={commitsQuery.isFetchingNextPage}
              onClick={() => commitsQuery.fetchNextPage()}
            >
              Load more
            </Button>
          )}
        </aside>

        {/* Right pane — contents of the selected commit */}
        <div className="min-w-0">
          {selectedId && id && dataset ? (
            <CommitContents
              datasetId={id}
              commitId={selectedId}
              projectId={dataset.project_id}
              commit={selectedCommit}
            />
          ) : (
            <div className="rounded-xl border border-border bg-surface-2 p-10 text-center text-sm text-text-muted shadow-sm">
              {commits.length === 0 ? 'No commits to show.' : 'Select a commit to view its samples.'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
