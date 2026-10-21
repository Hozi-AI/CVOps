import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ChartProps } from './Chart'

// Lazily loaded by Chart.tsx — import `Chart`, never this file, so Recharts
// stays out of the main bundle.

const PALETTE_SIZE = 8
const color = (i: number) => `var(--chart-${(i % PALETTE_SIZE) + 1})`
// Lines also differ by dash pattern so series stay distinguishable without colour.
const DASHES = ['', '6 3', '2 3', '8 3 2 3']

const AXIS = { stroke: 'var(--border-strong)', tick: { fill: 'var(--text-muted)', fontSize: 12 } }
const TOOLTIP = {
  contentStyle: {
    background: 'var(--surface-3)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    color: 'var(--text-primary)',
    fontSize: 12,
  },
  labelStyle: { color: 'var(--text-secondary)' },
  cursor: { fill: 'var(--surface-3)', stroke: 'var(--border-strong)' },
}
const LEGEND = { wrapperStyle: { fontSize: 12, color: 'var(--text-secondary)' } }

type Props = Pick<ChartProps, 'type' | 'data' | 'xKey' | 'series' | 'valueFormat'>

export default function ChartCanvas({ type, data, xKey, series, valueFormat }: Props) {
  const formatter = valueFormat ? (v: unknown) => (typeof v === 'number' ? valueFormat(v) : String(v)) : undefined
  const common = (
    <>
      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
      <XAxis dataKey={xKey} {...AXIS} />
      <YAxis {...AXIS} tickFormatter={formatter} width={48} />
      <Tooltip {...TOOLTIP} formatter={formatter} />
      <Legend {...LEGEND} />
    </>
  )

  return (
    <ResponsiveContainer width="100%" height="100%">
      {type === 'bar' ? (
        <BarChart data={data} accessibilityLayer={false}>
          {common}
          {series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} fill={color(i)} radius={[4, 4, 0, 0]} />
          ))}
        </BarChart>
      ) : (
        <LineChart data={data} accessibilityLayer={false}>
          {common}
          {series.map((s, i) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={color(i)}
              strokeWidth={2}
              strokeDasharray={DASHES[i % DASHES.length]}
              dot={{ r: 3, fill: color(i) }}
            />
          ))}
        </LineChart>
      )}
    </ResponsiveContainer>
  )
}
