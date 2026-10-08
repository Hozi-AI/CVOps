import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { API } from '../test/handlers'
import { server } from '../test/server'
import { renderWithProviders } from '../test/utils'
import ModelDetail from './ModelDetail'

const COMMIT_ID = '3b6632ce-5552-4912-9e81-20ca4f0c9115'
const DATASET_ID = '47e5564c-99e3-4213-a213-dc299029c432'

function mockModel(overrides: Record<string, unknown>) {
  server.use(
    http.get(`${API}/models/m1`, () =>
      HttpResponse.json({
        id: 'm1',
        project_id: 'p1',
        blob_hash: 'sha256:x',
        name: 'demo model',
        description: null,
        trained_on_commit_id: COMMIT_ID,
        trained_on_dataset_id: DATASET_ID,
        base_model: 'yolov8n',
        hyperparams: null,
        metrics: null,
        code_version: null,
        mlflow_run_id: null,
        created_at: '2026-01-01T00:00:00Z',
        ...overrides,
      }),
    ),
    http.get(`${API}/models/m1/weights-url`, () => HttpResponse.json({ url: 'https://signed.example/w' })),
    http.get(`${API}/models/m1/artifacts`, () => HttpResponse.json([])),
  )
}

function renderModelDetail() {
  return renderWithProviders(
    <Routes>
      <Route path="/models/:id" element={<ModelDetail />} />
    </Routes>,
    { route: '/models/m1' },
  )
}

describe('ModelDetail trained-on commit', () => {
  it('links to the commit page under its dataset', async () => {
    mockModel({})
    renderModelDetail()

    const link = await screen.findByRole('link', { name: /3b6632ce/ })
    expect(link).toHaveAttribute('href', `/datasets/${DATASET_ID}/commits/${COMMIT_ID}`)
  })

  it('shows the short commit id as plain text when the dataset is unknown', async () => {
    mockModel({ trained_on_dataset_id: null })
    renderModelDetail()

    expect(await screen.findByText('3b6632ce')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /3b6632ce/ })).toBeNull()
  })

  it('shows a dash when the model has no commit', async () => {
    mockModel({ trained_on_commit_id: null, trained_on_dataset_id: null })
    renderModelDetail()

    expect(await screen.findByText('Trained on commit')).toBeInTheDocument()
    expect(screen.queryByText('3b6632ce')).toBeNull()
  })
})
