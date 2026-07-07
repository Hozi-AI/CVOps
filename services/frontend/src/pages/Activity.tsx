import { useRef, useEffect, useState } from 'react'
import { useEvents } from '../api/events'
import type { ActivityEventOut } from '../api/events'
import { EmptyState, Button } from '../components/ui'

const ENTITY_FILTERS: { label: string; value?: string }[] = [
  { label: 'All', value: undefined },
  { label: 'Run', value: 'run' },
  { label: 'Commit', value: 'commit' },
  { label: 'Data Source', value: 'data_source' },
  { label: 'Sample', value: 'sample' },
  { label: 'Annotation', value: 'annotation_revision' },
]

const ENTITY_COLOR: Record<string, string> = {
  run: 'text-iris-400',
  commit: 'text-lime-400',
  data_source: 'text-amber-400',
  sample: 'text-sky-400',
  annotation_revision: 'text-pink-400',
}

const ENTITY_ICON: Record<string, string> = {
  run: '▶',
  commit: '◈',
  data_source: '⬆',
  sample: '⬡',
  annotation_revision: '✎',
}

const EVENT_LABELS: Record<string, string> = {
  'run/run.started': 'Run started',
  'run/run.succeeded': 'Run succeeded',
  'run/run.failed': 'Run failed',
  'run/run.waiting': 'Run waiting at gate',
  'commit/created': 'Dataset committed',
  'commit/branch.advanced': 'Branch advanced',
  'data_source/created': 'Data source uploaded',
  'data_source/images.uploaded': 'Images uploaded',
  'data_source/images.uploaded_annotated': 'Annotated images imported',
  'annotation_revision/created': 'Annotations saved',
  'sample/sample.updated': 'Sample updated',
  'sample/sample.deleted': 'Sample deleted',
}

function describeEvent(ev: ActivityEventOut): string {
  return EVENT_LABELS[`${ev.entity_type}/${ev.action}`] ?? `${ev.entity_type} ${ev.action}`
}

function actorLabel(ev: ActivityEventOut): string {
  if (ev.actor_email) return ev.actor_email.split('@')[0]
  return ev.actor_type ?? 'system'
}

function relativeTime(isoString: string): string {
  const s = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function EventRow({ ev }: { ev: ActivityEventOut }) {
  const color = ENTITY_COLOR[ev.entity_type] ?? 'text-text-muted'
  const icon = ENTITY_ICON[ev.entity_type] ?? '·'
  return (
    <div className="flex items-start gap-3 px-4 py-3 border-b border-border last:border-0">
      <span className={`mt-0.5 text-base w-5 flex-shrink-0 ${color}`}>{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="text-sm text-text-primary">{describeEvent(ev)}</p>
        <p className="text-xs text-text-muted mt-0.5">{actorLabel(ev)}</p>
      </div>
      <span className="text-xs text-text-muted flex-shrink-0 mt-0.5" title={ev.created_at}>
        {relativeTime(ev.created_at)}
      </span>
    </div>
  )
}

function RowSkeleton() {
  return (
    <div>
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex gap-3 px-4 py-3 border-b border-border">
          <div className="w-5 h-4 bg-surface-3 rounded animate-pulse flex-shrink-0 mt-0.5" />
          <div className="flex-1 space-y-1.5">
            <div className="h-3 bg-surface-3 rounded w-2/3 animate-pulse" />
            <div className="h-2.5 bg-surface-3 rounded w-1/3 animate-pulse" />
          </div>
          <div className="w-10 h-3 bg-surface-3 rounded animate-pulse flex-shrink-0 mt-0.5" />
        </div>
      ))}
    </div>
  )
}

export default function Activity() {
  const [entityType, setEntityType] = useState<string | undefined>(undefined)
  const [action, setAction] = useState('')
  const sentinelRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, hasNextPage, isFetchingNextPage, fetchNextPage } = useEvents({
    entity_type: entityType,
    action: action.trim() || undefined,
  })

  const events = data?.pages.flatMap(p => p.items) ?? []

  useEffect(() => {
    if (!sentinelRef.current) return
    const obs = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting && hasNextPage) fetchNextPage() },
      { threshold: 0.1 },
    )
    obs.observe(sentinelRef.current)
    return () => obs.disconnect()
  }, [hasNextPage, fetchNextPage])

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <h2 className="text-xl font-bold text-text-primary mb-4">Activity</h2>

      <div className="flex flex-wrap items-center gap-1.5 mb-4">
        {ENTITY_FILTERS.map(f => (
          <Button
            key={f.label}
            size="sm"
            variant={entityType === f.value ? 'primary' : 'secondary'}
            onClick={() => setEntityType(f.value)}
          >
            {f.label}
          </Button>
        ))}
        <input
          type="text"
          placeholder="Filter by action…"
          value={action}
          onChange={e => setAction(e.target.value)}
          className="ml-2 px-3 py-1 text-sm rounded-lg border border-border bg-surface-2 text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-focus"
        />
      </div>

      <div className="bg-surface-2 rounded-xl border border-border overflow-hidden">
        {isLoading && <RowSkeleton />}

        {!isLoading && events.length === 0 && (
          <div className="p-8">
            <EmptyState
              title="No activity yet"
              description="Events will appear here as the system runs"
            />
          </div>
        )}

        {events.map(ev => <EventRow key={ev.id} ev={ev} />)}

        {isFetchingNextPage && <RowSkeleton />}

        <div ref={sentinelRef} className="h-1" />
      </div>
    </div>
  )
}
