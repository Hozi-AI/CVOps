import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useDatasets } from '../api/datasets'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, SkeletonList } from '../components/ui'
import { ImportDatasetDialog } from '../components/dataset/ImportDatasetDialog'

export default function Datasets() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: datasets, isLoading, isError, refetch } = useDatasets(projectId)
  const [importing, setImporting] = useState(false)

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
            <Link key={d.id} to={`/datasets/${d.id}`}>
              <Card className="flex items-center justify-between px-5 py-4 transition-all hover:border-iris hover:shadow-md">
                <div>
                  <p className="font-semibold text-text-primary">{d.name}</p>
                  <p className="mt-0.5 text-xs text-text-muted">{new Date(d.created_at).toLocaleDateString()}</p>
                </div>
                <span className="text-lg text-text-muted">›</span>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
