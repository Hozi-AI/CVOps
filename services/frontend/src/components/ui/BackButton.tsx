import { useNavigate } from 'react-router-dom'
import { cn } from '../../lib/cn'

/** True when the user reached this page from inside the app (react-router stores `idx` in history.state). */
function hasInAppHistory(): boolean {
  const idx = (window.history.state as { idx?: number } | null)?.idx
  return typeof idx === 'number' && idx > 0
}

export interface BackButtonProps {
  /** Where to go when there is no in-app history (new tab, pasted link). */
  to: string
  /** Human name of `to`, used in the accessible label ("Back to Runs"). */
  label: string
  className?: string
}

/**
 * Icon-only back button for drill-down pages. Steps back through history when the
 * user navigated here inside the app, and falls back to `to` otherwise.
 */
export function BackButton({ to, label, className }: BackButtonProps) {
  const navigate = useNavigate()
  const inApp = hasInAppHistory()
  const name = inApp ? 'Back' : `Back to ${label}`

  return (
    <button
      type="button"
      aria-label={name}
      title={name}
      onClick={() => (hasInAppHistory() ? navigate(-1) : navigate(to))}
      className={cn(
        'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-text-secondary transition-colors hover:bg-surface-3 hover:text-text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-focus',
        className,
      )}
    >
      <svg
        className="h-4 w-4"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M19 12H5M12 19l-7-7 7-7" />
      </svg>
    </button>
  )
}
