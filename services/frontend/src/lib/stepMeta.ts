/**
 * Hand-authored metadata for each workflow step type: brand colour, curated
 * config fields, and the input/output "ports" the engine wires through
 * `inputs` references. The registry only ships JSON Schema + empty ui_hints,
 * so the human-facing labelling and data-flow wiring live here on the frontend.
 *
 * Input wiring follows a name-matching convention: an input port (e.g.
 * `sample_ids`) is satisfied by the nearest upstream step whose outputs include
 * a port of the same name, producing a `$steps.<id>.outputs.<key>` reference.
 * When no upstream provides it, a declared run-param input falls back to
 * `$run.params.<key>` (how the dispatcher seeds the entry step).
 */

export type FieldWidget = 'text' | 'number' | 'range' | 'select' | 'textarea' | 'tags' | 'keyvalue'

export interface FieldSpec {
  key: string
  label: string
  help?: string
  widget: FieldWidget
  placeholder?: string
  min?: number
  max?: number
  step?: number
  options?: { value: string; label: string }[]
}

export interface StepMeta {
  label: string
  blurb: string
  /** false → configurable but not yet executable (stub step). */
  runnable: boolean
  color: string
  inputs: string[]
  outputs: string[]
  /** Input ports that fall back to `$run.params.<key>` with no upstream. */
  runParamInputs: string[]
  fields: FieldSpec[]
}

export const STEP_META: Record<string, StepMeta> = {
  'step.extract_frames': {
    label: 'Extract Frames',
    blurb: 'Decode a video source into sample frames.',
    runnable: true,
    color: '#38BDF8',
    inputs: ['source_id'],
    outputs: ['sample_ids', 'sample_count'],
    runParamInputs: ['source_id'],
    fields: [
      { key: 'interval_seconds', label: 'Frame interval', help: 'Seconds between sampled frames.', widget: 'number', min: 0.1, step: 0.1, placeholder: '2.0' },
      { key: 'max_frames', label: 'Max frames', help: 'Optional cap on the number of frames.', widget: 'number', min: 1, placeholder: 'no limit' },
    ],
  },
  'step.auto_label': {
    label: 'Auto Label',
    blurb: 'Run an inference model to pre-label the incoming frames.',
    runnable: false,
    color: '#8E80FF',
    inputs: ['sample_ids'],
    outputs: ['annotation_revision_ids'],
    runParamInputs: ['sample_ids'],
    fields: [
      { key: 'model_version_id', label: 'Model version ID', help: 'Model version used for inference.', widget: 'text', placeholder: 'uuid' },
      { key: 'confidence_threshold', label: 'Confidence threshold', help: 'Drop predictions below this score.', widget: 'range', min: 0, max: 1, step: 0.05 },
    ],
  },
  'step.human_review': {
    label: 'Human Review',
    blurb: 'Push frames to a labeling backend and wait for human review.',
    runnable: false,
    color: '#FBBF24',
    inputs: ['sample_ids', 'annotation_revision_ids'],
    outputs: ['annotation_revision_ids'],
    runParamInputs: ['sample_ids', 'annotation_revision_ids'],
    fields: [
      { key: 'labeling_backend', label: 'Labeling backend', widget: 'select', options: [{ value: 'cvat', label: 'CVAT' }] },
      { key: 'assignees', label: 'Assignees', help: 'Usernames to assign the review task to.', widget: 'tags', placeholder: 'add username…' },
      { key: 'task_name_prefix', label: 'Task name prefix', widget: 'text', placeholder: 'review-' },
    ],
  },
  'step.commit_dataset': {
    label: 'Commit Dataset',
    blurb: 'Freeze the reviewed samples into an immutable dataset commit.',
    runnable: true,
    color: '#34D399',
    inputs: ['sample_ids', 'annotation_revision_ids'],
    outputs: ['commit_id', 'ref_id', 'dataset_id'],
    runParamInputs: ['sample_ids', 'annotation_revision_ids'],
    fields: [
      { key: 'dataset_name', label: 'Dataset name', help: 'Target dataset to commit into.', widget: 'text', placeholder: 'my-dataset' },
      { key: 'branch_name', label: 'Branch', widget: 'text', placeholder: 'main' },
      { key: 'split_strategy', label: 'Split strategy', widget: 'select', options: [
        { value: 'by_source_group', label: 'By source group' },
        { value: 'random_seeded', label: 'Random (seeded)' },
      ] },
      { key: 'train_ratio', label: 'Train ratio', widget: 'range', min: 0.1, max: 0.9, step: 0.05 },
      { key: 'val_ratio', label: 'Val ratio', widget: 'range', min: 0.05, max: 0.5, step: 0.05 },
      { key: 'seed', label: 'Seed', widget: 'number', min: 0, placeholder: '42' },
      { key: 'ontology_id', label: 'Ontology ID', widget: 'text', placeholder: 'uuid' },
      { key: 'message', label: 'Commit message', widget: 'textarea', placeholder: 'Describe this commit…' },
    ],
  },
  'step.export_yolo': {
    label: 'Export YOLO',
    blurb: 'Render the committed dataset as a YOLO archive.',
    runnable: true,
    color: '#C6F24E',
    inputs: ['commit_id'],
    outputs: ['export_blob_hash', 'commit_id'],
    runParamInputs: ['commit_id'],
    fields: [
      { key: 'ontology_id', label: 'Ontology override', help: 'Optional — defaults to the commit ontology.', widget: 'text', placeholder: 'uuid' },
    ],
  },
  'step.train': {
    label: 'Train',
    blurb: 'Clone a trainer repo and run it on the exported dataset.',
    runnable: true,
    color: '#FB7185',
    inputs: ['export_blob_hash', 'commit_id'],
    outputs: ['model_version_id', 'metrics'],
    runParamInputs: [],
    fields: [
      { key: 'training_container_id', label: 'Training container ID', help: 'Optional pre-built container.', widget: 'text', placeholder: 'uuid' },
      { key: 'git_url', label: 'Trainer git URL', widget: 'text', placeholder: 'https://github.com/org/trainer.git' },
      { key: 'branch', label: 'Branch', widget: 'text', placeholder: 'main' },
      { key: 'entry_point', label: 'Entry point', widget: 'text', placeholder: 'train.py' },
      { key: 'hyperparams', label: 'Hyperparameters', widget: 'keyvalue' },
    ],
  },
}

