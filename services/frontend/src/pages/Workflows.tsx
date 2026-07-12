import { useParams, Link, useNavigate } from 'react-router-dom'
import { useWorkflows, useCreateWorkflow, useDeleteWorkflow } from '../api/workflows'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, SkeletonList } from '../components/ui'

export default function Workflows() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { data: workflows, isLoading, isError, refetch } = useWorkflows(projectId)
  const createWorkflow = useCreateWorkflow()
  const deleteWorkflow = useDeleteWorkflow()

  async function handleNew() {
    if (!projectId) return
    const taken = new Set(workflows?.map(w => w.name) ?? [])
    let name = 'New workflow'
    let i = 2
    while (taken.has(name)) name = `New workflow (${i++})`
    const wf = await createWorkflow.mutateAsync({
      projectId,
      name,
      definition: { steps: [], edges: [] },
    })
    navigate(`/workflows/${wf.id}`)
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <Breadcrumbs
        items={[{ label: 'Project', to: `/projects/${projectId}` }, { label: 'Workflows' }]}
      />

      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-bold text-text-primary">Workflows</h2>
        <Button onClick={handleNew} loading={createWorkflow.isPending}>
          + New Workflow
        </Button>
      </div>

      {isLoading && <SkeletonList rows={3} />}

      {isError && (
        <ErrorState description="Could not load workflows for this project." onRetry={() => refetch()} />
      )}

      {workflows && workflows.length === 0 && (
        <EmptyState
          title="No workflows yet"
          description="Create a workflow to define your ingest pipeline."
          action={
            <Button onClick={handleNew} loading={createWorkflow.isPending}>
              + New Workflow
            </Button>
          }
        />
      )}

      {workflows && workflows.length > 0 && (
        <div className="space-y-2">
          {workflows.map((wf) => (
            <Card key={wf.id} className="flex items-center justify-between px-5 py-4">
              <Link to={`/workflows/${wf.id}`} className="flex-1 transition-colors hover:text-iris">
                <p className="font-semibold text-text-primary">{wf.name}</p>
                <p className="mt-0.5 text-xs text-text-muted">
                  v{wf.version} · {new Date(wf.created_at).toLocaleDateString()}
                </p>
              </Link>
              <div className="flex items-center gap-2">
                <Link to={`/workflows/${wf.id}`}>
                  <Button variant="secondary" size="sm">Edit</Button>
                </Link>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-error hover:text-error"
                  loading={deleteWorkflow.isPending && deleteWorkflow.variables === wf.id}
                  onClick={() => deleteWorkflow.mutate(wf.id)}
                >
                  Delete
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
