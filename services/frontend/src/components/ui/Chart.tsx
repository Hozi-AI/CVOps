import { lazy, Suspense } from 'react'
import { cn } from '../../lib/cn'
import { Skeleton } from './Skeleton'

export type ChartDatum = Record<string, string | number | null | undefined>

export interface ChartSeries {
  /** Field in each datum holding this series' value. */
  key: string
  /** Name shown in the legend, the tooltip and the screen-reader table. */
  label: string
}

export interface ChartProps {
  type: 'bar' | 'line'
  data: ChartDatum[]
  /** Field holding the category / x value (e.g. model name, epoch). */
  xKey: string
  /** Column header for `xKey` in the screen-reader table; defaults to `xKey`. */
  xLabel?: string
  series: ChartSeries[]
  /** Accessible name; also the caption of the screen-reader table. */
  title: string
  height?: number
  valueFormat?: (value: number) => string
  className?: string
}

// Recharts is ~100 KB gzipped; load it only on pages that actually draw a chart.
const ChartCanvas = lazy(() => import('./ChartCanvas'))

const fmt = (v: ChartDatum[string], f?: (n: number) => string) =>
  typeof v === 'number' ? (f ? f(v) : String(v)) : (v ?? '—')

/**
 * Bar or line chart themed with the `--chart-N` tokens. Meaning never rests on
 * colour alone: every chart has a legend and hover tooltips, line series also
 * differ by dash pattern, and the same numbers are rendered as a visually
 * hidden table for screen readers (the SVG itself is hidden from them).
 */
export function Chart({ title, data, xKey, xLabel, series, height = 240, valueFormat, className, type }: ChartProps) {
  return (
    <figure role="group" aria-label={title} className={cn('w-full', className)}>
      <div aria-hidden="true" style={{ height }}>
        <Suspense fallback={<Skeleton className="h-full w-full" />}>
          <ChartCanvas type={type} data={data} xKey={xKey} series={series} valueFormat={valueFormat} />
        </Suspense>
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">{xLabel ?? xKey}</th>
            {series.map((s) => (
              <th key={s.key} scope="col">
                {s.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((d, i) => (
            <tr key={i}>
              <th scope="row">{fmt(d[xKey])}</th>
              {series.map((s) => (
                <td key={s.key}>{fmt(d[s.key], valueFormat)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
