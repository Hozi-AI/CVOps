import { useEffect, useState } from 'react'
import type { InfiniteData } from '@tanstack/react-query'
import type { CursorPage, Sample } from '../../api/samples'
import { useImageUrl, useThumbnailUrl } from '../../api/samples'
import { useSelectionStore } from '../../store/selection'
import { useAnnotations } from '../../api/annotations'
import { cn } from '../../lib/cn'
import { LoadingState } from '../ui'
import { BoxOverlay } from './BoxOverlay'
import { SampleViewerText } from '../SampleViewerText'
import { SampleViewerSensor } from '../SampleViewerSensor'

// Review-status dot color, by semantic token. `bg-text-muted` is the default
// for unreviewed / unknown statuses.
const REVIEW_DOT: Record<string, string> = {
  accepted: 'bg-success',
  rejected: 'bg-error',
  unreviewed: 'bg-text-muted',
}

function CheckGlyph() {
  return (
    <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m5 13 4 4L19 7" />
    </svg>
  )
}

function ThumbnailCard({
  sample,
  selecting,
  selected,
  onOpen,
  onSelect,
}: {
  sample: Sample
  selecting: boolean
  selected: boolean
  onOpen: () => void
  onSelect: (shiftKey: boolean) => void
}) {
  const { data } = useThumbnailUrl(sample.id)

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={
        selecting
          ? `${selected ? 'Deselect' : 'Select'} frame ${sample.frame_index ?? ''}`
          : `Open frame ${sample.frame_index ?? ''}`
      }
      aria-pressed={selecting ? selected : undefined}
      onClick={(e) => (selecting ? onSelect(e.shiftKey) : onOpen())}
      // Right-click previews the image — even mid-selection, so a selected tile
      // can still be inspected without leaving select mode.
      onContextMenu={(e) => {
        e.preventDefault()
        onOpen()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          if (selecting) onSelect(e.shiftKey)
          else onOpen()
        }
      }}
      className={cn(
        'group relative aspect-square cursor-pointer select-none overflow-hidden rounded-lg border bg-surface-3 transition-shadow focus:outline-none focus:ring-2 focus:ring-focus',
        selected ? 'border-iris ring-2 ring-iris' : 'border-border hover:border-border-strong',
      )}
    >
      {sample.modality !== 'image' ? (
        <div className="flex h-full w-full flex-col items-center justify-center gap-1 text-text-muted">
          <span className="text-2xl">{sample.modality === 'text' ? '📄' : '📈'}</span>
          <span className="text-[10px] capitalize">{sample.modality}</span>
        </div>
      ) : data?.url ? (
        <img
          src={data.url}
          alt={`frame ${sample.frame_index ?? ''}`}
          className={cn('h-full w-full object-cover transition-opacity', selecting && !selected && 'opacity-90')}
          loading="lazy"
          draggable={false}
        />
      ) : (
        <div className="flex h-full w-full items-center justify-center">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-border-strong border-t-text-secondary" />
        </div>
      )}

      {/* Selection check — only in select mode. Purely visual; the whole tile toggles. */}
      {selecting && (
        <span
          className={cn(
            'pointer-events-none absolute left-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-full border transition-colors',
            selected
              ? 'border-iris bg-iris text-text-onAccent'
              : 'border-text-onAccent/70 bg-black/40 text-transparent group-hover:text-text-onAccent/70',
          )}
        >
          <CheckGlyph />
        </span>
      )}

      {/* Tag + review-status dots */}
      <div className="pointer-events-none absolute right-1.5 top-1.5 flex items-center gap-1">
        {sample.tags.slice(0, 3).map((t) => (
          <span
            key={t.id}
            className="h-2 w-2 rounded-full ring-1 ring-black/30"
            style={{ backgroundColor: t.color }}
            title={t.name}
          />
        ))}
        <span
          className={cn(
            'h-2 w-2 rounded-full ring-1 ring-black/30',
            REVIEW_DOT[sample.review_status] ?? 'bg-text-muted',
          )}
          title={sample.review_status}
        />
      </div>

      <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end bg-gradient-to-t from-black/60 to-transparent p-2 opacity-0 transition-opacity group-hover:opacity-100">
        <p className="text-xs text-text-onAccent">
          {sample.width != null && sample.height != null ? `${sample.width}×${sample.height}` : sample.modality}
          {sample.frame_index != null && ` · f${sample.frame_index}`}
        </p>
      </div>
    </div>
  )
}

