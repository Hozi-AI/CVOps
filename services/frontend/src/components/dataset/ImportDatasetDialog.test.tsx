import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { API } from '../../test/handlers'
import { server } from '../../test/server'
import { renderWithProviders } from '../../test/utils'
import { ImportDatasetDialog } from './ImportDatasetDialog'

describe('ImportDatasetDialog source tabs', () => {
  it('switches between upload and server path as real tabs', async () => {
    server.use(http.get(`${API}/ontologies`, () => HttpResponse.json([])))
    renderWithProviders(<ImportDatasetDialog projectId="p1" open onClose={() => {}} />)

    expect(screen.getByRole('tablist', { name: 'Import source' })).toBeInTheDocument()
    const upload = screen.getByRole('tab', { name: 'Upload' })
    const serverPath = screen.getByRole('tab', { name: 'Server folder path' })
    expect(upload).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Select a zip file or a local folder')
    expect(screen.queryByLabelText('Server-side folder path')).toBeNull()

    // Keyboard: arrow onto the second tab and its panel follows.
    upload.focus()
    await userEvent.keyboard('{ArrowRight}')
    expect(serverPath).toHaveFocus()
    expect(serverPath).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Server-side folder path')).toBeInTheDocument()

    // Tab triggers are type="button", so switching never submits the form.
    expect(serverPath).toHaveAttribute('type', 'button')
  })
})
