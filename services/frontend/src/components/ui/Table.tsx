import {
  forwardRef,
  type HTMLAttributes,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from 'react'
import { cn } from '../../lib/cn'

/**
 * Semantic table primitives: real `<table>` markup (so screen readers get row
 * and column headers) styled with tokens. The wrapper scrolls horizontally on
 * narrow screens instead of squashing columns. Mark number columns `numeric`
 * to right-align them with tabular figures.
 */
export const Table = forwardRef<HTMLTableElement, HTMLAttributes<HTMLTableElement>>(
  function Table({ className, ...rest }, ref) {
    return (
      <div className="w-full overflow-x-auto rounded-xl border border-border bg-surface-2">
        <table ref={ref} className={cn('w-full border-collapse text-sm', className)} {...rest} />
      </div>
    )
  },
)

export function TableCaption({ className, ...rest }: HTMLAttributes<HTMLTableCaptionElement>) {
  return (
    <caption className={cn('px-4 py-2 text-left text-xs text-text-muted', className)} {...rest} />
  )
}

export function TableHeader({ className, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn('border-b border-border bg-surface-1', className)} {...rest} />
}

export function TableBody({ className, ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn('[&>tr:last-child]:border-0', className)} {...rest} />
}

export function TableRow({ className, ...rest }: HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={cn('border-b border-border transition-colors hover:bg-surface-3', className)}
      {...rest}
    />
  )
}

interface CellAlign {
  /** Right-align with tabular figures, for numbers and metrics. */
  numeric?: boolean
}

export function TableHead({
  className,
  numeric,
  scope = 'col',
  ...rest
}: ThHTMLAttributes<HTMLTableCellElement> & CellAlign) {
  return (
    <th
      scope={scope}
      className={cn(
        'px-4 py-2 text-xs font-medium uppercase tracking-wide text-text-muted',
        numeric ? 'text-right' : 'text-left',
        className,
      )}
      {...rest}
    />
  )
}

export function TableCell({
  className,
  numeric,
  ...rest
}: TdHTMLAttributes<HTMLTableCellElement> & CellAlign) {
  return (
    <td
      className={cn(
        'px-4 py-2 text-text-secondary',
        numeric && 'text-right font-mono tabular-nums text-text-primary',
        className,
      )}
      {...rest}
    />
  )
}
