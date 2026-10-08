import { useNavigate } from 'react-router-dom'
import { useTheme, type ThemeMode } from '../../lib/theme'
import { isNoAuth, logout, useMe } from '../../api/auth'
import { useProject } from '../../api/projects'
import { useActiveProjectId } from '../../lib/useActiveProject'
import { useUIStore } from '../../store/ui'
import { queryClient } from '../../lib/queryClient'
import { Menu, MenuItem } from '../ui'

function getSection(pathname: string): string {
  if (pathname === '/projects')                   return 'Projects'
  if (pathname.endsWith('/data-sources'))         return 'Data Sources'
  if (pathname.endsWith('/samples'))              return 'Samples'
  if (pathname.endsWith('/datasets'))             return 'Datasets'
  if (pathname.endsWith('/workflows'))            return 'Workflows'
  if (pathname.endsWith('/runs'))                 return 'Runs'
  if (pathname.endsWith('/models'))               return 'Models'
  if (pathname.endsWith('/training-containers'))  return 'Training Environments'
  if (pathname.endsWith('/settings'))             return 'Settings'
  if (pathname.includes('/commits/'))             return 'Commit'
  if (pathname.startsWith('/datasets/'))          return 'Dataset'
  if (pathname.startsWith('/workflows/'))         return 'Workflow Builder'
  if (pathname.startsWith('/runs/'))              return 'Run'
  if (pathname.startsWith('/models/'))            return 'Model'
  if (/^\/projects\/[^/]+$/.test(pathname))       return 'Dashboard'
  return 'CVOps'
}

const themeLabel: Record<ThemeMode, string> = {
  light:  'Switch to dark theme',
  dark:   'Switch to system theme',
  system: 'Switch to light theme',
}

export function Header() {
  const navigate = useNavigate()
  const { mode, toggle } = useTheme()
  const { data: me } = useMe()
  const setCommandOpen = useUIStore((s) => s.setCommandOpen)
  const projectId = useActiveProjectId()
  const { data: project } = useProject(projectId)
  const section = getSection(location.pathname)
  const initial = me?.email?.[0]?.toUpperCase() ?? 'U'

  async function handleLogout() {
    await logout()
    queryClient.clear()
    navigate('/login', { replace: true })
  }

  return (
    <header className="h-14 border-b border-border bg-surface-2 flex items-center px-6 flex-shrink-0 gap-3">
      <h1 className="flex-1 truncate text-base font-semibold text-text-primary">
        {project && section !== 'Projects' ? (
          <>
            <span className="text-text-muted">{project.name}</span>
            <span className="mx-2 text-text-muted" aria-hidden>/</span>
            <span>{section}</span>
          </>
        ) : (
          section
        )}
      </h1>

      <button
        type="button"
        onClick={() => setCommandOpen(true)}
        aria-label="Open command palette"
        className="ml-auto flex h-8 items-center gap-2 rounded-lg border border-border-strong px-3 text-xs text-text-muted transition-colors hover:bg-surface-1 hover:text-text-secondary flex-shrink-0"
      >
        <span>Search…</span>
        <kbd className="rounded border border-border bg-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-text-muted">⌘K</kbd>
      </button>

      <button
        type="button"
        onClick={toggle}
        aria-label={themeLabel[mode]}
        title={themeLabel[mode]}
        className="h-8 w-8 rounded-lg text-text-secondary hover:bg-surface-1 hover:text-text-primary transition-colors flex items-center justify-center flex-shrink-0"
      >
        <ThemeIcon mode={mode} />
      </button>

      <Menu
        align="right"
        triggerLabel="Account menu"
        triggerContent={initial}
        triggerClassName="w-8 h-8 rounded-full bg-iris flex items-center justify-center text-text-onAccent text-sm font-bold flex-shrink-0 hover:opacity-80 transition-opacity"
      >
        <div className="px-3 py-2">
          <p className="truncate text-sm font-medium text-text-primary">{me?.email ?? 'Signed in'}</p>
          {me?.org_id && (
            <p className="truncate text-xs text-text-muted">Org · {me.org_id.slice(0, 8)}</p>
          )}
        </div>
        {!isNoAuth && (
          <>
            <div className="my-1 border-t border-border" />
            <MenuItem onClick={handleLogout}>Log out</MenuItem>
          </>
        )}
      </Menu>
    </header>
  )
}

function ThemeIcon({ mode }: { mode: ThemeMode }) {
  // Lucide-styled SVGs (1.5px stroke). We'll switch to lucide-react in step 2.
  if (mode === 'dark') {
    // moon
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"
           aria-hidden="true">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/>
      </svg>
    )
  }
  if (mode === 'light') {
    // sun
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"
           aria-hidden="true">
        <circle cx="12" cy="12" r="4"/>
        <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
      </svg>
    )
  }
  // monitor (system)
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true">
      <rect x="2" y="3" width="20" height="14" rx="2"/>
      <path d="M8 21h8M12 17v4"/>
    </svg>
  )
}
