import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useMatch } from 'react-router-dom'
import { useProjects } from '../api/projects'
import { useUIStore } from '../store/ui'
import { useTheme } from '../lib/theme'
import { logout } from '../api/auth'
import { isNoAuth } from '../lib/authMode'
import { queryClient } from '../lib/queryClient'
import { cn } from '../lib/cn'

interface Command {
  id: string
  label: string
  group: string
  hint?: string
  run: () => void
}

const PROJECT_SECTIONS = [
  { label: 'Data Sources', path: 'data-sources' },
  { label: 'Samples', path: 'samples' },
  { label: 'Datasets', path: 'datasets' },
  { label: 'Workflows', path: 'workflows' },
  { label: 'Models', path: 'models' },
  { label: 'Training', path: 'training-containers' },
  { label: 'Settings', path: 'settings' },
]

export function CommandPalette() {
  const open = useUIStore((s) => s.commandOpen)
  const setOpen = useUIStore((s) => s.setCommandOpen)
  const navigate = useNavigate()
  const { toggle } = useTheme()
  const { data: projects } = useProjects()

  const matchDeep = useMatch('/projects/:id/*')
  const matchExact = useMatch('/projects/:id')
  const projectId = (matchDeep ?? matchExact)?.params.id

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  // Global ⌘K / Ctrl+K toggle.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen(!useUIStore.getState().commandOpen)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setOpen])

  // Reset transient state each time the palette opens.
  useEffect(() => {
    if (open) {
      setQuery('')
      setSelected(0)
      // Focus after the portal paints.
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  const close = () => setOpen(false)

  const commands = useMemo<Command[]>(() => {
    const cmds: Command[] = [
      { id: 'nav-projects', label: 'Go to Projects', group: 'Navigate', run: () => navigate('/projects') },
    ]

    if (projectId) {
      for (const s of PROJECT_SECTIONS) {
        cmds.push({
          id: `section-${s.path}`,
          label: `Project · ${s.label}`,
          group: 'This project',
          run: () => navigate(`/projects/${projectId}/${s.path}`),
        })
      }
    }

    for (const p of projects ?? []) {
      cmds.push({
        id: `project-${p.id}`,
        label: p.name,
        hint: p.task_type,
        group: 'Projects',
        run: () => navigate(`/projects/${p.id}`),
      })
    }

    cmds.push({ id: 'action-theme', label: 'Toggle theme', group: 'Actions', run: () => toggle() })
    // No-login mode has no session to sign out of.
    if (!isNoAuth) {
      cmds.push({
        id: 'action-logout',
        label: 'Sign out',
        group: 'Actions',
        run: async () => {
          await logout()
          queryClient.clear()
          navigate('/login', { replace: true })
        },
      })
    }
    return cmds
  }, [projects, projectId, navigate, toggle])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q),
    )
  }, [commands, query])

  // Keep the highlighted index in range as the filtered list shrinks.
  useEffect(() => {
    setSelected((i) => Math.min(i, Math.max(0, filtered.length - 1)))
  }, [filtered.length])

  if (!open) return null

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      close()
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSelected((i) => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSelected((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const cmd = filtered[selected]
      if (cmd) {
        close()
        cmd.run()
      }
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center p-4 pt-[12vh]">
      <div className="absolute inset-0 bg-black/50" onClick={close} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="relative z-10 w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface-3 shadow-lg"
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search projects and actions…"
          className="w-full border-b border-border bg-transparent px-4 py-3 text-sm text-text-primary placeholder:text-text-muted focus:outline-none"
        />
        <ul className="max-h-80 overflow-auto py-1">
          {filtered.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-text-muted">No matches</li>
          )}
          {filtered.map((cmd, i) => (
            <li key={cmd.id}>
              <button
                type="button"
                onMouseEnter={() => setSelected(i)}
                onClick={() => {
                  close()
                  cmd.run()
                }}
                className={cn(
                  'flex w-full items-center justify-between px-4 py-2 text-left text-sm',
                  i === selected ? 'bg-iris/15 text-text-primary' : 'text-text-secondary',
                )}
              >
                <span className="truncate">{cmd.label}</span>
                <span className="ml-3 flex-shrink-0 text-xs text-text-muted">{cmd.hint ?? cmd.group}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>,
    document.body,
  )
}
