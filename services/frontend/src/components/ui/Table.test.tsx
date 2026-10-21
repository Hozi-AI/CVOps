import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './Table'

describe('Table', () => {
  it('renders semantic table markup with column headers and a caption', () => {
    render(
      <Table>
        <TableCaption>Model metrics</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead numeric>mAP50</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>yolo-v1</TableCell>
            <TableCell numeric>0.81</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )

    const table = screen.getByRole('table', { name: 'Model metrics' })
    const headers = within(table).getAllByRole('columnheader')
    expect(headers.map((h) => h.textContent)).toEqual(['Model', 'mAP50'])
    expect(headers[0]).toHaveAttribute('scope', 'col')
    expect(within(table).getByRole('cell', { name: '0.81' })).toHaveClass('text-right', 'tabular-nums')
  })

  it('lets a row header override the default column scope', () => {
    render(
      <Table>
        <TableBody>
          <TableRow>
            <TableHead scope="row">epochs</TableHead>
            <TableCell numeric>50</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    )
    expect(screen.getByRole('rowheader', { name: 'epochs' })).toBeInTheDocument()
  })
})
