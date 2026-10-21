import { cloneElement, type ReactElement } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Chart } from './Chart'
import ChartCanvas from './ChartCanvas'

// jsdom has no layout, so ResponsiveContainer would measure 0×0 and draw
// nothing. Give the chart a fixed size instead.
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactElement }) =>
      cloneElement(children as ReactElement<{ width: number; height: number }>, {
        width: 400,
        height: 200,
      }),
  }
})

const data = [
  { model: 'yolo-v1', map50: 0.812, map: 0.5 },
  { model: 'yolo-v2', map50: 0.874, map: null },
]
const series = [
  { key: 'map50', label: 'mAP50' },
  { key: 'map', label: 'mAP50-95' },
]

describe('Chart', () => {
  it('exposes the same numbers as a screen-reader table and hides the SVG', async () => {
    const { container } = render(
      <Chart
        type="bar"
        title="Model comparison"
        data={data}
        xKey="model"
        xLabel="Model"
        series={series}
        valueFormat={(v) => v.toFixed(2)}
      />,
    )

    expect(screen.getByRole('group', { name: 'Model comparison' })).toBeInTheDocument()
    const table = screen.getByRole('table', { name: 'Model comparison' })
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Model',
      'mAP50',
      'mAP50-95',
    ])
    const v2 = within(table).getByRole('rowheader', { name: 'yolo-v2' }).closest('tr')!
    // Formatted values, and a dash for a missing one.
    expect(within(v2).getAllByRole('cell').map((c) => c.textContent)).toEqual(['0.87', '—'])

    // The lazily loaded SVG lands inside the aria-hidden wrapper.
    await waitFor(() =>
      expect(container.querySelector('[aria-hidden="true"] svg.recharts-surface')).not.toBeNull(),
    )
  })

  it('names every series in the legend, for bar and line charts', () => {
    for (const type of ['bar', 'line'] as const) {
      const { container, unmount } = render(
        <ChartCanvas type={type} data={data} xKey="model" series={series} />,
      )
      const legend = container.querySelector('.recharts-legend-wrapper') as HTMLElement
      expect(legend).toHaveTextContent('mAP50')
      expect(legend).toHaveTextContent('mAP50-95')
      unmount()
    }
  })

  it('colours series from the theme tokens and dashes the second line', () => {
    const { container } = render(
      <ChartCanvas type="line" data={data} xKey="model" series={series} />,
    )
    const lines = container.querySelectorAll('path.recharts-line-curve')
    expect(lines).toHaveLength(2)
    expect(lines[0]).toHaveAttribute('stroke', 'var(--chart-1)')
    expect(lines[1]).toHaveAttribute('stroke', 'var(--chart-2)')
    expect(lines[1].getAttribute('stroke-dasharray')).toBeTruthy()
  })
})