export function Lightbox({
  samples,
  index,
  onClose,
  onNavigate,
}: {
  samples: Sample[]
  index: number
  onClose: () => void
  onNavigate: (next: number) => void
}) {
  const sample = samples[index]
  const { data, isLoading } = useImageUrl(sample?.id)
  const { data: revisions } = useAnnotations(sample?.id)
  const [selectedRevId, setSelectedRevId] = useState<string | null>(null)
  const [showBoxes, setShowBoxes] = useState(true)

  // Default to the highest revision_no (backend orders ascending, so last);
  // reset whenever the sample or its revision set changes.
  useEffect(() => {
    if (revisions && revisions.length > 0) {
      setSelectedRevId(revisions[revisions.length - 1].id)
    } else {
      setSelectedRevId(null)
    }
  }, [sample?.id, revisions])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowRight' && index < samples.length - 1) onNavigate(index + 1)
      if (e.key === 'ArrowLeft' && index > 0) onNavigate(index - 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, samples.length, onClose, onNavigate])

  if (!sample) return null

  const selectedRev = revisions?.find(r => r.id === selectedRevId) ?? null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6" onClick={onClose}>
      <button
        onClick={onClose}
        className="absolute right-5 top-4 text-2xl leading-none text-text-onAccent/70 hover:text-text-onAccent"
        aria-label="Close"
      >
        ×
      </button>

      {index > 0 && (
        <button
          onClick={(e) => {
            e.stopPropagation()
            onNavigate(index - 1)
          }}
          className="absolute left-4 px-2 text-4xl text-text-onAccent/60 hover:text-text-onAccent"
          aria-label="Previous"
        >
          ‹
        </button>
      )}

      <div className="flex max-h-[85vh] max-w-5xl flex-col items-center" onClick={(e) => e.stopPropagation()}>
        {isLoading || !data?.url ? (
          <div className="h-10 w-10 animate-spin rounded-full border-2 border-text-onAccent/30 border-t-text-onAccent" />
        ) : sample.modality === 'text' ? (
          <div className="max-h-[70vh] w-full max-w-2xl overflow-auto">
            <SampleViewerText blobUrl={data.url} />
          </div>
        ) : sample.modality === 'sensor' ? (
          <div className="max-h-[70vh] w-full max-w-3xl overflow-auto">
            <SampleViewerSensor blobUrl={data.url} />
          </div>
        ) : (
          <div className="relative inline-block">
            <img src={data.url} alt={`frame ${sample.frame_index ?? ''}`} className="max-h-[80vh] max-w-full object-contain rounded-lg" />
            {showBoxes && selectedRev && <BoxOverlay boxes={selectedRev.payload} />}
          </div>
        )}

        {revisions && revisions.length > 0 && (
          <div className="flex items-center gap-3 mt-3">
            <select
              value={selectedRevId ?? ''}
              onChange={e => setSelectedRevId(e.target.value)}
              className="bg-surface-3 text-text-primary text-xs rounded-md px-2 py-1 border border-border-strong focus:outline-none"
            >
              {revisions.map(r => (
                <option key={r.id} value={r.id} className="bg-surface-3 text-text-primary">
                  rev {r.revision_no} · {String(r.provenance?.source ?? 'unknown')}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 text-text-onAccent/70 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={showBoxes}
                onChange={e => setShowBoxes(e.target.checked)}
              />
              Boxes
            </label>
          </div>
        )}

        <p className="text-text-onAccent/70 text-xs mt-3">
          {sample.width != null && sample.height != null ? `${sample.width}×${sample.height}` : sample.modality}
          {sample.frame_index != null && ` · frame ${sample.frame_index}`}
          {` · ${index + 1} / ${samples.length}`}
        </p>
      </div>

      {index < samples.length - 1 && (
        <button
          onClick={(e) => {
            e.stopPropagation()
            onNavigate(index + 1)
          }}
          className="absolute right-4 px-2 text-4xl text-text-onAccent/60 hover:text-text-onAccent"
          aria-label="Next"
        >
          ›
        </button>
      )}
    </div>
  )
}

interface Props {
  data: InfiniteData<CursorPage<Sample>> | undefined
  isLoading: boolean
  hasNextPage: boolean
  isFetchingNextPage: boolean
  fetchNextPage: () => void
  selectable?: boolean
}

export function SampleGrid({
  data,
  isLoading,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  selectable = false,
}: Props) {
  const samples = data?.pages.flatMap((p: CursorPage<Sample>) => p.items) ?? []
  const [openIndex, setOpenIndex] = useState<number | null>(null)
  const selected = useSelectionStore((s) => s.selected)
  const selectMode = useSelectionStore((s) => s.selectMode)
  const toggle = useSelectionStore((s) => s.toggle)
  const add = useSelectionStore((s) => s.add)
  const clear = useSelectionStore((s) => s.clear)
  const lastClicked = useSelectionStore((s) => s.lastClicked)
  const setLastClicked = useSelectionStore((s) => s.setLastClicked)

  const selecting = selectable && selectMode

  if (isLoading) {
    return <LoadingState />
  }

  if (samples.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-surface-2 p-10 text-center shadow-sm">
        <p className="text-sm font-medium text-text-primary">No samples yet</p>
        <p className="mt-1 text-xs text-text-muted">Samples are extracted from uploaded videos</p>
      </div>
    )
  }

  const pageIds = samples.map((s) => s.id)
  const allSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))

  // Plain click toggles a tile; shift-click selects the inclusive range from the
  // last-acted tile to this one (using the loaded order as the anchor).
  function handleSelect(index: number, shiftKey: boolean) {
    const id = samples[index].id
    if (shiftKey && lastClicked) {
      const from = samples.findIndex((s) => s.id === lastClicked)
      if (from !== -1) {
        const [a, b] = from < index ? [from, index] : [index, from]
        add(samples.slice(a, b + 1).map((s) => s.id))
        setLastClicked(id)
        return
      }
    }
    toggle(id)
    setLastClicked(id)
  }

  return (
    <div>
      {selecting && (
        <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-text-muted">
          <button
            type="button"
            onClick={() => (allSelected ? clear() : add(pageIds))}
            className="underline hover:text-text-secondary"
          >
            {allSelected ? 'Deselect all' : 'Select all on page'}
          </button>
          <span>{selected.size} selected</span>
          <span className="text-text-muted/70">
            click to select · shift-click for a range · right-click to preview
          </span>
        </div>
      )}

      <div className="grid grid-cols-4 gap-2 sm:grid-cols-6 lg:grid-cols-8">
        {samples.map((s, i) => (
          <ThumbnailCard
            key={s.id}
            sample={s}
            selecting={selecting}
            selected={selected.has(s.id)}
            onOpen={() => setOpenIndex(i)}
            onSelect={(shiftKey) => handleSelect(i, shiftKey)}
          />
        ))}
      </div>

      {hasNextPage && (
        <button
          onClick={() => fetchNextPage()}
          disabled={isFetchingNextPage}
          className="mt-4 w-full rounded-lg border border-border-strong py-2 text-sm text-text-secondary transition-colors hover:bg-surface-3 disabled:opacity-60"
        >
          {isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      )}

      {openIndex !== null && (
        <Lightbox
          samples={samples}
          index={openIndex}
          onClose={() => setOpenIndex(null)}
          onNavigate={setOpenIndex}
        />
      )}
    </div>
  )
}
