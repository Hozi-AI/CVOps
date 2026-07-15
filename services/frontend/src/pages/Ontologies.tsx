import { useRef, useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useOntologies,
  useCreateOntology,
  useUpdateOntology,
  useDeleteOntology,
  useLabelClasses,
  useCreateLabelClass,
  useUpdateLabelClass,
  useDeleteLabelClass,
  type Ontology,
  type LabelClass,
} from '../api/ontologies'
import { client } from '../lib/client'
import { toast } from '../store/toast'
import {
  Breadcrumbs,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  SkeletonList,
  ErrorState,
} from '../components/ui'

const LABEL_COLORS = [
  '#e6194b','#3cb44b','#4363d8','#f58231','#911eb4',
  '#42d4f4','#f032e6','#bfef45','#fabed4','#469990',
  '#dcbeff','#9a6324','#fffac8','#800000','#aaffc3',
  '#808000','#ffd8b1','#000075','#a9a9a9','#ffffff',
]


function LabelClassRow({ lc, ontologyId }: { lc: LabelClass; ontologyId: string }) {
  const [editing, setEditing] = useState(false)
  const [displayName, setDisplayName] = useState(lc.display_name)
  const [color, setColor] = useState(lc.color)
  const updateClass = useUpdateLabelClass(ontologyId)
  const deleteClass = useDeleteLabelClass(ontologyId)

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    await updateClass.mutateAsync({ classId: lc.id, body: { display_name: displayName, color } })
    setEditing(false)
  }

  return (
    <li className="flex items-center gap-3 rounded-lg border border-border px-3 py-2">
      {editing ? (
        <form onSubmit={handleSave} className="flex flex-1 items-center gap-2">
          <input
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            className="h-8 w-10 cursor-pointer rounded border border-border-strong"
          />
          <Input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="flex-1 text-sm"
          />
          <span className="text-xs text-text-muted font-mono">{lc.class_key}</span>
          <Button size="sm" type="submit" loading={updateClass.isPending}>Save</Button>
          <Button size="sm" variant="secondary" type="button" onClick={() => setEditing(false)}>Cancel</Button>
        </form>
      ) : (
        <>
          <span
            className="h-4 w-4 flex-shrink-0 rounded-full border border-border-strong"
            style={{ backgroundColor: lc.color }}
            aria-hidden
          />
          <span className="text-sm text-text-primary">{lc.display_name}</span>
          <span className="text-xs text-text-muted font-mono">{lc.class_key}</span>
          <span className="text-xs text-text-muted">#{lc.sort_order}</span>
          <div className="ml-auto flex gap-1">
            <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Edit</Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-error hover:text-error"
              onClick={() => deleteClass.mutate(lc.id)}
              loading={deleteClass.isPending}
            >
              Delete
            </Button>
          </div>
        </>
      )}
    </li>
  )
}

function AddClassForm({ ontologyId, nextSortOrder }: { ontologyId: string; nextSortOrder: number }) {
  const [key, setKey] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [color, setColor] = useState('#7B6CF6')
  const createClass = useCreateLabelClass(ontologyId)

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    if (!key.trim()) return
    await createClass.mutateAsync({
      class_key: key.trim(),
      display_name: displayName.trim() || key.trim(),
      color,
      sort_order: nextSortOrder,
    })
    setKey('')
    setDisplayName('')
    setColor('#7B6CF6')
  }

  return (
    <form onSubmit={handleAdd} className="flex items-end gap-2 pt-2">
      <Field label="Class key" className="flex-1">
        <Input required value={key} onChange={(e) => setKey(e.target.value)} placeholder="e.g. car" />
      </Field>
      <Field label="Display name" className="flex-1">
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="e.g. Car" />
      </Field>
      <Field label="Color">
        <input
          type="color"
          value={color}
          onChange={(e) => setColor(e.target.value)}
          className="h-10 w-12 cursor-pointer rounded-lg border border-border-strong"
        />
      </Field>
      <Button type="submit" size="sm" loading={createClass.isPending} disabled={!key.trim()}>Add</Button>
    </form>
  )
}

