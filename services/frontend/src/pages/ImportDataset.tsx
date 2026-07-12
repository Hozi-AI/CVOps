import { useRef, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useOntologies } from '../api/ontologies'
import { useImportFromFolder } from '../api/imports'
import { putWithProgress } from '../lib/upload'
import { sha256Hex } from '../lib/hash'
import { client } from '../lib/client'
import { toast } from '../store/toast'
import { Button, Spinner } from '../components/ui'
import { Field, Input, Select } from '../components/ui/Field'

type Tab = 'zip' | 'folder'

export default function ImportDataset() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { data: ontologies = [], isLoading: ontsLoading } = useOntologies()

  const [tab, setTab] = useState<Tab>('zip')
  const [file, setFile] = useState<File | null>(null)
  const [folderPath, setFolderPath] = useState('')
  const [ontologyId, setOntologyId] = useState('')
  const [datasetName, setDatasetName] = useState('Imported Dataset')
  const [format, setFormat] = useState('auto')
  const [review, setReview] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  const fileRef = useRef<HTMLInputElement>(null)

  const importFolder = useImportFromFolder(projectId)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!projectId || !ontologyId) return
    setBusy(true)
    setProgress(null)

    try {
      if (tab === 'zip') {
        if (!file) return
        const hex = await sha256Hex(file)
        const blobHash = `sha256:${hex}`

        // Get presigned PUT URL
        const { data: urlData } = await client.post<{ upload_url: string }>(
          `/projects/${projectId}/imports/upload-url`,
          { blob_hash: blobHash },
        )

        // Upload zip with progress
        await putWithProgress(urlData.upload_url, file, (f) => setProgress(f))

        // Dispatch import run
        const { data: run } = await client.post(`/projects/${projectId}/imports`, {
          blob_hash: blobHash,
          ontology_id: ontologyId,
          dataset_name: datasetName,
          format,
          review,
        })
        toast.success('Import started', `Run ${run.id.slice(0, 8)} dispatched`)
        navigate(`/projects/${projectId}/runs`)
      } else {
        const run = await importFolder.mutateAsync({
          folderPath,
          ontologyId,
          datasetName,
          format,
          review,
        })
        toast.success('Import started', `Run ${run.id.slice(0, 8)} dispatched`)
        navigate(`/projects/${projectId}/runs`)
      }
    } catch (err) {
      toast.error('Import failed', err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  const tabCls = (t: Tab) =>
    `px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
      tab === t
        ? 'border-iris-400 text-text-primary'
        : 'border-transparent text-text-muted hover:text-text-primary'
    }`

  const canSubmit =
    !!ontologyId &&
    !busy &&
    (tab === 'zip' ? !!file : folderPath.trim().length > 0)

  return (
    <div className="p-6 max-w-xl">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-2 text-sm text-text-muted mb-1">
          <Link to={`/projects/${projectId}`} className="hover:text-text-primary">
            Project
          </Link>
          <span>/</span>
          <span className="text-text-primary">Import Dataset</span>
        </div>
        <h1 className="text-xl font-semibold text-text-primary">Import Dataset</h1>
        <p className="text-sm text-text-secondary mt-1">
          Import an existing labeled dataset (YOLO, COCO, or raw images).
        </p>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border mb-6">
        <button className={tabCls('zip')} onClick={() => setTab('zip')}>
          Upload zip
        </button>
        <button className={tabCls('folder')} onClick={() => setTab('folder')}>
          Server folder path
        </button>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Source */}
        {tab === 'zip' ? (
          <Field label="Dataset zip file" htmlFor="file">
            <div
              className="border-2 border-dashed border-border rounded-lg p-6 text-center cursor-pointer hover:border-iris-400 transition-colors"
              onClick={() => fileRef.current?.click()}
            >
              {file ? (
                <p className="text-sm text-text-primary">{file.name}</p>
              ) : (
                <p className="text-sm text-text-muted">
                  Click to select a zip file containing your dataset
                </p>
              )}
              <input
                id="file"
                ref={fileRef}
                type="file"
                accept=".zip"
                className="hidden"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </div>
          </Field>
        ) : (
          <Field label="Server-side folder path" htmlFor="folderPath">
            <Input
              id="folderPath"
              placeholder="/data/my-dataset"
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
            />
          </Field>
        )}

        {/* Dataset name */}
        <Field label="Dataset name" htmlFor="datasetName">
          <Input
            id="datasetName"
            value={datasetName}
            onChange={(e) => setDatasetName(e.target.value)}
          />
        </Field>

        {/* Ontology */}
        <Field label="Label set (ontology)" htmlFor="ontologyId">
          {ontsLoading ? (
            <div className="flex items-center gap-2 py-2 text-sm text-text-muted">
              <Spinner className="h-4 w-4" /> Loading…
            </div>
          ) : (
            <Select
              id="ontologyId"
              value={ontologyId}
              onChange={(e) => setOntologyId(e.target.value)}
            >
              <option value="">— select a label set —</option>
              {ontologies.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </Select>
          )}
        </Field>

        {/* Format */}
        <Field label="Format" htmlFor="format">
          <Select
            id="format"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            <option value="auto">Auto-detect</option>
            <option value="yolo">YOLO</option>
            <option value="coco">COCO</option>
            <option value="raw">Raw images (no annotations)</option>
          </Select>
        </Field>

        {/* Review gate */}
        <label className="flex items-center gap-2 text-sm text-text-primary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={review}
            onChange={(e) => setReview(e.target.checked)}
            className="rounded"
          />
          Send to CVAT for human review before committing
        </label>

        {/* Progress bar */}
        {progress !== null && (
          <div className="h-1.5 bg-surface-3 rounded-full overflow-hidden">
            <div
              className="h-full bg-iris-400 transition-all"
              style={{ width: `${Math.round((progress ?? 0) * 100)}%` }}
            />
          </div>
        )}

        <Button type="submit" disabled={!canSubmit} loading={busy} className="w-full">
          {busy ? (progress !== null ? `Uploading ${Math.round((progress ?? 0) * 100)}%…` : 'Dispatching…') : 'Import'}
        </Button>
      </form>
    </div>
  )
}
