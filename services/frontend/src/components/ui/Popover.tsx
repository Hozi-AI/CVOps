import * as RadixPopover from '@radix-ui/react-popover'
import { type ReactElement, type ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface PopoverProps {
  /** One focusable element that toggles the panel, e.g. a `Button`. */
  trigger: ReactElement
  /** Accessible name for the panel (it is announced as a dialog). */
  label: string
  children: ReactNode
  side?: 'top' | 'right' | 'bottom' | 'left'
  align?: 'start' | 'center' | 'end'
  /** Controlled mode; omit both to let the popover manage itself. */
  open?: boolean
  onOpenChange?: (open: boolean) => void
  className?: string
}

/**
 * A floating panel anchored to its trigger, on Radix: moves focus into the
 * panel on open, closes on outside click and Escape (returning focus to the
 * trigger), and flips at viewport edges. Use it for small forms and info
 * panels; `Menu` is for a list of actions and `Dialog` for anything modal.
 */
export function Popover({
  trigger,
  label,
  children,
  side = 'bottom',
  align = 'start',
  open,
  onOpenChange,
  className,
}: PopoverProps) {
  return (
    <RadixPopover.Root open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          aria-label={label}
          side={side}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            'z-50 w-72 rounded-lg border border-border bg-surface-3 p-4 text-sm text-text-primary shadow-lg focus:outline-none',
            className,
          )}
        >
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  )
}

/** Closes the surrounding popover; wrap a single button with it (`asChild`). */
export const PopoverClose = RadixPopover.Close
