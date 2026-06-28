# Design: Workflow Run Params Dialog

**Date:** 2026-06-28  
**Status:** Approved

## Problem

When a user clicks "Run workflow" in the WorkflowBuilder canvas, the run is created with no params (`{}`). If the workflow contains steps whose inputs reference `$run.params.<key>` (e.g., `extract_frames` needing `source_id`), the engine fails immediately at input resolution with "Run param 'source_id' not found". There are no step records created and no way to recover via retry (retry copies the same empty params).

The `confirm-upload` auto-trigger path is unaffected — it already passes `{"source_id": str(ds.id)}` correctly.

## Scope

Frontend only. No backend changes required.

Files touched:
- `services/frontend/src/lib/stepMeta.ts` — add `extractRunParams` utility
- `services/frontend/src/components/runs/RunParamsDialog.tsx` — new modal component
- `services/frontend/src/pages/WorkflowBuilder.tsx` — update `handleRun`

## Design

### 1. `extractRunParams(definition)` utility

A pure function added to `stepMeta.ts`:

```ts
/** Extract unique $run.params.<name> references from a workflow definition. */
export function extractRunParams(definition: Record<string, unknown>): string[]
```

- Iterates `definition.steps[*].inputs` (each a `Record<string, string>`)
- Matches values against `/^\$run\.params\.(.+)$/`
- Returns deduplicated list of param names
- Returns `[]` if definition has no steps or no run-param references

### 2. `RunParamsDialog` component

A modal dialog (`services/frontend/src/components/runs/RunParamsDialog.tsx`):

**Props:**
```ts
interface RunParamsDialogProps {
  params: string[]           // param names to collect
  open: boolean
  onConfirm: (values: Record<string, string>) => void
  onCancel: () => void
  loading?: boolean
}
```

**Behavior:**
- Renders one labeled text input per param name
- All inputs required (Run button disabled until all are non-empty)
- Cancel closes without running
- Run calls `onConfirm` with collected values
- Uses existing design tokens (semantic color tokens, no raw hex)

### 3. `handleRun` update in `WorkflowBuilder.tsx`

```
click "Run workflow"
  ↓
extractRunParams(workflow.definition)
  ↓ no params                   ↓ params found
createRun immediately     open RunParamsDialog
                                ↓ user fills + clicks Run
                          createRun({ workflowId, params: values })
                                ↓ user clicks Cancel
                          close dialog, do nothing
```

State added to `FlowCanvas`:
- `showParamsDialog: boolean`
- `pendingParams: string[]`

## Error Handling

- If `workflow` is not yet loaded when "Run workflow" is clicked, the button is already disabled (nodes.length === 0 guard covers this).
- Validation: all param fields must be non-empty before confirming. No type coercion — all values are passed as strings (the engine stores them as JSONB strings).

## Testing

- Unit test for `extractRunParams`: empty definition returns `[]`, definition with `$run.params.*` references returns correct names, duplicates are deduplicated.
- The existing `runs.test.ts` `useCreateRun` test covers the API call shape; no changes needed there.
- Manual: build a canvas workflow with `extract_frames`, click "Run workflow", verify dialog appears with a `source_id` field, fill it, verify the run is created with correct params.
