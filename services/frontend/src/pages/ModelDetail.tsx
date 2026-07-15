import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModel, usePatchModel, useWeightsUrl } from '../api/models'
import { usePinProject } from '../lib/useActiveProject'
import { Breadcrumbs, Button, Card, ErrorState, Field, Input, SkeletonList } from '../components/ui'
import { mlflowRunUrl } from '../lib/mlflow'
import { formatValue } from '../lib/format'
import { toast } from '../store/toast'

export default function ModelDetail() {
  const { id } = useParams<{ id: string }>()
  const { data: model, isLoading, isError, refetch } = useModel(id)
  const { data: weightsUrl } = useWeightsUrl(id)
  const patch = usePatchModel(id!)
  usePinProject(model?.project_id)

  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editMlflow, setEditMlflow] = useState('')

  function startEdit() {
    setEditName(model?.name ?? '')
    setEditDesc(model?.description ?? '')
    setEditMlflow(model?.mlflow_run_id ?? '')
    setEditing(true)
  }

  async function saveEdit() {
    try {
      await patch.mutateAsync({ name: editName, description: editDesc, mlflow_run_id: editMlflow || undefined })
      setEditing(false)
      toast.success('Model updated')
    } catch {
      toast.error('Update failed')
    }
  }

  if (isLoading) {
    return <div className="mx-auto max-w-3xl p-6"><SkeletonList rows={3} /></div>
  }
  if (isError || !model) {
    return <div className="mx-auto max-w-3xl p-6"><ErrorState description="Could not load this model." onRetry={() => refetch()} /></div>
  }

  const mlflowUrl = model.mlflow_run_id
    ? mlflowRunUrl(model.mlflow_run_id, (model.metrics?.mlflow_experiment_id as string | undefined) ?? '0')
    : null
  const displayMetrics = model.metrics
    ? Object.entries(model.metrics).filter(([k]) => !k.startsWith('mlflow_'))
    : []

  return (
    <div className="mx-auto max-w-3xl p-6">
      <Breadcrumbs
        items={[
          { label: 'Models', to: `/projects/${model.project_id}/models` },
          { label: model.name ?? id?.slice(0, 8) ?? '', mono: !model.name },
        ]}
      />

      <Card className="mb-4 p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-text-primary">
            {model.name ?? <span className="font-mono text-sm">{id?.slice(0, 8)}</span>}
          </h2>
          <div className="flex gap-2">
            {!editing && <Button variant="secondary" onClick={startEdit}>Edit</Button>}
            {weightsUrl && (
              <a
                href={weightsUrl.url}
                className="rounded-lg bg-iris px-3 py-1.5 text-xs text-text-onAccent transition-colors hover:bg-iris-hover"
              >
                Download weights
              </a>
            )}
          </div>
        </div>

        {editing ? (
          <div className="flex flex-col gap-3">
            <Field label="Name">
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
            </Field>
            <Field label="Description">
              <Input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />
            </Field>
            <Field label="MLflow run ID">
              <Input value={editMlflow} onChange={(e) => setEditMlflow(e.target.value)} className="font-mono text-xs" />
            </Field>
            <div className="flex gap-2">
              <Button onClick={saveEdit} loading={patch.isPending}>Save</Button>
              <Button variant="secondary" onClick={() => setEditing(false)} disabled={patch.isPending}>Cancel</Button>
            </div>
          </div>
        ) : (
          <>
            {model.description && <p className="mb-4 text-sm text-text-secondary">{model.description}</p>}
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <div>
                <dt className="text-xs text-text-muted">Base model</dt>
                <dd className="mt-0.5 font-medium text-text-primary">{model.base_model ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Created</dt>
                <dd className="mt-0.5 font-medium text-text-primary">{new Date(model.created_at).toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Trained on commit</dt>
                <dd className="mt-0.5 font-mono text-xs font-medium text-text-primary">
                  {model.trained_on_commit_id ? (
                    <Link
                      to={`/projects/${model.project_id}/commits/${model.trained_on_commit_id}`}
                      className="text-iris-400 hover:opacity-80"
                    >
                      {model.trained_on_commit_id.slice(0, 8)} ↗
                    </Link>
                  ) : '—'}
                </dd>
              </div>
              {model.mlflow_run_id && (
                <div>
                  <dt className="text-xs text-text-muted">MLflow run</dt>
                  <dd className="mt-0.5 font-mono text-xs font-medium">
                    {mlflowUrl ? (
                      <a href={mlflowUrl} target="_blank" rel="noreferrer" className="text-iris-400 hover:opacity-80">
                        {model.mlflow_run_id.slice(0, 12)} ↗
                      </a>
                    ) : (
                      <span className="text-text-secondary">{model.mlflow_run_id.slice(0, 12)}</span>
                    )}
                  </dd>
                </div>
              )}
            </dl>
          </>
        )}
      </Card>

      {displayMetrics.length > 0 && (
        <Card className="mb-4 p-6">
          <h3 className="mb-3 text-sm font-bold text-text-secondary">Metrics</h3>
          <div className="grid grid-cols-3 gap-3">
            {displayMetrics.map(([k, v]) => (
              <div key={k} className="rounded-lg bg-surface-3 px-3 py-2">
                <p className="text-xs capitalize text-text-muted">{k.replace(/_/g, ' ')}</p>
                <p className="break-words text-lg font-bold text-text-primary">{formatValue(v)}</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      {model.hyperparams && Object.keys(model.hyperparams).length > 0 && (
        <Card className="p-6">
          <h3 className="mb-3 text-sm font-bold text-text-secondary">Hyperparameters</h3>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
            {Object.entries(model.hyperparams).map(([k, v]) => (
              <div key={k}>
                <dt className="text-xs capitalize text-text-muted">{k.replace(/_/g, ' ')}</dt>
                <dd className="mt-0.5 break-words font-medium text-text-primary">{formatValue(v)}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  )
}
