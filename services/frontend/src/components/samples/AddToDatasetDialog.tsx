import { useState } from 'react'
import { useDatasets, useCreateDataset, useCommitFromSamples } from '../../api/datasets'
import { toast } from '../../store/toast'
import { Button, Dialog, Field, Input, Select } from '../ui'

export function AddToDatasetDialog({
  projectId,
  sampleIds,
  open,
  onClose,
  onDone,
}: {
  projectId: string
  sampleIds: string[]
  open: boolean
  onClose: () => void
  onDone: () => void
}) {
  const { data: datasets } = useDatasets(projectId)
  const createDataset = useCreateDataset()
  const commit = useCommitFromSamples()

  const [mode, setMode] = useState<'existing' | 'new'>('existing')
  const [datasetId, setDatasetId] = useState('')
  const [newName, setNewName] = useState('')
  const [branch, setBranch] = useState('main')
  const [message, setMessage] = useState('')
  const [trainPct, setTrainPct] = useState(70)
  const [valPct, setValPct] = useState(15)
  const testPct = Math.max(0, 100 - trainPct - valPct)

  const busy = createDataset.isPending || commit.isPending

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    try {
      let dsId = datasetId
      if (mode === 'new') {
        const ds = await createDataset.mutateAsync({ projectId, name: newName })
        dsId = ds.id
      }
      if (!dsId) return
      const res = await commit.mutateAsync({
        datasetId: dsId,
        sample_ids: sampleIds,
        branch_name: branch || 'main',
        message: message.trim() || `Add ${sampleIds.length} samples`,
        split_strategy: { train_ratio: trainPct / 100, val_ratio: valPct / 100 },
      })
      toast.success(
        'Added to dataset',
        `${res.committed_count} committed${res.skipped_count ? `, ${res.skipped_count} skipped (unannotated)` : ''}`,
      )
      onDone()
      onClose()
    } catch {
      // Surfaced by the global mutation error handler.
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Add ${sampleIds.length} samples to a dataset`}>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="flex gap-2 text-sm">
          <label className="flex items-center gap-1.5 text-text-secondary">
            <input type="radio" checked={mode === 'existing'} onChange={() => setMode('existing')} />
            Existing
          </label>
          <label className="flex items-center gap-1.5 text-text-secondary">
            <input type="radio" checked={mode === 'new'} onChange={() => setMode('new')} />
            New
          </label>
        </div>

        {mode === 'existing' ? (
          <Field label="Dataset" htmlFor="ds-select">
            <Select id="ds-select" value={datasetId} onChange={(e) => setDatasetId(e.target.value)} required>
              <option value="">Select a dataset…</option>
              {datasets?.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : (
          <Field label="New dataset name" htmlFor="ds-name">
            <Input id="ds-name" value={newName} onChange={(e) => setNewName(e.target.value)} required placeholder="my-dataset" />
          </Field>
        )}

        <Field label="Branch" htmlFor="ds-branch">
          <Input id="ds-branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
        </Field>

        <Field label="Commit message (optional)" htmlFor="ds-message">
          <Input id="ds-message" value={message} onChange={(e) => setMessage(e.target.value)}
            placeholder={`Add ${sampleIds.length} samples`} />
        </Field>

        <fieldset className="space-y-2">
          <legend className="text-xs font-medium text-text-secondary mb-1">Split</legend>
          <div className="flex gap-3">
            <Field label={`Train ${trainPct}%`} htmlFor="train-pct" className="flex-1">
              <Input id="train-pct" type="number" min={0} max={100} value={trainPct}
                onChange={(e) => setTrainPct(Number(e.target.value))} />
            </Field>
            <Field label={`Val ${valPct}%`} htmlFor="val-pct" className="flex-1">
              <Input id="val-pct" type="number" min={0} max={100} value={valPct}
                onChange={(e) => setValPct(Number(e.target.value))} />
            </Field>
            <Field label={`Test ${testPct}%`} htmlFor="test-pct" className="flex-1">
              <Input id="test-pct" type="number" value={testPct} readOnly className="opacity-50" />
            </Field>
          </div>
        </fieldset>

        <p className="text-xs text-text-muted">
          Only annotated samples are committed; unannotated ones are skipped and reported.
        </p>

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={busy}>
            Add to dataset
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
