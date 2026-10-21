import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Tooltip } from './Tooltip'

describe('Tooltip', () => {
  it('is hidden until the trigger gets keyboard focus, then describes it', async () => {
    render(
      <Tooltip content="Delete sample">
        <button type="button" aria-label="Delete">
          ✕
        </button>
      </Tooltip>,
    )
    expect(screen.queryByRole('tooltip')).toBeNull()

    await userEvent.tab()
    const trigger = screen.getByRole('button', { name: 'Delete' })
    expect(trigger).toHaveFocus()
    const tip = await screen.findByRole('tooltip')
    expect(tip).toHaveTextContent('Delete sample')
    // The hint is announced as a description, not as a replacement for the label.
    expect(trigger).toHaveAccessibleName('Delete')
    expect(trigger).toHaveAccessibleDescription('Delete sample')
  })

  it('closes on Escape', async () => {
    render(
      <Tooltip content="Refresh">
        <button type="button" aria-label="Refresh">
          ↻
        </button>
      </Tooltip>,
    )
    await userEvent.tab()
    await screen.findByRole('tooltip')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })
})
