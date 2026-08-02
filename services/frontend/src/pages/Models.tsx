import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModels, useUploadModel, useDeployModelToCvat } from '../api/models'
import { useDatasets, useCommits } from '../api/datasets'
import { useCvatModels, useDeleteCvatModel } from '../api/cvat'
import { toast } from '../store/toast'
import { Badge, Breadcrumbs, Button, Card, Dialog, EmptyState, ErrorState, Field, Input, Label, Select, SkeletonList, Spinner } from '../components/ui'
import { formatValue } from '../lib/format'

export default function Models() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: models, isLoading, isError, refetch } = useModels(projectId)
  const { data: datasets } = useDatasets(projectId)
  const upload = useUploadModel(projectId!)
  const deployCvat = useDeployModelToCvat()
  const { data: cvatModels, isLoading: cvatLoading } = useCvatModels()
  const deleteModel = useDeleteCvatModel()

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [baseModel, setBaseModel] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [commitId, setCommitId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [pendingDelete, setPendingDelete] = useState<{ id: string; name: string } | null>(null)

  const { data: commitsData } = useCommits(datasetId || undefined)
  const commits = commitsData?.pages.flatMap((p) => p.items) ?? []

  function resetForm() {
    setName(''); setDescription(''); setBaseModel('')
    setDatasetId(''); setCommitId(''); setFile(null)
    if (fileRef.current) fileRef.current.value = ''
  }

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault()
    if (!file || !projectId) return
    const toastId = toast.info(`Uploading "${name || file.name}"…`, 'Computing hash and uploading', 0)
    try {
      await upload.mutateAsync({ file, name, description, baseModel, trainedOnCommitId: commitId })
      toast.dismiss(toastId)
      toast.success('Model version uploaded')
      setShowForm(false)
      resetForm()
    } catch {
      toast.dismiss(toastId)
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return
    const { id, name: modelName } = pendingDelete
    setPendingDelete(null)
    const toastId = toast.info(`Deleting "${modelName}"…`, undefined, 0)
    try {
      await deleteModel.mutateAsync(id)
      toast.dismiss(toastId)
      toast.success(`Model "${modelName}" deleted from CVAT`)
    } catch {
      toast.dismiss(toastId)
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <Breadcrumbs
        items={[{ label: 'Project', to: `/projects/${projectId}` }, { label: 'Models' }]}
      />

      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-bold text-text-primary">Models</h2>
        <Button onClick={() => setShowForm((v) => !v)}>+ Upload Model</Button>
      </div>

      {showForm && (
        <Card className="mb-6 p-5">
          <form onSubmit={handleUpload} className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. yolov8n-v2" />
              </Field>
              <Field label="Base model">
                <Input value={baseModel} onChange={(e) => setBaseModel(e.target.value)} placeholder="e.g. yolov8n" />
              </Field>
            </div>
            <Field label="Description">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What changed, what it was trained on…" />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Dataset (optional)">
                <Select
                  value={datasetId}
                  onChange={(e) => { setDatasetId(e.target.value); setCommitId('') }}
                >
                  <option value="">— none —</option>
                  {(datasets ?? []).map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Commit (optional)">
                <Select
                  value={commitId}
                  onChange={(e) => setCommitId(e.target.value)}
                  disabled={!datasetId}
                >
                  <option value="">— none —</option>
                  {commits.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.message ?? c.id.slice(0, 8)} · {new Date(c.created_at).toLocaleDateString()}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div>
              <Label>Weights file (.pt)</Label>
              <input
                required
                ref={fileRef}
                type="file"
                accept=".pt"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="w-full text-sm text-text-secondary file:mr-3 file:rounded-lg file:border-0 file:bg-iris/10 file:px-3 file:py-2 file:text-sm file:font-medium file:text-iris-400 hover:file:bg-iris/20"
              />
            </div>
            <div className="flex items-center gap-2">
              <Button type="submit" loading={upload.isPending} disabled={!file}>
                {upload.isPending ? 'Uploading…' : 'Upload'}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setShowForm(false)} disabled={upload.isPending}>
                Cancel
              </Button>
              {upload.isPending && (
                <span className="text-xs text-text-muted">Hashing file and uploading to storage…</span>
              )}
            </div>
          </form>
        </Card>
      )}

      {isLoading && <SkeletonList rows={3} />}
      {isError && <ErrorState description="Could not load models for this project." onRetry={() => refetch()} />}
      {!isLoading && !isError && models?.length === 0 && (
        <EmptyState title="No models yet" description='Upload a .pt file or run a training workflow.' />
      )}

      {models && models.length > 0 && (
        <div className="space-y-2">
          {models.map((m) => (
            <Card key={m.id} className="flex items-center justify-between px-5 py-4">
              <Link to={`/models/${m.id}`} className="min-w-0 flex-1">
                <p className="font-semibold text-text-primary hover:text-iris-400">{m.name ?? <span className="font-mono text-sm">{m.id.slice(0, 8)}…</span>}</p>
                <p className="mt-0.5 text-xs text-text-muted">
                  {m.base_model ?? 'Unknown base'} · {new Date(m.created_at).toLocaleDateString()}
                </p>
                {m.description && <p className="mt-1 text-xs text-text-secondary">{m.description}</p>}
              </Link>
              <div className="ml-4 flex items-center gap-3">
                {m.metrics && (
                  <div className="text-right">
                    {Object.entries(m.metrics)
                      .filter(([, v]) => v !== null && typeof v !== 'object')
                      .slice(0, 2)
                      .map(([k, v]) => (
                        <p key={k} className="text-xs text-text-muted">
                          {k}: <span className="font-medium text-text-secondary">{formatValue(v)}</span>
                        </p>
                      ))}
                  </div>
                )}
                <Button
                  variant="secondary"
                  disabled={deployCvat.isPending}
                  onClick={async (e) => {
                    e.preventDefault()
                    const deployName = window.prompt('CVAT model name', m.name || m.base_model || 'model')
                    if (!deployName) return
                    try {
                      await deployCvat.mutateAsync({ modelId: m.id, modelName: deployName })
                      toast.success(`Deployed "${deployName}" to CVAT`)
                    } catch {
                      // global error handler surfaces the message
                    }
                  }}
                >
                  Deploy to CVAT
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* ── Deployed in CVAT ──────────────────────────────────────────── */}
      <div className="mt-10">
        <h3 className="mb-3 text-base font-semibold text-text-primary">Deployed in CVAT</h3>

        {cvatLoading && (
          <div className="flex items-center gap-2 py-6 text-sm text-text-muted">
            <Spinner className="h-4 w-4" /> Loading…
          </div>
        )}

        {!cvatLoading && cvatModels?.length === 0 && (
          <p className="text-sm text-text-muted">No models currently deployed in CVAT.</p>
        )}

        {cvatModels && cvatModels.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {cvatModels.map((m) => (
              <Card key={m.id} className="px-5 py-4">
                <div className="flex items-start justify-between">
                  <div>
                    <p className="font-semibold text-text-primary">{m.name}</p>
                    <p className="mt-1 font-mono text-xs text-text-muted">{m.id}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge tone="info" className="capitalize">{m.kind || 'detector'}</Badge>
                    <button
                      onClick={() => setPendingDelete({ id: m.id, name: m.name })}
                      disabled={deleteModel.isPending}
                      className="text-text-muted transition-colors hover:text-error disabled:opacity-40"
                      title="Delete from CVAT"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                        <path fillRule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clipRule="evenodd" />
                      </svg>
                    </button>
                  </div>
                </div>
                {m.description && <p className="mt-2 text-xs text-text-secondary">{m.description}</p>}
              </Card>
            ))}
          </div>
        )}
      </div>

      <Dialog
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="Delete model"
      >
        <p className="text-sm text-text-secondary">
          Delete <span className="font-medium text-text-primary">{pendingDelete?.name}</span> from CVAT? This cannot be undone.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setPendingDelete(null)}>Cancel</Button>
          <Button variant="danger" loading={deleteModel.isPending} onClick={confirmDelete}>Delete</Button>
        </div>
      </Dialog>
    </div>
  )
}
