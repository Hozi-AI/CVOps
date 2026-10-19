import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import { BackButton } from './BackButton'

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>
}

function renderOnPage() {
  render(
    <MemoryRouter initialEntries={['/previous', '/page']} initialIndex={1}>
      <BackButton to="/fallback" label="Runs" />
      <Where />
    </MemoryRouter>,
  )
}

describe('BackButton', () => {
  afterEach(() => window.history.replaceState(null, ''))

  it('steps back through history when the user navigated here inside the app', () => {
    window.history.replaceState({ idx: 1 }, '')
    renderOnPage()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/previous')
  })

  it('goes to the fallback when there is no in-app history (new tab, pasted link)', () => {
    window.history.replaceState(null, '')
    renderOnPage()

    // The label names the destination so the click is predictable.
    fireEvent.click(screen.getByRole('button', { name: 'Back to Runs' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/fallback')
  })

  it('is a real, keyboard-focusable button', () => {
    renderOnPage()
    const btn = screen.getByRole('button')
    btn.focus()
    expect(btn).toHaveFocus()
    expect(btn).toHaveAttribute('type', 'button')
  })
})
