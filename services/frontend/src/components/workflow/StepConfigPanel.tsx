import { useState } from 'react'
import { STEP_META, type FieldSpec, type ResolvedInput } from '../../lib/stepMeta'
import { Field, Input, Select, Textarea } from '../ui'
import { useOntologies } from '../../api/ontologies'

function setOrDrop(
  config: Record<string, unknown>,
  key: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...config }
  const empty =
    value === undefined ||
    value === '' ||
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === 'object' && value !== null && Object.keys(value).length === 0)
  if (empty) delete next[key]
  else next[key] = value
  return next
}

function TagsEditor({ value, placeholder, onChange }: { value: string[]; placeholder?: string; onChange: (v: string[]) => void }) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const t = draft.trim()
    if (t && !value.includes(t)) onChange([...value, t])
    setDraft('')
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {value.map((t) => (
        <span key={t} className="inline-flex items-center gap-1 rounded-full bg-surface-3 px-2 py-0.5 text-xs text-text-primary">
          {t}
          <button type="button" aria-label={`Remove ${t}`} className="text-text-muted hover:text-error" onClick={() => onChange(value.filter((x) => x !== t))}>
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            add()
          }
        }}
        onBlur={add}
        className="min-w-[8rem] flex-1 rounded-lg border border-border-strong bg-surface-2 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-focus"
      />
    </div>
  )
}

type KV = { k: string; v: string }

function KeyValueEditor({ value, onChange }: { value: Record<string, unknown>; onChange: (v: Record<string, string>) => void }) {
  const [rows, setRows] = useState<KV[]>(() => {
    const entries = Object.entries(value).map(([k, v]) => ({ k, v: String(v) }))
    return entries.length > 0 ? entries : [{ k: '', v: '' }]
  })

  const commit = (next: KV[]) => {
    setRows(next)
    const obj: Record<string, string> = {}
    for (const r of next) if (r.k.trim()) obj[r.k.trim()] = r.v
    onChange(obj)
  }

  return (
    <div className="space-y-2">
      {rows.map((row, i) => (
        <div key={i} className="flex gap-2">
          <input
            value={row.k}
            placeholder="key"
            onChange={(e) => commit(rows.map((r, idx) => (idx === i ? { ...r, k: e.target.value } : r)))}
            className="flex-1 rounded-lg border border-border-strong bg-surface-2 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-focus"
          />
          <input
            value={row.v}
            placeholder="value"
            onChange={(e) => commit(rows.map((r, idx) => (idx === i ? { ...r, v: e.target.value } : r)))}
            className="flex-1 rounded-lg border border-border-strong bg-surface-2 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-focus"
          />
        </div>
      ))}
      <button type="button" onClick={() => commit([...rows, { k: '', v: '' }])} className="text-xs text-iris-400 hover:text-iris">
        + Add parameter
      </button>
    </div>
  )
}

function FieldRow({
  spec,
  config,
  onConfigChange,
}: {
  spec: FieldSpec
  config: Record<string, unknown>
  onConfigChange: (c: Record<string, unknown>) => void
}) {
  const id = `cfg-${spec.key}`
  const raw = config[spec.key]
  const set = (v: unknown) => onConfigChange(setOrDrop(config, spec.key, v))
  const { data: ontologies } = useOntologies()

  let control
  switch (spec.widget) {
    case 'number':
      control = (
        <Input
          id={id}
          type="number"
          min={spec.min}
          max={spec.max}
          step={spec.step}
          placeholder={spec.placeholder}
          value={typeof raw === 'number' ? raw : ''}
          onChange={(e) => set(e.target.value === '' ? undefined : Number(e.target.value))}
        />
      )
      break
    case 'range': {
      const num = typeof raw === 'number' ? raw : (spec.min ?? 0)
      control = (
        <div className="flex items-center gap-3">
          <input
            id={id}
            type="range"
            min={spec.min}
            max={spec.max}
            step={spec.step}
            value={num}
            onChange={(e) => set(Number(e.target.value))}
            className="flex-1 accent-iris"
          />
          <span className="w-10 text-right text-xs tabular-nums text-text-secondary">{num}</span>
        </div>
      )
      break
    }
    case 'select':
      control = (
        <Select id={id} value={typeof raw === 'string' ? raw : ''} onChange={(e) => set(e.target.value)}>
          <option value="">Default</option>
          {spec.options?.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
      )
      break
    case 'textarea':
      control = (
        <Textarea id={id} rows={3} placeholder={spec.placeholder} value={typeof raw === 'string' ? raw : ''} onChange={(e) => set(e.target.value)} />
      )
      break
    case 'tags':
      control = <TagsEditor value={Array.isArray(raw) ? (raw as string[]) : []} placeholder={spec.placeholder} onChange={set} />
      break
    case 'keyvalue':
      control = <KeyValueEditor value={raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}} onChange={set} />
      break
    case 'ontology-picker':
      control = (
        <Select id={id} value={typeof raw === 'string' ? raw : ''} onChange={(e) => set(e.target.value || undefined)}>
          <option value="">Select an ontology…</option>
          {(ontologies ?? []).map((o) => (
            <option key={o.id} value={o.id}>
              {o.name} (v{o.version})
            </option>
          ))}
        </Select>
      )
      break
    default:
      control = (
        <Input id={id} placeholder={spec.placeholder} value={typeof raw === 'string' ? raw : ''} onChange={(e) => set(e.target.value)} />
      )
  }

  return (
    <Field label={spec.label} htmlFor={id}>
      {control}
      {spec.help && <p className="mt-1 text-xs text-text-muted">{spec.help}</p>}
    </Field>
  )
}

/**
 * Curated, per-type configuration panel. Renders a data-flow summary (what this
 * step receives and from where, derived from the graph edges) followed by the
 * hand-authored config fields for the step type.
 */
export function StepConfigPanel({
  typeKey,
  config,
  inputs,
  onConfigChange,
}: {
  typeKey: string
  config: Record<string, unknown>
  inputs: ResolvedInput[]
  onConfigChange: (c: Record<string, unknown>) => void
}) {
  const meta = STEP_META[typeKey]

  if (!meta) {
    return <p className="text-sm text-text-muted">Unknown step type — no curated configuration available.</p>
  }

  return (
    <div className="space-y-5">
      <p className="text-sm text-text-secondary">{meta.blurb}</p>

      {!meta.runnable && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          This step type isn’t executable yet — you can design and save it, but runs will not process it.
        </div>
      )}

      {inputs.length > 0 && (
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">Inputs</p>
          <ul className="space-y-1.5">
            {inputs.map((inp) => (
              <li key={inp.key} className="flex items-center gap-2 text-xs">
                <code className="rounded bg-surface-3 px-1.5 py-0.5 text-text-primary">{inp.key}</code>
                <span className="text-text-muted">←</span>
                <span className={inp.ref ? 'text-text-secondary' : 'text-error'}>{inp.source}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-text-muted">Connect an upstream step to change where these come from.</p>
        </div>
      )}

      {meta.outputs.length > 0 && (
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">Outputs</p>
          <div className="flex flex-wrap gap-1.5">
            {meta.outputs.map((o) => (
              <code key={o} className="rounded bg-surface-3 px-1.5 py-0.5 text-xs text-text-secondary">
                {o}
              </code>
            ))}
          </div>
        </div>
      )}

      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">Configuration</p>
        {meta.fields.length === 0 ? (
          <p className="text-sm text-text-muted">This step has no configurable parameters.</p>
        ) : (
          <div className="space-y-4">
            {meta.fields.map((spec) => (
              <FieldRow key={spec.key} spec={spec} config={config} onConfigChange={onConfigChange} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
