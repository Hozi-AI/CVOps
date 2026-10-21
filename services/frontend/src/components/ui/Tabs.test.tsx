import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './Tabs'

function Example({ onValueChange }: { onValueChange?: (v: string) => void }) {
  return (
    <Tabs defaultValue="files" onValueChange={onValueChange}>
      <TabsList aria-label="Commit view">
        <TabsTrigger value="files">Files</TabsTrigger>
        <TabsTrigger value="changes">Changes</TabsTrigger>
        <TabsTrigger value="locked" disabled>
          Locked
        </TabsTrigger>
      </TabsList>
      <TabsContent value="files">Files panel</TabsContent>
      <TabsContent value="changes">Changes panel</TabsContent>
      <TabsContent value="locked">Locked panel</TabsContent>
    </Tabs>
  )
}

describe('Tabs', () => {
  it('exposes tab semantics and links each tab to its panel', () => {
    render(<Example />)
    expect(screen.getByRole('tablist', { name: 'Commit view' })).toBeInTheDocument()
    const files = screen.getByRole('tab', { name: 'Files' })
    expect(files).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Changes' })).toHaveAttribute('aria-selected', 'false')

    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveTextContent('Files panel')
    expect(files).toHaveAttribute('aria-controls', panel.id)
    // Inactive panels unmount, like the conditional renders they replace.
    expect(screen.queryByText('Changes panel')).toBeNull()
  })

  it('switches panels on click', async () => {
    const onValueChange = vi.fn()
    render(<Example onValueChange={onValueChange} />)
    await userEvent.click(screen.getByRole('tab', { name: 'Changes' }))
    expect(onValueChange).toHaveBeenCalledWith('changes')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Changes panel')
  })

  it('moves with the arrow keys, skipping disabled tabs and wrapping', async () => {
    render(<Example />)
    await userEvent.tab()
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveFocus()

    await userEvent.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Changes' })).toHaveFocus()
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Changes panel')

    // "Locked" is disabled, so the next stop wraps back to the first tab.
    await userEvent.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveFocus()
  })
})
