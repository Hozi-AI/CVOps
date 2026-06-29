import { useState } from 'react'
import { Dialog } from '../ui/Dialog'
import { Button } from '../ui'
import { useDataSources } from '../../api/data-sources'

interface RunParamsDialogProps {
  params: string[]
  open: boolean
  onConfirm: (values: Record<string, string>) => void
  onCancel: () => void
  loading?: boolean
  projectId?: string
}

export function RunParamsDialog({ params, open, onConfirm, onCancel, loading, projectId }: RunParamsDialogProps) {
  const [values, setValues] = useState<Record<string, string>>({})
  const { data: dataSources } = useDataSources(projectId)

  const allFilled = params.every((p) => (values[p] ?? '').trim() !== '')

  function handleConfirm() {
    if (!allFilled) return
    onConfirm(values)
  }

  function handleClose() {
    setValues({})
    onCancel()
  }

  return (
    <Dialog open={open} onClose={handleClose} title="Run workflow">
      <div className="flex flex-col gap-4">
        <p className="text-sm text-text-secondary">
          This workflow requires the following parameters before it can run.
        </p>
        <div className="flex flex-col gap-3">
          {params.map((param) => (
            <div key={param} className="flex flex-col gap-1">
              <label className="text-xs font-medium text-text-primary" htmlFor={`param-${param}`}>
                {param}
              </label>
              {param === 'source_id' && dataSources && dataSources.length > 0 ? (
                <select
                  id={`param-${param}`}
                  value={values[param] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [param]: e.target.value }))}
                  className="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:ring-2 focus:ring-iris-500"
                >
                  <option value="">Select a data source…</option>
                  {dataSources.map((ds) => {
                    const name = typeof ds.metadata?.name === 'string' ? ds.metadata.name : ds.type
                    return (
                      <option key={ds.id} value={ds.id}>
                        {name} — {ds.id.slice(0, 8)}
                      </option>
                    )
                  })}
                </select>
              ) : (
                <input
                  id={`param-${param}`}
                  type="text"
                  value={values[param] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [param]: e.target.value }))}
                  placeholder={`Enter ${param}`}
                  className="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-iris-500"
                />
              )}
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" size="sm" onClick={handleClose} disabled={loading}>
            Cancel
          </Button>
          <Button size="sm" onClick={handleConfirm} disabled={!allFilled} loading={loading}>
            Run
          </Button>
        </div>
      </div>
    </Dialog>
  )
}
