import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useCommitDiff, useCommitFromSamples, useCommitSamples, useDownloadCommit, type Commit } from '../../api/datasets'
import { useDataSources } from '../../api/data-sources'
import { useThumbnailUrl, type Sample } from '../../api/samples'
import { cn } from '../../lib/cn'
import { Button, EmptyState, Select } from '../ui'
import { CommitStats } from './CommitStats'
import { Lightbox } from './SampleGrid'

type GroupKey = 'source' | 'review' | 'annotation' | 'none'
type ViewKey = 'files' | 'changes'

const GROUP_OPTIONS: { value: GroupKey; label: string }[] = [
  { value: 'source', label: 'Group by source' },
  { value: 'review', label: 'Group by review status' },
  { value: 'annotation', label: 'Group by annotation' },
  { value: 'none', label: 'No grouping' },
]

function CommitThumb({ sample, onOpen }: { sample: Sample; onOpen: () => void }) {
  const { data } = useThumbnailUrl(sample.id)
  return (
    <button
      type="button"
      onClick={onOpen}
      onContextMenu={(e) => {
        e.preventDefault()
        onOpen()
      }}
      aria-label={`Open frame ${sample.frame_index ?? ''}`}
      className="group relative aspect-square overflow-hidden rounded-md border border-border bg-surface-3 transition-shadow hover:border-border-strong focus:outline-none focus:ring-2 focus:ring-focus"
    >
      {data?.url ? (
        <img src={data.url} alt={`frame ${sample.frame_index ?? ''}`} className="h-full w-full object-cover" loading="lazy" />
      ) : (
        <div className="flex h-full w-full items-center justify-center">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-border-strong border-t-text-secondary" />
        </div>
      )}
    </button>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={cn('h-4 w-4 transition-transform', open ? 'rotate-90' : '')}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m9 18 6-6-6-6" />
    </svg>
  )
}

/** A single thumbnail in the Changes "Added" grid (no lightbox wiring). */
function DiffThumb({ sample }: { sample: Sample }) {
  const { data } = useThumbnailUrl(sample.id)
  return (
    <div className="relative aspect-square overflow-hidden rounded-md border border-success/40 bg-surface-3">
      {data?.url ? (
        <img src={data.url} alt={`frame ${sample.frame_index ?? ''}`} className="h-full w-full object-cover" loading="lazy" />
      ) : (
        <div className="flex h-full w-full items-center justify-center">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-border-strong border-t-text-secondary" />
        </div>
      )}
    </div>
  )
}

/**
 * GitHub-style per-commit changeset: the diff of this commit against its parent.
 * "Added" shows thumbnails (resolved from the commit's cumulative samples);
 * "Removed" ids are gone from the current state, so they render as a plain list.
 */