function OntologyCard({ ont }: { ont: Ontology }) {
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(ont.name)
  const updateOntology = useUpdateOntology(ont.id)
  const deleteOntology = useDeleteOntology()
  const { data: classes, isLoading } = useLabelClasses(ont.id)

  const nextSortOrder =
    classes && classes.length > 0 ? Math.max(...classes.map((c) => c.sort_order)) + 1 : 0

  async function handleRename(e: FormEvent) {
    e.preventDefault()
    if (!newName.trim() || newName === ont.name) {
      setRenaming(false)
      return
    }
    await updateOntology.mutateAsync({ name: newName.trim() })
    setRenaming(false)
  }

  function handleDelete() {
    if (!window.confirm(`Delete label set "${ont.name}"? This cannot be undone.`)) return
    deleteOntology.mutate(ont.id)
  }

  return (
    <Card className="p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          {renaming ? (
            <form onSubmit={handleRename} className="flex items-center gap-2">
              <Input
                autoFocus
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="text-base font-semibold"
              />
              <Button size="sm" type="submit" loading={updateOntology.isPending}>Save</Button>
              <Button size="sm" variant="secondary" type="button" onClick={() => { setRenaming(false); setNewName(ont.name) }}>Cancel</Button>
            </form>
          ) : (
            <div className="flex items-center gap-2">
              <h3 className="text-base font-semibold text-text-primary">{ont.name}</h3>
              <span className="text-xs text-text-muted bg-surface-3 rounded px-1.5 py-0.5">v{ont.version}</span>
            </div>
          )}
        </div>
        {!renaming && (
          <div className="flex gap-1 flex-shrink-0">
            <Button size="sm" variant="ghost" onClick={() => setRenaming(true)}>Rename</Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-error hover:text-error"
              onClick={handleDelete}
              loading={deleteOntology.isPending}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      {isLoading ? (
        <SkeletonList rows={2} />
      ) : classes && classes.length > 0 ? (
        <ul className="space-y-1.5">
          {classes.map((lc) => (
            <LabelClassRow key={lc.id} lc={lc} ontologyId={ont.id} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-text-muted">No classes yet — add the first one below.</p>
      )}

      <AddClassForm ontologyId={ont.id} nextSortOrder={nextSortOrder} />
    </Card>
  )
}

function CreateOntologyDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [name, setName] = useState('')
  const createOntology = useCreateOntology()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    await createOntology.mutateAsync({ name: name.trim() })
    setName('')
    onClose()
  }

  function handleClose() {
    setName('')
    onClose()
  }

  return (
    <Dialog open={open} onClose={handleClose} title="New label set">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Field label="Name">
          <Input
            autoFocus
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. detections-v1"
          />
        </Field>
        {createOntology.isError && (
          <p className="text-sm text-error">Name already exists — choose a different one.</p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" type="button" size="sm" onClick={handleClose}>Cancel</Button>
          <Button type="submit" size="sm" loading={createOntology.isPending} disabled={!name.trim()}>
            Create
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

export default function Ontologies() {
  const { data: ontologies, isLoading, isError, refetch } = useOntologies()
  const [createOpen, setCreateOpen] = useState(false)
  const [importing, setImporting] = useState(false)
  const importRef = useRef<HTMLInputElement>(null)
  const qc = useQueryClient()

  async function handleImportTxt(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setImporting(true)
    try {
      const text = await file.text()
      const labels = text.split('\n').map(l => l.trim()).filter(Boolean)
      if (!labels.length) { toast.error('Empty file', 'No class names found'); return }
      const name = file.name.replace(/\.[^.]+$/, '') || 'Imported labels'
      const { data: ont } = await client.post<Ontology>('/ontologies', { name })
      await Promise.all(
        labels.map((label, i) =>
          client.post(`/ontologies/${ont.id}/classes`, {
            class_key: label, display_name: label,
            color: LABEL_COLORS[i % LABEL_COLORS.length], sort_order: i,
          })
        )
      )
      await qc.invalidateQueries({ queryKey: ['ontologies'] })
      toast.success('Label set imported', `"${ont.name}" created with ${labels.length} classes`)
    } catch (err) {
      toast.error('Import failed', err instanceof Error ? err.message : String(err))
    } finally {
      setImporting(false)
      e.target.value = ''
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <Breadcrumbs items={[{ label: 'Label Sets' }]} />

      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-text-primary">Label Sets</h2>
          <p className="mt-0.5 text-sm text-text-muted">
            Org-wide label vocabularies — use one across any number of projects
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" loading={importing} onClick={() => importRef.current?.click()}>
            Import .txt
          </Button>
          <input ref={importRef} type="file" accept=".txt,text/plain" className="hidden" onChange={handleImportTxt} />
          <Button onClick={() => setCreateOpen(true)}>+ New Label Set</Button>
        </div>
      </div>

      {isLoading && <SkeletonList rows={3} />}
      {isError && <ErrorState onRetry={refetch} />}
      {!isLoading && !isError && ontologies?.length === 0 && (
        <EmptyState
          title="No label sets yet"
          description="Create your first label set to define what reviewers can annotate in CVAT."
          action={<Button onClick={() => setCreateOpen(true)}>+ New Label Set</Button>}
        />
      )}
      {ontologies && ontologies.length > 0 && (
        <div className="space-y-4">
          {ontologies.map((ont) => (
            <OntologyCard key={ont.id} ont={ont} />
          ))}
        </div>
      )}

      <CreateOntologyDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  )
}
