import { useCallback, useMemo, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  ReactFlow,
  Background,
  MiniMap,
  Panel,
  addEdge,
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  useReactFlow,
  BackgroundVariant,
  MarkerType,
} from '@xyflow/react'
import type { Connection, Edge } from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { StepNode } from '../components/workflow/StepNode'
import type { StepNodeType, StepNodeData } from '../components/workflow/StepNode'
import { StepPalette } from '../components/workflow/StepPalette'
import { StepConfigPanel } from '../components/workflow/StepConfigPanel'
import { useWorkflow, useUpdateWorkflow } from '../api/workflows'
import { useCreateRun } from '../api/runs'
import { useRegistryTypes } from '../api/registry'
import { usePinProject } from '../lib/useActiveProject'
import { STEP_TYPES } from '../lib/stepCatalog'
import { STEP_META, stepColor, stepLabel, resolveInputs, buildInputs, extractRunParams } from '../lib/stepMeta'
import { Button, Drawer, Field, Input } from '../components/ui'
import { RunParamsDialog } from '../components/runs/RunParamsDialog'
import { toast } from '../store/toast'
import { validateDag, layeredLayout, type GraphNode, type GraphEdge } from '../lib/workflowGraph'

const EDGE_OPTIONS = { type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18 } } as const

function defToNodes(definition: Record<string, unknown>): StepNodeType[] {
  const steps = (definition.steps as Array<Record<string, unknown>>) ?? []
  return steps.map((s, i) => ({
    id: String(s.id ?? `step-${i}`),
    type: 'step',
    position: (s.position as { x: number; y: number }) ?? { x: i * 240, y: 150 },
    data: {
      label: String(s.label ?? s.type ?? 'Step'),
      type_key: String(s.type ?? ''),
      status: null,
      config: (s.config as Record<string, unknown>) ?? {},
    },
  }))
}

function defToEdges(definition: Record<string, unknown>): Edge[] {
  const edges = (definition.edges as Array<Record<string, unknown>>) ?? []
  return edges.map((e, i) => ({
    id: String(e.id ?? `e${i}`),
    source: String(e.source ?? e.from ?? ''),
    target: String(e.target ?? e.to ?? ''),
    sourceHandle: e.sourceHandle != null ? String(e.sourceHandle) : undefined,
    targetHandle: e.targetHandle != null ? String(e.targetHandle) : undefined,
    ...EDGE_OPTIONS,
  }))
}

function nodesToDef(nodes: StepNodeType[], edges: Edge[]): Record<string, unknown> {
  // Lightweight views for input-ref resolution.
  const gNodes = nodes.map((n) => ({ id: n.id, type_key: n.data.type_key, label: n.data.label }))
  const gEdges = edges.map((e) => ({ source: e.source, target: e.target }))
  return {
    steps: nodes.map((n) => {
      const inputs = buildInputs(n.id, n.data.type_key, gNodes, gEdges)
      const step: Record<string, unknown> = {
        id: n.id,
        type: n.data.type_key,
        label: n.data.label,
        position: n.position,
        config: n.data.config ?? {},
      }
      // Edge-derived `$steps.../$run.params` references the engine resolves.
      if (Object.keys(inputs).length > 0) step.inputs = inputs
      return step
    }),
    // sourceHandle/targetHandle are UI-only; the engine reads source/target.
    edges: edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle ?? null,
      targetHandle: e.targetHandle ?? null,
    })),
  }
}

function ControlBtn({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="flex h-8 w-8 items-center justify-center rounded-md text-text-secondary transition-colors hover:bg-surface-3 hover:text-text-primary"
    >
      {children}
    </button>
  )
}

