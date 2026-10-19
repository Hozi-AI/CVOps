import { Suspense, lazy } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Layout } from './components/layout/Layout'
import { Spinner } from './components/ui'
import Projects from './pages/Projects'
import Project from './pages/Project'
import DataSources from './pages/DataSources'
import SampleBrowser from './pages/SampleBrowser'
import Datasets from './pages/Datasets'
import DatasetView from './pages/DatasetView'
import CommitDetail from './pages/CommitDetail'
import Workflows from './pages/Workflows'
import RunView from './pages/RunView'
import Runs from './pages/Runs'
import Models from './pages/Models'
import ModelDetail from './pages/ModelDetail'
import TrainingContainers from './pages/TrainingContainers'
import Ontologies from './pages/Ontologies'
import Activity from './pages/Activity'
import ProjectSettings from './pages/ProjectSettings'
import ImportDataset from './pages/ImportDataset'
import Login from './pages/Login'
import Register from './pages/Register'
import { isAuthenticated } from './api/auth'
import { authMode, isNoAuth } from './lib/authMode'

// Code-split the workflow builder: it pulls in @xyflow/react and rjsf, which
// would otherwise double the initial bundle for a route most sessions skip.
const WorkflowBuilder = lazy(() => import('./pages/WorkflowBuilder'))

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!isAuthenticated()) return <Navigate to="/login" replace />
  return <>{children}</>
}

// AUTH_MODE has no default, so a build without it must not guess a login mode.
function AuthModeMissing() {
  return (
    <div className="flex h-screen items-center justify-center bg-surface-1 p-6">
      <div className="max-w-md rounded-lg border border-border bg-surface-2 p-6 text-sm text-text-secondary">
        <h1 className="mb-2 text-base font-semibold text-text-primary">Login mode not configured</h1>
        <p>
          Set <code className="text-text-primary">AUTH_MODE</code> in <code className="text-text-primary">manifests/.env</code> to{' '}
          <code className="text-text-primary">local</code> (register/login page) or{' '}
          <code className="text-text-primary">none</code> (no login, local dev only), then restart.
        </p>
      </div>
    </div>
  )
}

function RouteFallback() {
  return (
    <div className="flex h-full items-center justify-center text-text-muted">
      <Spinner className="h-6 w-6" />
    </div>
  )
}

export default function App() {
  if (authMode === null) return <AuthModeMissing />
  return (
    <>
    <Routes>
      {/* No-login mode has no accounts: send stale /login links into the app. */}
      <Route path="/login"    element={isNoAuth ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/register" element={isNoAuth ? <Navigate to="/" replace /> : <Register />} />

      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/projects"                              element={<Projects />} />
        <Route path="/ontologies"                            element={<Ontologies />} />
        <Route path="/activity"                              element={<Activity />} />
        <Route path="/projects/:id"                          element={<Project />} />
        <Route path="/projects/:id/data-sources"             element={<DataSources />} />
        <Route path="/projects/:id/samples"                  element={<SampleBrowser />} />
        <Route path="/projects/:id/datasets"                 element={<Datasets />} />
        <Route path="/datasets/:id"                          element={<DatasetView />} />
        <Route path="/datasets/:id/commits/:cid"             element={<CommitDetail />} />
        <Route path="/projects/:id/workflows"                element={<Workflows />} />
        <Route
          path="/workflows/:id"
          element={
            <Suspense fallback={<RouteFallback />}>
              <WorkflowBuilder />
            </Suspense>
          }
        />
        <Route path="/projects/:id/runs"                     element={<Runs />} />
        <Route path="/runs/:id"                              element={<RunView />} />
        <Route path="/projects/:id/models"                   element={<Models />} />
        <Route path="/models/:id"                            element={<ModelDetail />} />
        <Route path="/projects/:id/training-containers"      element={<TrainingContainers />} />
        <Route path="/projects/:id/settings"                 element={<ProjectSettings />} />
        <Route path="/projects/:id/import"                   element={<ImportDataset />} />
      </Route>
    </Routes>
    </>
  )
}
