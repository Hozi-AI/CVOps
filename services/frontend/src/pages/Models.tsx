import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModels, useUploadModel, useDeployModelToCvat } from '../api/models'
import { useDatasets, useCommits } from '../api/datasets'
import { toast } from '../store/toast'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, Field, Input, Label, Select, SkeletonList } from '../components/ui'
import { formatValue } from '../lib/format'

export default function Models() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: models, isLoading, isError, refetch } = useModels(projectId)
  const { data: datasets } = useDatasets(projectId)
  const upload = useUploadModel(projectId!)
  const deployCvat = useDeployModelToCvat()

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [baseModel, setBaseModel] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [commitId, setCommitId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

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
      // Global mutationCache.onError surfaces the specific error.
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
      {models?.length === 0 && (
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
                    const name = window.prompt('CVAT model name', m.name || m.base_model || 'model')
                    if (!name) return
                    try {
                      await deployCvat.mutateAsync({ modelId: m.id, modelName: name })
                      toast.success(`Deployed "${name}" to CVAT`)
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
    </div>
  )
}