function FlowCanvas({ workflowId }: { workflowId: string }) {
  const navigate = useNavigate()
  const { data: workflow } = useWorkflow(workflowId)
  const updateWorkflow = useUpdateWorkflow(workflowId)
  const createRun = useCreateRun()
  const { data: registryTypes } = useRegistryTypes('step')
  const { screenToFlowPosition, zoomIn, zoomOut, fitView } = useReactFlow()

  const [nodes, setNodes, onNodesChange] = useNodesState<StepNodeType>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [loaded, setLoaded] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showIssues, setShowIssues] = useState(false)
  const [paramsDialogOpen, setParamsDialogOpen] = useState(false)
  const [pendingParams, setPendingParams] = useState<string[]>([])

  const selectedNode = nodes.find((n) => n.id === selectedId) ?? null

  const schemaByType = useMemo(() => {
    const m = new Map<string, Record<string, unknown>>()
    for (const rt of registryTypes ?? []) m.set(rt.type_key, rt.json_schema)
    return m
  }, [registryTypes])

  const graph = useMemo(() => {
    const gNodes: GraphNode[] = nodes.map((n) => ({
      id: n.id,
      type_key: n.data.type_key,
      label: n.data.label,
      config: n.data.config ?? {},
    }))
    const gEdges: GraphEdge[] = edges.map((e) => ({ source: e.source, target: e.target }))
    return { gNodes, gEdges }
  }, [nodes, edges])

  const issues = useMemo(() => validateDag(graph.gNodes, graph.gEdges, schemaByType), [graph, schemaByType])
  const errorCount = issues.filter((i) => i.level === 'error').length

  const selectedInputs = useMemo(
    () => (selectedNode ? resolveInputs(selectedNode.id, selectedNode.data.type_key, graph.gNodes, graph.gEdges) : []),
    [selectedNode, graph],
  )

  const handleTidy = useCallback(() => {
    const pos = layeredLayout(graph.gNodes, graph.gEdges)
    setNodes((nds) => nds.map((n) => (pos.has(n.id) ? { ...n, position: pos.get(n.id)! } : n)))
  }, [graph, setNodes])

  const onNodeClick = useCallback((_e: React.MouseEvent, node: StepNodeType) => setSelectedId(node.id), [])

  const patchNodeData = useCallback(
    (id: string, patch: Partial<StepNodeType['data']>) => {
      setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)))
    },
    [setNodes],
  )

  const deleteNode = useCallback(
    (id: string) => {
      setNodes((nds) => nds.filter((n) => n.id !== id))
      setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id))
      setSelectedId(null)
    },
    [setNodes, setEdges],
  )

  useEffect(() => {
    if (workflow && !loaded) {
      setNodes(defToNodes(workflow.definition))
      setEdges(defToEdges(workflow.definition))
      setLoaded(true)
    }
  }, [workflow, loaded, setNodes, setEdges])

  const nodeTypes = useMemo(() => ({ step: StepNode }), [])

  const onConnect = useCallback(
    (params: Connection) => setEdges((eds) => addEdge({ ...params, ...EDGE_OPTIONS }, eds)),
    [setEdges],
  )

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      const typeKey = e.dataTransfer.getData('application/xyflow')
      if (!typeKey) return
      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      setNodes((nds) => [
        ...nds,
        {
          id: `${typeKey}-${Date.now()}`,
          type: 'step',
          position,
          data: { label: stepLabel(typeKey), type_key: typeKey, status: null, config: {} },
        },
      ])
    },
    [screenToFlowPosition, setNodes],
  )

  async function handleSave() {
    try {
      const wf = await updateWorkflow.mutateAsync({ definition: nodesToDef(nodes, edges) })
      toast.success('Workflow saved', `Now at v${wf.version}`)
    } catch {
      // Surfaced by the global mutation error handler (toast).
    }
  }

  async function handleRun() {
    const required = extractRunParams(workflow?.definition ?? {})
    if (required.length > 0) {
      setPendingParams(required)
      setParamsDialogOpen(true)
      return
    }
    try {
      const run = await createRun.mutateAsync({ workflowId })
      navigate(`/runs/${run.id}`)
    } catch {
      // Surfaced by the global mutation error handler (toast).
    }
  }

  async function handleRunWithParams(values: Record<string, string>) {
    try {
      const run = await createRun.mutateAsync({ workflowId, params: values })
      setParamsDialogOpen(false)
      navigate(`/runs/${run.id}`)
    } catch {
      // Surfaced by the global mutation error handler (toast).
    }
  }

  return (
    <div className="relative flex-1">
      <div className="absolute right-4 top-4 z-10 flex gap-2">
        <Button variant="secondary" onClick={handleTidy} disabled={nodes.length === 0} className="shadow-sm">
          Tidy
        </Button>
        <Button variant="secondary" onClick={handleSave} loading={updateWorkflow.isPending} className="shadow-sm">
          Save
        </Button>
        <Button
          onClick={handleRun}
          loading={createRun.isPending}
          disabled={errorCount > 0 || nodes.length === 0}
          title={errorCount > 0 ? 'Fix validation errors before running' : undefined}
          className="shadow-sm"
        >
          Run workflow
        </Button>
      </div>

      {nodes.length > 0 && (
        <div className="absolute left-4 top-4 z-10 max-w-sm">
          {issues.length === 0 ? (
            <div className="rounded-lg border border-border bg-surface-2 px-3 py-1.5 text-xs font-medium text-success shadow-sm">
              Valid DAG · {nodes.length} step{nodes.length === 1 ? '' : 's'}
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-surface-2 shadow-sm">
              <button
                type="button"
                onClick={() => setShowIssues((v) => !v)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-xs font-medium text-text-secondary hover:bg-surface-3"
              >
                <span
                  aria-hidden
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ backgroundColor: errorCount > 0 ? 'var(--cv-error)' : 'var(--cv-warning)' }}
                />
                {issues.length} issue{issues.length === 1 ? '' : 's'}
                {errorCount > 0 && ` · ${errorCount} blocking`}
                <span className="ml-1 text-text-muted">{showIssues ? '▾' : '▸'}</span>
              </button>
              {showIssues && (
                <ul className="border-t border-border">
                  {issues.map((issue, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        onClick={() => issue.nodeId && setSelectedId(issue.nodeId)}
                        className="flex w-full items-start gap-2 px-3 py-1.5 text-left text-xs text-text-secondary hover:bg-surface-3"
                      >
                        <span style={{ color: issue.level === 'error' ? 'var(--cv-error)' : 'var(--cv-warning)' }}>
                          {issue.level === 'error' ? '✕' : '!'}
                        </span>
                        <span>{issue.message}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        nodeTypes={nodeTypes}
        onDrop={onDrop}
        onDragOver={onDragOver}
        defaultEdgeOptions={EDGE_OPTIONS}
        proOptions={{ hideAttribution: true }}
        fitView
        fitViewOptions={{ padding: 0.3 }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--border)" />

        {/* Custom side controls — token-styled, no default react-flow chrome. */}
        <Panel position="bottom-left">
          <div className="flex flex-col gap-0.5 rounded-lg border border-border bg-surface-2 p-1 shadow-sm">
            <ControlBtn label="Zoom in" onClick={() => zoomIn()}>
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
            </ControlBtn>
            <ControlBtn label="Zoom out" onClick={() => zoomOut()}>
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M5 12h14" /></svg>
            </ControlBtn>
            <ControlBtn label="Fit view" onClick={() => fitView({ padding: 0.3 })}>
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3" /></svg>
            </ControlBtn>
            <ControlBtn label="Tidy layout" onClick={handleTidy}>
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="6" height="6" rx="1" /><rect x="15" y="4" width="6" height="6" rx="1" /><rect x="9" y="14" width="6" height="6" rx="1" /><path d="M6 10v2a2 2 0 0 0 2 2h1m9-4v2a2 2 0 0 1-2 2h-1" /></svg>
            </ControlBtn>
          </div>
        </Panel>

        <MiniMap
          pannable
          zoomable
          className="overflow-hidden rounded-lg border border-border"
          style={{ background: 'var(--surface-1)' }}
          maskColor="rgba(10, 11, 13, 0.6)"
          nodeColor={(node) => stepColor((node.data as StepNodeData).type_key)}
          nodeStrokeColor={(node) => stepColor((node.data as StepNodeData).type_key)}
          nodeBorderRadius={4}
        />
      </ReactFlow>

      <Drawer
        open={selectedNode !== null}
        onClose={() => setSelectedId(null)}
        title={selectedNode ? `Configure · ${stepLabel(selectedNode.data.type_key)}` : ''}
      >
        {selectedNode && (
          <div className="space-y-5">
            <Field label="Step label" htmlFor="step-label">
              <Input
                id="step-label"
                value={selectedNode.data.label}
                onChange={(e) => patchNodeData(selectedNode.id, { label: e.target.value })}
              />
            </Field>

            <StepConfigPanel
              typeKey={selectedNode.data.type_key}
              config={selectedNode.data.config ?? {}}
              inputs={selectedInputs}
              onConfigChange={(config) => patchNodeData(selectedNode.id, { config })}
            />

            <div className="flex justify-between border-t border-border pt-4">
              <Button
                variant="ghost"
                size="sm"
                className="text-error hover:text-error"
                onClick={() => deleteNode(selectedNode.id)}
              >
                Delete step
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setSelectedId(null)}>
                Done
              </Button>
            </div>
          </div>
        )}
      </Drawer>

      <RunParamsDialog
        params={pendingParams}
        open={paramsDialogOpen}
        onConfirm={handleRunWithParams}
        onCancel={() => setParamsDialogOpen(false)}
        loading={createRun.isPending}
        projectId={workflow?.project_id}
      />
    </div>
  )
}

export default function WorkflowBuilder() {
  const { id } = useParams<{ id: string }>()
  const { data: workflow } = useWorkflow(id)
  const { data: registryTypes } = useRegistryTypes('step')
  usePinProject(workflow?.project_id)

  const paletteSteps = useMemo(() => {
    const keys =
      registryTypes && registryTypes.length > 0
        ? registryTypes.map((rt) => rt.type_key)
        : STEP_TYPES.map((s) => s.type_key)
    return keys.map((tk) => ({
      type_key: tk,
      label: STEP_META[tk]?.label ?? tk.replace('step.', '').replace(/_/g, ' '),
      description: STEP_META[tk]?.blurb ?? '',
      color: stepColor(tk),
    }))
  }, [registryTypes])

  return (
    <div className="flex" style={{ height: 'calc(100vh - 56px)' }}>
      <StepPalette steps={paletteSteps} />
      {id && workflow && (
        <ReactFlowProvider>
          <FlowCanvas workflowId={id} />
        </ReactFlowProvider>
      )}
      {!workflow && (
        <div className="flex-1 flex items-center justify-center text-text-muted text-sm">Loading workflow…</div>
      )}
    </div>
  )
}
