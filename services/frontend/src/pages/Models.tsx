import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModels, useUploadModel } from '../api/models'
import { toast } from '../store/toast'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, Field, Input, Label, SkeletonList } from '../components/ui'
import { formatValue } from '../lib/format'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export default function Models() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: models, isLoading, isError, refetch } = useModels(projectId)
  const upload = useUploadModel(projectId!)

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [baseModel, setBaseModel] = useState('')
  const [commitId, setCommitId] = useState('')
  const [commitIdTouched, setCommitIdTouched] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const commitIdInvalid = !!commitId && !UUID_RE.test(commitId)
  const commitIdError = commitIdTouched && commitIdInvalid ? 'Must be a valid UUID' : ''

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault()
    if (!file || !projectId || commitIdError) return
    const toastId = toast.info(`Uploading "${name || file.name}"…`, 'Computing hash and uploading', 0)
    try {
      await upload.mutateAsync({ file, name, description, baseModel, trainedOnCommitId: commitId })
      toast.dismiss(toastId)
      toast.success('Model version uploaded')
      setShowForm(false)
      setName(''); setDescription(''); setBaseModel(''); setCommitId(''); setCommitIdTouched(false); setFile(null)
      if (fileRef.current) fileRef.current.value = ''
    } catch {
      toast.dismiss(toastId)
      // Global mutationCache.onError shows the specific error; no redundant toast here.
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
            <Field label="Dataset commit ID (optional)">
              <Input
                value={commitId}
                onChange={(e) => setCommitId(e.target.value)}
                onBlur={() => setCommitIdTouched(true)}
                placeholder="Paste commit UUID"
                className={`font-mono text-xs${commitIdError ? ' border-error' : ''}`}
              />
              {commitIdError && <p className="mt-1 text-xs text-error">{commitIdError}</p>}
            </Field>
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
              <Button type="submit" loading={upload.isPending} disabled={!file || commitIdInvalid}>
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
            <Link key={m.id} to={`/models/${m.id}`}>
              <Card className="flex items-center justify-between px-5 py-4 transition-all hover:border-iris hover:shadow-md">
                <div>
                  <p className="font-semibold text-text-primary">{m.name ?? <span className="font-mono text-sm">{m.id.slice(0, 8)}…</span>}</p>
                  <p className="mt-0.5 text-xs text-text-muted">
                    {m.base_model ?? 'Unknown base'} · {new Date(m.created_at).toLocaleDateString()}
                  </p>
                  {m.description && <p className="mt-1 text-xs text-text-secondary">{m.description}</p>}
                </div>
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
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
