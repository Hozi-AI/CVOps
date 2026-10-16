import { useEffect, useMemo, useState } from 'react'
import { useLocation, useParams, useSearchParams } from 'react-router-dom'
import { useSamples } from '../api/samples'
import type { Sample } from '../api/samples'
import { SampleGrid } from '../components/dataset/SampleGrid'
import { SampleFilterBar } from '../components/samples/SampleFilterBar'
import { parseSampleFilters } from '../lib/sampleFilters'
import { BulkActionBar } from '../components/samples/BulkActionBar'
import { SampleEditDrawer } from '../components/samples/SampleEditDrawer'
import { useSelectionStore } from '../store/selection'
import { BackButton, Breadcrumbs, ErrorState } from '../components/ui'
import { cn } from '../lib/cn'

export default function SampleBrowser() {
  const { id: projectId } = useParams<{ id: string }>()
  const [params] = useSearchParams()
  const filters = useMemo(() => parseSampleFilters(params), [params])
  const filterKey = params.toString()
  const location = useLocation()
  // The Data Sources "View samples" link marks where the user came from; only then is "back" meaningful.
  const cameFromDataSources = (location.state as { from?: string } | null)?.from === 'data-sources'

  const { data, isLoading, isError, refetch, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useSamples(projectId, filters)

  const clear = useSelectionStore((s) => s.clear)
  const selectMode = useSelectionStore((s) => s.selectMode)
  const setSelectMode = useSelectionStore((s) => s.setSelectMode)
  const [editId, setEditId] = useState<string | null>(null)

  // Selection mode is page-scoped UI state — never leak it into another view.
  useEffect(() => () => setSelectMode(false), [setSelectMode])

  // Drop the selection whenever the active filters change — selected ids from a
  // previous view would otherwise leak into bulk actions.
  useEffect(() => {
    clear()
  }, [filterKey, clear])

  // Also clear on unmount so a selection can't leak into another view.
  useEffect(() => () => clear(), [clear])

  const samples = data?.pages.flatMap((p) => p.items) ?? []
  const editSample: Sample | null = samples.find((s) => s.id === editId) ?? null
  const totalLoaded = samples.length

  return (
    <div className="mx-auto max-w-7xl p-6 pb-24">
      <Breadcrumbs items={[{ label: 'Project', to: `/projects/${projectId}` }, { label: 'Samples' }]} />

      <div className="mb-4 flex items-start gap-3">
        {cameFromDataSources && projectId && (
          <BackButton to={`/projects/${projectId}/data-sources`} label="Data Sources" className="-mt-1" />
        )}
        <div>
          <h2 className="text-xl font-bold text-text-primary">Samples</h2>
          {totalLoaded > 0 && (
            <p className="mt-0.5 text-sm text-text-muted">
              {totalLoaded} frame{totalLoaded === 1 ? '' : 's'} loaded
            </p>
          )}
        </div>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">{projectId && <SampleFilterBar projectId={projectId} />}</div>
        <button
          type="button"
          onClick={() => setSelectMode(!selectMode)}
          aria-pressed={selectMode}
          title={selectMode ? 'Exit selection mode' : 'Select multiple samples'}
          className={cn(
            'shrink-0 rounded-lg border px-3 py-2 text-sm font-medium transition-colors',
            selectMode
              ? 'border-iris bg-iris text-white hover:bg-iris-hover'
              : 'border-border-strong bg-surface-2 text-text-secondary hover:text-text-primary',
          )}
        >
          {selectMode ? 'Done' : 'Select'}
        </button>
      </div>

      {isError ? (
        <ErrorState description="Could not load samples." onRetry={() => refetch()} />
      ) : (
        <SampleGrid
          data={data}
          isLoading={isLoading}
          hasNextPage={hasNextPage}
          isFetchingNextPage={isFetchingNextPage}
          fetchNextPage={fetchNextPage}
          selectable
        />
      )}

      {projectId && <BulkActionBar projectId={projectId} onEdit={setEditId} />}
      {projectId && (
        <SampleEditDrawer projectId={projectId} sample={editSample} onClose={() => setEditId(null)} />
      )}
    </div>
  )
}
