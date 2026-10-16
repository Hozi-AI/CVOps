import { useParams } from 'react-router-dom'
import { useRun, useRunSSE, useCancelRun, useRetryRun } from '../api/runs'
import { usePinProject } from '../lib/useActiveProject'
import { StepRunCard } from '../components/runs/StepRunCard'
import { GateResolutionBanner } from '../components/runs/GateResolutionBanner'
import type { RunStep } from '../components/runs/types'
import { BackButton, Breadcrumbs, Button, Card, EmptyState, SkeletonList, StatusPill } from '../components/ui'

function stepLabel(typeKey: string) {
  return typeKey.replace('step.', '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

// worker-cvat bakes the CVAT link as CVAT_PUBLIC_URL (localhost:8080 by default —
// it has no request context). Opened from a remote dev VM, localhost won't reach
// the VM, so rewrite a localhost host to whatever host the browser actually used
// (mirroring how the API derives presign hosts per-request). A configured,
// non-localhost CVAT host is left untouched.
function browserReachableCvatUrl(stored: string | null | undefined): string | null {
  if (!stored) return null
  try {
    const u = new URL(stored)
    if (u.hostname === 'localhost' || u.hostname === '127.0.0.1') {
      u.hostname = window.location.hostname
    }
    return u.toString()
  } catch {
    return stored
  }
}

export default function RunView() {
  const { id } = useParams<{ id: string }>()
  const { data: detail, isLoading } = useRun(id)
  useRunSSE(id)
  usePinProject(detail?.run?.project_id)

  const cancelRun = useCancelRun()
  const retryRun = useRetryRun()

  if (isLoading) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <SkeletonList rows={3} />
      </div>
    )
  }

  const run = detail?.run
  const steps = detail?.steps ?? []
  const waitingStep = steps.find((s) => s.status === 'waiting')

  const stepCards: RunStep[] = steps.map((s) => {
    const outputs = asRecord(s.output_refs)
    return {
      id: s.id,
      type_key: s.kind === 'step' ? (s.step_id ?? s.kind) : s.kind,
      label: stepLabel(s.step_id ?? s.kind ?? ''),
      status: s.status,
      started_at: s.started_at,
      finished_at: s.finished_at,
      duration:
        s.started_at && s.finished_at
          ? `${Math.round((new Date(s.finished_at).getTime() - new Date(s.started_at).getTime()) / 1000)}s`
          : null,
      config: asRecord(s.config),
      inputs: asRecord(s.input_refs),
      outputs,
      metrics: asRecord(s.metrics),
      error: s.error,
      // The coordinator nests gate data under output_refs.gate_data; fall back
      // to a top-level cvat_url for any older shape.
      cvat_url:
        browserReachableCvatUrl(
          ((outputs?.gate_data as Record<string, unknown> | undefined)?.cvat_url as string) ??
            (outputs?.cvat_url as string),
        ) ?? undefined,
    }
  })

  const isActive = run?.status === 'pending' || run?.status === 'running'
  const isRetryable = run?.status === 'failed' || run?.status === 'cancelled' || run?.status === 'canceled'

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-6 flex items-center gap-2">
        <BackButton
          to={run ? `/projects/${run.project_id}/runs` : '/projects'}
          label={run ? 'Runs' : 'Projects'}
        />
        <Breadcrumbs
          className="mb-0"
          items={[{ label: 'Projects', to: '/projects' }, { label: `Run ${id?.slice(0, 8)}`, mono: true }]}
        />
      </div>

      {run && (
        <Card className="mb-4 flex items-center justify-between px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <StatusPill status={run.status} />
              <span className="text-xs text-text-muted">Attempt {run.attempt}</span>
            </div>
            {run.error && <p className="mt-1 text-xs text-error">{run.error}</p>}
          </div>
          <div className="flex gap-2">
            {isActive && (
              <Button variant="secondary" size="sm" loading={cancelRun.isPending} onClick={() => id && cancelRun.mutate(id)}>
                Cancel
              </Button>
            )}
            {isRetryable && (
              <Button size="sm" loading={retryRun.isPending} onClick={() => id && retryRun.mutate(id)}>
                Retry
              </Button>
            )}
          </div>
        </Card>
      )}

      {waitingStep && id && (
        <GateResolutionBanner
          runId={id}
          stepId={waitingStep.step_id ?? waitingStep.id}
          cvatUrl={browserReachableCvatUrl(
            ((asRecord(waitingStep.output_refs)?.gate_data as Record<string, unknown> | undefined)?.cvat_url
              ?? asRecord(waitingStep.output_refs)?.cvat_url) as string,
          )}
        />
      )}

      {stepCards.length === 0 ? (
        <EmptyState
          title={run?.status === 'pending' ? 'Starting…' : 'No steps yet'}
          description={
            run?.status === 'pending'
              ? 'The workflow is being scheduled. Steps appear here as they are created.'
              : 'This run has no step records.'
          }
        />
      ) : (
        <div className="space-y-3">
          {stepCards.map((step, i) => (
            <StepRunCard key={step.id} step={step} defaultOpen={i === 0} />
          ))}
        </div>
      )}
    </div>
  )
}
