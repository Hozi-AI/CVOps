import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModel, useModelArtifacts, usePatchModel, useUploadArtifact, useWeightsUrl } from '../api/models'
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

  const { data: artifacts } = useModelArtifacts(id)
  const uploadArtifact = useUploadArtifact(id!)
  const dropRef = useRef<HTMLDivElement>(null)

  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editMlflow, setEditMlflow] = useState('')

  async function handleFiles(files: FileList | null) {
    if (!files) return
    for (const file of Array.from(files)) {
      try {
        await uploadArtifact.mutateAsync(file)
      } catch {
        toast.error(`Failed to upload ${file.name}`)
      }
    }
  }

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

      {/* Artifact gallery */}
      <Card className="mt-4 p-6">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-bold text-text-secondary">Training Artifacts</h3>
          <label className="cursor-pointer rounded-lg border border-border px-3 py-1 text-xs text-text-muted transition-colors hover:border-iris hover:text-iris-400">
            + Add files
            <input
              type="file"
              multiple
              className="hidden"
              onChange={(e) => handleFiles(e.target.files)}
            />
          </label>
        </div>

        {/* Drop zone */}
        <div
          ref={dropRef}
          onDragOver={(e) => { e.preventDefault(); dropRef.current?.classList.add('border-iris') }}
          onDragLeave={() => dropRef.current?.classList.remove('border-iris')}
          onDrop={(e) => { e.preventDefault(); dropRef.current?.classList.remove('border-iris'); handleFiles(e.dataTransfer.files) }}
          className="mb-4 rounded-lg border-2 border-dashed border-border py-6 text-center text-xs text-text-muted transition-colors"
        >
          Drop training plots, CSVs, or any run files here
        </div>

        {uploadArtifact.isPending && (
          <p className="mb-3 text-xs text-text-muted">Uploading…</p>
        )}

        {artifacts && artifacts.length === 0 && (
          <p className="text-xs text-text-muted">No artifacts yet.</p>
        )}

        {artifacts && artifacts.length > 0 && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {artifacts.map((a) => (
              <div key={a.id} className="overflow-hidden rounded-lg border border-border">
                {a.mime_type?.startsWith('image/') && a.url ? (
                  <a href={a.url} target="_blank" rel="noreferrer">
                    <img src={a.url} alt={a.filename} className="h-36 w-full object-cover" />
                  </a>
                ) : (
                  <div className="flex h-36 items-center justify-center bg-surface-3">
                    <span className="text-3xl">📄</span>
                  </div>
                )}
                <div className="px-2 py-1.5">
                  <p className="truncate text-xs text-text-secondary" title={a.filename}>{a.filename}</p>
                  {a.url && (
                    <a href={a.url} target="_blank" rel="noreferrer" className="text-xs text-iris-400 hover:opacity-80">
                      Download
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
