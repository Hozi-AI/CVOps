import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { Button } from './Button'
import { Popover, PopoverClose } from './Popover'

function Example() {
  return (
    <>
      <Popover label="Filters" trigger={<Button>Filter</Button>}>
        <label>
          Min score <input />
        </label>
        <PopoverClose asChild>
          <Button>Done</Button>
        </PopoverClose>
      </Popover>
      <p>Outside</p>
    </>
  )
}

describe('Popover', () => {
  it('opens on click as a labelled dialog and moves focus inside', async () => {
    render(<Example />)
    const trigger = screen.getByRole('button', { name: 'Filter' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('dialog')).toBeNull()

    await userEvent.click(trigger)
    const panel = screen.getByRole('dialog', { name: 'Filters' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(panel).toContainElement(document.activeElement as HTMLElement)
  })

  it('closes on Escape and returns focus to the trigger', async () => {
    render(<Example />)
    const trigger = screen.getByRole('button', { name: 'Filter' })
    await userEvent.click(trigger)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(trigger).toHaveFocus()
  })

  it('closes on an outside click and from a PopoverClose button', async () => {
    render(<Example />)
    await userEvent.click(screen.getByRole('button', { name: 'Filter' }))
    await userEvent.click(screen.getByText('Outside'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    await userEvent.click(screen.getByRole('button', { name: 'Filter' }))
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
