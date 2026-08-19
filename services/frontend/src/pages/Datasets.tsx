import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useDatasets, useDeleteDataset, type Dataset } from '../api/datasets'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, SkeletonList } from '../components/ui'
import { ImportDatasetDialog } from '../components/dataset/ImportDatasetDialog'

export default function Datasets() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: datasets, isLoading, isError, refetch } = useDatasets(projectId)
  const [importing, setImporting] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const deleteDataset = useDeleteDataset()

  return (
    <div className="mx-auto max-w-5xl p-6">
      <Breadcrumbs
        items={[{ label: 'Project', to: `/projects/${projectId}` }, { label: 'Datasets' }]}
      />

      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold text-text-primary">Datasets</h2>
        <Button size="sm" onClick={() => setImporting(true)}>Import Dataset</Button>
      </div>

      {projectId && (
        <ImportDatasetDialog
          projectId={projectId}
          open={importing}
          onClose={() => setImporting(false)}
        />
      )}

      {isLoading && <SkeletonList rows={3} />}

      {isError && (
        <ErrorState description="Could not load datasets for this project." onRetry={() => refetch()} />
      )}

      {datasets && datasets.length === 0 && (
        <EmptyState
          title="No datasets yet"
          description="Datasets are created by workflow commit steps."
        />
      )}

      {datasets && datasets.length > 0 && (
        <div className="space-y-2">
          {datasets.map((d) => (
            <DatasetRow
              key={d.id}
              dataset={d}
              confirming={confirmId === d.id}
              deleting={deleteDataset.isPending && confirmId === d.id}
              onConfirm={() => setConfirmId(d.id)}
              onCancel={() => setConfirmId(null)}
              onDelete={() => {
                deleteDataset.mutate(d, { onSuccess: () => setConfirmId(null) })
              }}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function DatasetRow({
  dataset,
  confirming,
  deleting,
  onConfirm,
  onCancel,
  onDelete,
}: {
  dataset: Dataset
  confirming: boolean
  deleting: boolean
  onConfirm: () => void
  onCancel: () => void
  onDelete: () => void
}) {
  return (
    <Card className="flex items-center justify-between px-5 py-4">
      <Link to={`/datasets/${dataset.id}`} className="flex-1 min-w-0">
        <p className="font-semibold text-text-primary">{dataset.name}</p>
        <p className="mt-0.5 text-xs text-text-muted">{new Date(dataset.created_at).toLocaleDateString()}</p>
      </Link>
      <div className="flex items-center gap-2 ml-4 shrink-0">
        {confirming ? (
          <>
            <span className="text-sm text-text-secondary">Delete?</span>
            <button
              onClick={onDelete}
              disabled={deleting}
              className="bg-error text-white px-3 py-1 rounded-lg text-sm font-medium hover:bg-error/90 disabled:opacity-60"
            >
              {deleting ? 'Deleting…' : 'Yes'}
            </button>
            <button
              onClick={onCancel}
              className="border border-border-strong text-text-secondary px-3 py-1 rounded-lg text-sm hover:bg-surface-3"
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <Link to={`/datasets/${dataset.id}`} className="text-lg text-text-muted">›</Link>
            <button
              onClick={(e) => { e.preventDefault(); onConfirm() }}
              className="text-xs text-error/70 hover:text-error px-2 py-1 rounded hover:bg-error/10 transition-colors"
            >
              Delete
            </button>
          </>
        )}
      </div>
    </Card>
  )
}