function ChangesView({
  datasetId,
  commitId,
  parentCommitId,
  sampleById,
}: {
  datasetId: string
  commitId: string
  parentCommitId: string | null
  sampleById: Map<string, Sample>
}) {
  const diff = useCommitDiff(datasetId, parentCommitId, commitId)

  // First commit (no parent): everything is "added", so we don't need a request.
  if (!parentCommitId) {
    const added = [...sampleById.values()]
    return (
      <div className="space-y-4">
        <EmptyState
          title="Initial commit"
          description={`${added.length} file${added.length === 1 ? '' : 's'} added — this commit has no parent to diff against.`}
        />
        {added.length > 0 && (
          <section className="overflow-hidden rounded-xl border border-border bg-surface-2">
            <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
              <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
              <span className="text-sm font-medium text-success">Added</span>
              <span className="ml-auto rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-muted">{added.length}</span>
            </div>
            <div className="grid grid-cols-3 gap-2 px-4 py-4 sm:grid-cols-5 lg:grid-cols-6">
              {added.map((s) => (
                <DiffThumb key={s.id} sample={s} />
              ))}
            </div>
          </section>
        )}
      </div>
    )
  }

  if (diff.isLoading) {
    return <div className="py-12 text-center text-sm text-text-muted">Loading changes…</div>
  }
  if (diff.isError || !diff.data) {
    return (
      <EmptyState title="Could not load changes" description="The diff for this commit is unavailable." />
    )
  }

  const { added, removed, changed } = diff.data
  if (added.length === 0 && removed.length === 0 && changed.length === 0) {
    return <EmptyState title="No changes" description="This commit's dataset state matches its parent." />
  }

  return (
    <div className="space-y-4">
      {added.length > 0 && (
        <section className="overflow-hidden rounded-xl border border-border bg-surface-2">
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
            <span className="text-sm font-medium text-success">Added</span>
            <span className="ml-auto rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-muted">{added.length}</span>
          </div>
          <div className="grid grid-cols-3 gap-2 px-4 py-4 sm:grid-cols-5 lg:grid-cols-6">
            {added.map((id) => {
              const s = sampleById.get(id)
              return s ? (
                <DiffThumb key={id} sample={s} />
              ) : (
                <div
                  key={id}
                  className="flex aspect-square items-center justify-center rounded-md border border-success/40 bg-surface-3 px-1 text-center font-mono text-[10px] text-text-muted"
                >
                  {id.slice(0, 8)}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {removed.length > 0 && (
        <section className="overflow-hidden rounded-xl border border-border bg-surface-2">
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            <span className="h-2.5 w-2.5 rounded-full bg-error" aria-hidden="true" />
            <span className="text-sm font-medium text-error">Removed</span>
            <span className="ml-auto rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-muted">{removed.length}</span>
          </div>
          <ul className="divide-y divide-border">
            {removed.map((id) => (
              <li key={id} className="flex items-center gap-2 px-4 py-2 text-sm">
                <span className="text-error">−</span>
                <span className="font-mono text-text-secondary">{id.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {changed.length > 0 && (
        <section className="overflow-hidden rounded-xl border border-border bg-surface-2">
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            <span className="h-2.5 w-2.5 rounded-full bg-iris" aria-hidden="true" />
            <span className="text-sm font-medium text-text-primary">Changed</span>
            <span className="ml-auto rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-muted">{changed.length}</span>
          </div>
          <ul className="divide-y divide-border">
            {changed.map((id) => (
              <li key={id} className="flex items-center gap-2 px-4 py-2 text-sm">
                <span className="font-mono text-text-secondary">{id.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

export function CommitContents({
  datasetId,
  commitId,
  projectId,
  commit,
}: {
  datasetId: string
  commitId: string
  projectId: string
  commit: Commit | undefined
}) {
  const q = useCommitSamples(datasetId, commitId)
  const { data: sources } = useDataSources(projectId)
  const dl = useDownloadCommit(datasetId, commitId)
  const resplit = useCommitFromSamples()
  const [view, setView] = useState<ViewKey>('files')
  const [groupBy, setGroupBy] = useState<GroupKey>('source')
  const [openIndex, setOpenIndex] = useState<number | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [showResplit, setShowResplit] = useState(false)
  const [trainRatio, setTrainRatio] = useState(0.8)
  const [valRatio, setValRatio] = useState(0.2)

  // Auto-load remaining pages when re-split panel is open so we have all IDs.
  useEffect(() => {
    if (showResplit && q.hasNextPage && !q.isFetchingNextPage) q.fetchNextPage()
  }, [showResplit, q])

  async function handleResplit() {
    const sampleIds = samples.map((s) => s.id)
    await resplit.mutateAsync({
      datasetId,
      sample_ids: sampleIds,
      split_strategy: { train_ratio: trainRatio, val_ratio: valRatio },
      message: `re-split: train ${Math.round(trainRatio * 100)}% val ${Math.round(valRatio * 100)}%`,
    })
    setShowResplit(false)
  }

  const effectiveValMax = Math.min(0.5, Math.max(0.05, +(1 - trainRatio).toFixed(2)))
  const testPct = Math.max(0, Math.round((1 - trainRatio - valRatio) * 100))

  const samples = useMemo(() => q.data?.pages.flatMap((p) => p.items) ?? [], [q.data])

  // sample id → sample, so the Changes view can resolve thumbnails for "added"
  // ids out of the cumulative state already loaded for this commit.
  const sampleById = useMemo(() => {
    const m = new Map<string, Sample>()
    for (const s of samples) m.set(s.id, s)
    return m
  }, [samples])

  // source_id → friendly label, so "source" folders read as names not UUIDs.
  const sourceLabel = useMemo(() => {
    const m = new Map<string, string>()
    for (const s of sources ?? []) {
      const name = typeof s.metadata?.name === 'string' ? s.metadata.name : `${s.type} · ${s.id.slice(0, 8)}`
      m.set(s.id, name)
    }
    return m
  }, [sources])

  // Partition the loaded samples into folders, keeping each sample's global
  // index so the lightbox can navigate the whole commit as one flat list.
  const folders = useMemo(() => {
    const map = new Map<string, number[]>()
    samples.forEach((s, i) => {
      let key: string
      switch (groupBy) {
        case 'source':
          key = sourceLabel.get(s.source_id) ?? `Source ${s.source_id.slice(0, 8)}`
          break
        case 'review':
          key = s.review_status
          break
        case 'annotation':
          key = s.has_annotations ? 'Annotated' : 'Unannotated'
          break
        default:
          key = 'All samples'
      }
      const arr = map.get(key)
      if (arr) arr.push(i)
      else map.set(key, [i])
    })
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [samples, groupBy, sourceLabel])

  function toggleFolder(key: string) {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <section className="min-w-0">
      {/* Header */}
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-text-primary">{commit?.message ?? 'Commit'}</h3>
          <p className="mt-0.5 text-xs text-text-muted">
            <span className="font-mono">{commitId.slice(0, 10)}</span>
            {commit && ` · ${new Date(commit.created_at).toLocaleString()}`}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" variant="secondary" loading={dl.busy} onClick={dl.trigger}>
            {dl.label}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setShowResplit((v) => !v)}>
            Re-split
          </Button>
          <Link to={`/datasets/${datasetId}/commits/${commitId}`}>
            <Button size="sm" variant="secondary">Open detail</Button>
          </Link>
        </div>
      </div>

      {showResplit && (
        <div className="mb-4 rounded-xl border border-border bg-surface-2 p-4 space-y-3">
          <p className="text-sm font-medium text-text-primary">Re-assign splits for this commit</p>
          <div className="space-y-2">
            <label className="flex items-center justify-between text-xs text-text-secondary">
              <span>Train</span><span className="tabular-nums">{Math.round(trainRatio * 100)}%</span>
            </label>
            <input type="range" min={0.1} max={0.9} step={0.05} value={trainRatio}
              onChange={(e) => {
                const t = Number(e.target.value)
                setTrainRatio(t)
                if (valRatio > 1 - t) setValRatio(Math.max(0.05, +(1 - t).toFixed(2)))
              }}
              className="w-full accent-iris" />
            <label className="flex items-center justify-between text-xs text-text-secondary">
              <span>Val</span><span className="tabular-nums">{Math.round(valRatio * 100)}%</span>
            </label>
            <input type="range" min={0.05} max={effectiveValMax} step={0.05} value={Math.min(valRatio, effectiveValMax)}
              onChange={(e) => setValRatio(Number(e.target.value))}
              className="w-full accent-iris" />
            <p className="text-xs text-text-muted">
              train {Math.round(trainRatio * 100)}% · val {Math.round(valRatio * 100)}% · test {testPct}%
            </p>
          </div>
          {(q.hasNextPage || q.isFetchingNextPage) && (
            <p className="text-xs text-text-muted">Loading all samples…</p>
          )}
          <div className="flex gap-2">
            <Button size="sm" onClick={handleResplit}
              loading={resplit.isPending || q.isFetchingNextPage || q.hasNextPage}>
              Apply
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setShowResplit(false)}>Cancel</Button>
          </div>
        </div>
      )}

      {/* View toggle — full dataset state ("Files") vs. diff against parent ("Changes"). */}
      <div className="mb-4 inline-flex rounded-lg border border-border bg-surface-2 p-0.5" role="tablist" aria-label="Commit view">
        {([
          { key: 'files', label: 'Files' },
          { key: 'changes', label: 'Changes' },
        ] as const).map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={view === t.key}
            onClick={() => setView(t.key)}
            className={cn(
              'rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
              view === t.key ? 'bg-iris/15 text-text-primary' : 'text-text-muted hover:text-text-secondary',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {commit?.stats && (
        <div className="mb-4">
          <CommitStats stats={commit.stats} />
        </div>
      )}

      {view === 'files' ? (
        <>
          <div className="mb-3 flex items-center justify-between gap-3">
            <Select
              className="w-auto"
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as GroupKey)}
              aria-label="Group commit samples"
            >
              {GROUP_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
            <span className="text-xs text-text-muted">
              {samples.length > 0 ? `${samples.length} sample${samples.length === 1 ? '' : 's'} at this commit` : ''}
            </span>
          </div>

          {q.isLoading ? (
            <div className="py-12 text-center text-sm text-text-muted">Loading samples…</div>
          ) : samples.length === 0 ? (
            <div className="rounded-xl border border-border bg-surface-2 p-10 text-center shadow-sm">
              <p className="text-sm font-medium text-text-primary">No samples at this commit</p>
            </div>
          ) : (
            <div className="space-y-3">
              {folders.map(([label, indices]) => {
                const open = !collapsed.has(label)
                return (
                  <div key={label} className="overflow-hidden rounded-xl border border-border bg-surface-2">
                    <button
                      type="button"
                      onClick={() => toggleFolder(label)}
                      aria-expanded={open}
                      className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-surface-3"
                    >
                      <Chevron open={open} />
                      <span className="truncate text-sm font-medium text-text-primary">{label}</span>
                      <span className="ml-auto rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-muted">
                        {indices.length}
                      </span>
                    </button>
                    {open && (
                      <div className="grid grid-cols-3 gap-2 px-4 pb-4 sm:grid-cols-5 lg:grid-cols-6">
                        {indices.map((gi) => (
                          <CommitThumb key={samples[gi].id} sample={samples[gi]} onOpen={() => setOpenIndex(gi)} />
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}

              {q.hasNextPage && (
                <Button
                  variant="secondary"
                  className="w-full"
                  loading={q.isFetchingNextPage}
                  onClick={() => q.fetchNextPage()}
                >
                  Load more samples
                </Button>
              )}
            </div>
          )}
        </>
      ) : (
        <ChangesView
          datasetId={datasetId}
          commitId={commitId}
          parentCommitId={commit?.parent_commit_id ?? null}
          sampleById={sampleById}
        />
      )}

      {openIndex !== null && (
        <Lightbox
          samples={samples}
          index={openIndex}
          onClose={() => setOpenIndex(null)}
          onNavigate={setOpenIndex}
        />
      )}
    </section>
  )
}
