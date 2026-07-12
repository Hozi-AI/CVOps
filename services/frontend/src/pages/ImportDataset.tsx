import { useRef, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { zipSync } from 'fflate'
import { useOntologies } from '../api/ontologies'
import { useImportFromFolder } from '../api/imports'
import { putWithProgress } from '../lib/upload'
import { sha256Hex } from '../lib/hash'
import { client } from '../lib/client'
import { toast } from '../store/toast'
import { Button, Spinner } from '../components/ui'
import { Field, Input, Select } from '../components/ui/Field'

type Tab = 'upload' | 'server-path'

async function folderToZip(files: File[]): Promise<Blob> {
  const entries: Record<string, Uint8Array> = {}
  for (const file of files) {
    entries[file.webkitRelativePath] = new Uint8Array(await file.arrayBuffer())
  }
  return new Blob([zipSync(entries)], { type: 'application/zip' })
}

export default function ImportDataset() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { data: ontologies = [], isLoading: ontsLoading } = useOntologies()

  const [tab, setTab] = useState<Tab>('upload')
  const [zipFile, setZipFile] = useState<File | null>(null)
  const [folderFiles, setFolderFiles] = useState<File[] | null>(null)
  const [folderPath, setFolderPath] = useState('')
  const [ontologyId, setOntologyId] = useState('')
  const [datasetName, setDatasetName] = useState('Imported Dataset')
  const [format, setFormat] = useState('auto')
  const [review, setReview] = useState(false)
  const [trainPct, setTrainPct] = useState(70)
  const [valPct, setValPct] = useState(15)
  const testPct = Math.max(0, 100 - trainPct - valPct)
  const splitStrategy = { train_ratio: trainPct / 100, val_ratio: valPct / 100 }
  const [progress, setProgress] = useState<number | null>(null)
  const [zipping, setZipping] = useState(false)
  const [busy, setBusy] = useState(false)

  const zipRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)

  const importFolder = useImportFromFolder(projectId)

  function clearSelection() {
    setZipFile(null)
    setFolderFiles(null)
    if (zipRef.current) zipRef.current.value = ''
    if (folderRef.current) folderRef.current.value = ''
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!projectId || !ontologyId) return
    setBusy(true)
    setProgress(null)

    try {
      if (tab === 'upload') {
        let blob: Blob
        if (folderFiles) {
          setZipping(true)
          blob = await folderToZip(folderFiles)
          setZipping(false)
        } else if (zipFile) {
          blob = zipFile
        } else {
          return
        }

        const hex = await sha256Hex(blob)
        const blobHash = `sha256:${hex}`

        const { data: urlData } = await client.post<{ upload_url: string }>(
          `/projects/${projectId}/imports/upload-url`,
          { blob_hash: blobHash },
        )
        await putWithProgress(urlData.upload_url, blob, (f) => setProgress(f))

        const { data: run } = await client.post(`/projects/${projectId}/imports`, {
          blob_hash: blobHash,
          ontology_id: ontologyId,
          dataset_name: datasetName,
          format,
          review,
          split_strategy: splitStrategy,
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
          splitStrategy,
        })
        toast.success('Import started', `Run ${run.id.slice(0, 8)} dispatched`)
        navigate(`/projects/${projectId}/runs`)
      }
    } catch (err) {
      toast.error('Import failed', err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
      setZipping(false)
      setProgress(null)
    }
  }

  const tabCls = (t: Tab) =>
    `px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
      tab === t
        ? 'border-iris-400 text-text-primary'
        : 'border-transparent text-text-muted hover:text-text-primary'
    }`

  const hasSource = tab === 'upload' ? !!(zipFile || folderFiles) : folderPath.trim().length > 0
  const canSubmit = !!ontologyId && !busy && hasSource

  const selectionLabel = zipFile
    ? zipFile.name
    : folderFiles
      ? `${folderFiles[0]?.webkitRelativePath.split('/')[0] ?? 'folder'} (${folderFiles.length} files)`
      : null

  const buttonLabel = busy
    ? zipping
      ? 'Zipping…'
      : progress !== null
        ? `Uploading ${Math.round(progress * 100)}%…`
        : 'Dispatching…'
    : 'Import'

  return (
    <div className="p-6 max-w-xl mx-auto">
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

      <div className="flex border-b border-border mb-6">
        <button type="button" className={tabCls('upload')} onClick={() => setTab('upload')}>
          Upload
        </button>
        <button type="button" className={tabCls('server-path')} onClick={() => setTab('server-path')}>
          Server folder path
        </button>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {tab === 'upload' ? (
          <Field label="Dataset source" htmlFor="zip-input">
            <div className="border-2 border-dashed border-border rounded-lg p-5 space-y-3">
              {selectionLabel ? (
                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-primary truncate">{selectionLabel}</span>
                  <button
                    type="button"
                    onClick={clearSelection}
                    className="ml-2 flex-shrink-0 text-text-muted hover:text-text-primary"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <p className="text-sm text-text-muted text-center">
                  Select a zip file or a local folder
                </p>
              )}
              <div className="flex gap-2 justify-center">
                <Button type="button" size="sm" variant="secondary" onClick={() => zipRef.current?.click()}>
                  Select zip
                </Button>
                <Button type="button" size="sm" variant="secondary" onClick={() => folderRef.current?.click()}>
                  Select folder
                </Button>
              </div>
            </div>

            <input
              id="zip-input"
              ref={zipRef}
              type="file"
              accept=".zip"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0] ?? null
                setFolderFiles(null)
                setZipFile(f)
              }}
            />
            {/* ponytail: webkitdirectory is not in React types — spread as untyped attr */}
            <input
              ref={folderRef}
              type="file"
              multiple
              className="hidden"
              {...({ webkitdirectory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
              onChange={(e) => {
                const files = e.target.files ? Array.from(e.target.files) : null
                setZipFile(null)
                setFolderFiles(files?.length ? files : null)
              }}
            />
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

        <Field label="Dataset name" htmlFor="datasetName">
          <Input
            id="datasetName"
            value={datasetName}
            onChange={(e) => setDatasetName(e.target.value)}
          />
        </Field>

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

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-text-secondary">Split <span className="font-normal text-text-muted">(fallback — ignored if dataset has train/val/test folders)</span></legend>
          <div className="flex gap-3">
            <Field label={`Train ${trainPct}%`} htmlFor="train-pct">
              <Input id="train-pct" type="number" min={0} max={100} value={trainPct}
                onChange={(e) => setTrainPct(Number(e.target.value))} />
            </Field>
            <Field label={`Val ${valPct}%`} htmlFor="val-pct">
              <Input id="val-pct" type="number" min={0} max={100} value={valPct}
                onChange={(e) => setValPct(Number(e.target.value))} />
            </Field>
            <Field label={`Test ${testPct}%`} htmlFor="test-pct">
              <Input id="test-pct" type="number" value={testPct} readOnly className="opacity-50" />
            </Field>
          </div>
        </fieldset>

        <label className="flex items-center gap-2 text-sm text-text-primary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={review}
            onChange={(e) => setReview(e.target.checked)}
            className="rounded"
          />
          Send to CVAT for human review before committing
        </label>

        {(zipping || progress !== null) && (
          <div className="h-1.5 bg-surface-3 rounded-full overflow-hidden">
            <div
              className="h-full bg-iris-400 transition-all"
              style={{ width: zipping ? '100%' : `${Math.round((progress ?? 0) * 100)}%` }}
            />
          </div>
        )}

        <Button type="submit" disabled={!canSubmit} loading={busy} className="w-full">
          {buttonLabel}
        </Button>
      </form>
    </div>
  )
}
