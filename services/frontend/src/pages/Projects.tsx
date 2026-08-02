import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useProjects, useCreateProject } from '../api/projects'
import { Button, Card, Dialog, EmptyState, ErrorState, Field, Input, Select, SkeletonList } from '../components/ui'

export default function Projects() {
  const { data: projects, isLoading, isError, refetch } = useProjects()
  const createProject = useCreateProject()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [taskType, setTaskType] = useState('detection')
  const [modality, setModality] = useState('image')

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    await createProject.mutateAsync({ name, task_type: taskType, modality })
    setName('')
    setModality('image')
    setShowForm(false)
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-text-primary">Projects</h2>
          <p className="mt-0.5 text-sm text-text-muted">Each project is one ML problem domain</p>
        </div>
        <Button onClick={() => setShowForm(true)}>+ New Project</Button>
      </div>

      <Dialog open={showForm} onClose={() => setShowForm(false)} title="New project">
        <form onSubmit={handleCreate} className="space-y-4">
          <Field label="Name" htmlFor="project-name">
            <Input
              id="project-name"
              required
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My project"
            />
          </Field>
          <Field label="Task type" htmlFor="project-task">
            <Select id="project-task" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
              <option value="detection">Detection</option>
              <option value="segmentation">Segmentation</option>
              <option value="classification">Classification</option>
            </Select>
          </Field>
          <Field label="Modality" htmlFor="project-modality">
            <Select id="project-modality" value={modality} onChange={(e) => setModality(e.target.value)}>
              <option value="image">Image / Video</option>
              <option value="text">Text / NLP</option>
              <option value="sensor">Sensor / Time-Series</option>
            </Select>
          </Field>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={() => setShowForm(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={createProject.isPending}>
              Create
            </Button>
          </div>
        </form>
      </Dialog>

      {isLoading && <SkeletonList rows={3} />}

      {isError && <ErrorState description="Could not load your projects." onRetry={() => refetch()} />}

      {projects && projects.length === 0 && (
        <EmptyState
          title="No projects yet"
          description="Create your first project to get started."
          action={<Button onClick={() => setShowForm(true)}>+ New Project</Button>}
        />
      )}

      {projects && projects.length > 0 && (
        <div className="grid gap-3">
          {projects.map((p) => (
            <Link key={p.id} to={`/projects/${p.id}`}>
              <Card className="flex items-center justify-between px-5 py-4 transition-all hover:border-iris hover:shadow-md">
                <div>
                  <p className="font-semibold text-text-primary">{p.name}</p>
                  <p className="mt-0.5 text-xs capitalize text-text-muted">{p.modality} · {p.task_type}</p>
                </div>
                <span className="text-lg text-text-muted">›</span>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