export function stepColor(typeKey: string): string {
  return STEP_META[typeKey]?.color ?? '#94a3b8'
}

export function stepLabel(typeKey: string): string {
  return STEP_META[typeKey]?.label ?? typeKey.replace('step.', '').replace(/_/g, ' ')
}

interface GraphNodeLite {
  id: string
  type_key: string
  label: string
}

interface GraphEdgeLite {
  source: string
  target: string
}

/** Nearest upstream node (BFS over predecessors) whose outputs include `key`. */
function findProvider(
  nodeId: string,
  key: string,
  nodes: GraphNodeLite[],
  edges: GraphEdgeLite[],
): GraphNodeLite | null {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const preds = new Map<string, string[]>()
  for (const e of edges) {
    const arr = preds.get(e.target)
    if (arr) arr.push(e.source)
    else preds.set(e.target, [e.source])
  }
  const seen = new Set<string>([nodeId])
  let frontier = preds.get(nodeId) ?? []
  while (frontier.length > 0) {
    const nextFrontier: string[] = []
    for (const pid of frontier) {
      if (seen.has(pid)) continue
      seen.add(pid)
      const pnode = byId.get(pid)
      if (pnode && STEP_META[pnode.type_key]?.outputs.includes(key)) return pnode
      nextFrontier.push(...(preds.get(pid) ?? []))
    }
    frontier = nextFrontier
  }
  return null
}

export interface ResolvedInput {
  key: string
  ref: string
  /** Human description of where the value comes from. */
  source: string
}

/** Resolve a node's input ports to engine references, following the graph edges. */
export function resolveInputs(
  nodeId: string,
  typeKey: string,
  nodes: GraphNodeLite[],
  edges: GraphEdgeLite[],
): ResolvedInput[] {
  const meta = STEP_META[typeKey]
  if (!meta) return []
  const resolved: ResolvedInput[] = []
  for (const key of meta.inputs) {
    const provider = findProvider(nodeId, key, nodes, edges)
    if (provider) {
      resolved.push({ key, ref: `$steps.${provider.id}.outputs.${key}`, source: provider.label })
    } else if (meta.runParamInputs.includes(key)) {
      resolved.push({ key, ref: `$run.params.${key}`, source: 'run parameter' })
    } else {
      resolved.push({ key, ref: '', source: 'not connected' })
    }
  }
  return resolved
}

/** Extract unique $run.params.<name> references from a saved workflow definition. */
export function extractRunParams(definition: Record<string, unknown>): string[] {
  const steps = (definition.steps ?? []) as Array<{ inputs?: Record<string, string> }>
  const seen = new Set<string>()
  const RE = /^\$run\.params\.(.+)$/
  for (const step of steps) {
    for (const ref of Object.values(step.inputs ?? {})) {
      const m = RE.exec(ref)
      if (m) seen.add(m[1])
    }
  }
  return [...seen]
}

/** The `inputs` object persisted onto a step in the workflow definition. */
export function buildInputs(
  nodeId: string,
  typeKey: string,
  nodes: GraphNodeLite[],
  edges: GraphEdgeLite[],
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const r of resolveInputs(nodeId, typeKey, nodes, edges)) {
    if (r.ref) out[r.key] = r.ref
  }
  return out
}
