import { useMemo, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useCommit, useCommitSamples, useDataset, useTrainCommit, useDownloadCommit } from '../api/datasets'
import { useTrainingContainers } from '../api/training-containers'
import { usePinProject } from '../lib/useActiveProject'
import { icdInputsToRjsfSchema } from '../lib/icdSchema'
import { CommitStats } from '../components/dataset/CommitStats'
import { SampleGrid } from '../components/dataset/SampleGrid'
import { StepConfigForm } from '../components/workflow/StepConfigForm'
import { Breadcrumbs, Button, Card, ErrorState, Select, SkeletonList } from '../components/ui'

interface HyperparamRow {
  key: string
  value: string
}

function TrainModal({
  datasetId,
  projectId,
  commitId,
  onClose,
}: {
  datasetId: string
  projectId: string | undefined
  commitId: string
  onClose: () => void
}) {
  const navigate = useNavigate()
  const train = useTrainCommit(datasetId)
  const { data: containers } = useTrainingContainers(projectId)
  const [gitUrl, setGitUrl] = useState('')
  const [entryPoint, setEntryPoint] = useState('train.py')
  const [branch, setBranch] = useState('')
  const [rows, setRows] = useState<HyperparamRow[]>([{ key: '', value: '' }])
  const [selectedContainerId, setSelectedContainerId] = useState('')
  // rjsf form data for the typed-hyperparam path.
  const [typedParams, setTypedParams] = useState<Record<string, unknown>>({})

  const selectedContainer = containers?.find(c => c.id === selectedContainerId)
  const typedSchema = useMemo(
    () => (selectedContainer ? icdInputsToRjsfSchema(selectedContainer.icd_config) : null),
    [selectedContainer],
  )

  const setRow = (i: number, patch: Partial<HyperparamRow>) =>
    setRows(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const hyperparams = typedSchema
      ? typedParams
      : Object.fromEntries(rows.filter(r => r.key.trim()).map(r => [r.key.trim(), r.value]))
    train.mutate(
      {
        commitId,
        git_url: gitUrl.trim(),
        entry_point: entryPoint.trim() || 'train.py',
        branch: branch.trim() || null,
        hyperparams: Object.keys(hyperparams).length ? (hyperparams as Record<string, string | number | boolean>) : null,
        training_container_id: selectedContainerId || undefined,
      },
      { onSuccess: run => navigate(`/runs/${run.id}`) },
    )
  }

  const inputCls =
    'w-full border border-border-strong bg-surface-2 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-iris'

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-6"
      onClick={onClose}
    >
      <form
        onClick={e => e.stopPropagation()}
        onSubmit={submit}
        className="bg-surface-2 rounded-xl border border-border shadow-xl p-6 w-full max-w-md max-h-[85vh] overflow-y-auto"
      >
        <h3 className="text-lg font-bold text-text-primary mb-4">Train this commit</h3>

        <label className="block text-xs text-text-secondary mb-1">Training environment</label>
        <Select
          value={selectedContainerId}
          onChange={e => setSelectedContainerId(e.target.value)}
          className="mb-3"
        >
          <option value="">Ad-hoc (git repo)</option>
          {(containers ?? []).map(c => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </Select>

        <label className="block text-xs text-text-secondary mb-1">Git repository URL</label>
        <input
          autoFocus
          required
          value={gitUrl}
          onChange={e => setGitUrl(e.target.value)}
          placeholder="https://github.com/org/trainer.git"
          className={`${inputCls} mb-3`}
        />

        <div className="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label className="block text-xs text-text-secondary mb-1">Entry point</label>
            <input
              value={entryPoint}
              onChange={e => setEntryPoint(e.target.value)}
              placeholder="train.py"
              className={inputCls}
            />
          </div>
          <div>
            <label className="block text-xs text-text-secondary mb-1">Branch (optional)</label>
            <input
              value={branch}
              onChange={e => setBranch(e.target.value)}
              placeholder="main"
              className={inputCls}
            />
          </div>
        </div>

        <label className="block text-xs text-text-secondary mb-1">Hyperparameters (optional)</label>
        {typedSchema ? (
          <div className="mb-3">
            <StepConfigForm schema={typedSchema} formData={typedParams} onChange={setTypedParams} />
          </div>
        ) : (
          <div className="space-y-2 mb-3">
            {rows.map((row, i) => (
              <div key={i} className="flex gap-2">
                <input
                  value={row.key}
                  onChange={e => setRow(i, { key: e.target.value })}
                  placeholder="epochs"
                  className="flex-1 min-w-0 border border-border-strong bg-surface-2 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-iris"
                />
                <input
                  value={row.value}
                  onChange={e => setRow(i, { value: e.target.value })}
                  placeholder="10"
                  className="flex-1 min-w-0 border border-border-strong bg-surface-2 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-iris"
                />
              </div>
            ))}
            <button
              type="button"
              onClick={() => setRows([...rows, { key: '', value: '' }])}
              className="text-xs text-iris-400 hover:text-iris"
            >
              + Add hyperparameter
            </button>
          </div>
        )}

        {train.isError && (
          <p className="text-xs text-error mb-3">
            {(train.error as Error)?.message ?? 'Failed to start training'}
          </p>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={train.isPending} disabled={!gitUrl.trim()}>
            {train.isPending ? 'Starting…' : 'Start training'}
          </Button>
        </div>
      </form>
    </div>
  )
}

export default function CommitDetail() {
  const { id: datasetId, cid: commitId } = useParams<{ id: string; cid: string }>()
  const { data: commit, isLoading, isError, refetch } = useCommit(datasetId, commitId)
  const { data: dataset } = useDataset(datasetId)
  const commitSamples = useCommitSamples(datasetId, commitId)
  const [trainOpen, setTrainOpen] = useState(false)
  const dl = useDownloadCommit(datasetId, commitId)
  usePinProject(dataset?.project_id)

  if (isLoading) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <SkeletonList rows={3} />
      </div>
    )
  }

  if (isError || !commit) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <ErrorState description="Could not load this commit." onRetry={() => refetch()} />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-6 flex items-center justify-between">
        <Breadcrumbs
          items={[
            { label: 'Dataset', to: `/datasets/${datasetId}` },
            { label: commitId?.slice(0, 8) ?? '', mono: true },
          ]}
        />
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" loading={dl.busy} onClick={dl.trigger}>
            {dl.label}
          </Button>
          <Button size="sm" onClick={() => setTrainOpen(true)}>
            Train
          </Button>
        </div>
      </div>

      <Card className="mb-4 p-6">
        <h2 className="mb-1 text-lg font-bold text-text-primary">{commit.message ?? 'Commit'}</h2>
        <p className="text-xs text-text-muted">{new Date(commit.created_at).toLocaleString()}</p>

        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-xs text-text-muted">Ontology version</dt>
            <dd className="mt-0.5 font-medium text-text-primary">v{commit.ontology_version}</dd>
          </div>
          {commit.stats?.sample_count != null && (
            <div>
              <dt className="text-xs text-text-muted">Samples</dt>
              <dd className="mt-0.5 font-medium text-text-primary">{String(commit.stats.sample_count)}</dd>
            </div>
          )}
        </dl>
      </Card>

      <CommitStats stats={commit.stats} />

      <div className="mt-6">
        <h3 className="mb-3 text-sm font-bold text-text-primary">Samples</h3>
        <SampleGrid
          data={commitSamples.data}
          isLoading={commitSamples.isLoading}
          hasNextPage={commitSamples.hasNextPage}
          isFetchingNextPage={commitSamples.isFetchingNextPage}
          fetchNextPage={commitSamples.fetchNextPage}
        />
      </div>

      {trainOpen && datasetId && commitId && (
        <TrainModal
          datasetId={datasetId}
          projectId={dataset?.project_id}
          commitId={commitId}
          onClose={() => setTrainOpen(false)}
        />
      )}
    </div>
  )
}
