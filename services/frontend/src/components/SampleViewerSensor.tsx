import { useEffect, useState } from 'react'

interface SensorRow {
  timestamp_ms: number
  [key: string]: number | string
}

interface Props {
  blobUrl: string
}

export function SampleViewerSensor({ blobUrl }: Props) {
  const [rows, setRows] = useState<SensorRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch(blobUrl)
      .then((r) => r.json())
      .then(setRows)
      .catch(() => setError('Failed to load sensor data'))
  }, [blobUrl])

  if (error) return <div className="text-error text-sm">{error}</div>
  if (!rows) return <div className="text-text-muted text-sm">Loading…</div>

  const cols = rows.length > 0 ? Object.keys(rows[0]) : []
  const preview = rows.slice(0, 20)

  return (
    <div className="overflow-x-auto">
      <p className="mb-2 text-xs text-text-muted">{rows.length} rows · showing first 20</p>
      <table className="w-full border-collapse text-xs text-text-primary">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c} className="border border-border bg-surface-2 px-2 py-1 text-left">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {preview.map((row, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c} className="border border-border px-2 py-1 font-mono">
                  {String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
