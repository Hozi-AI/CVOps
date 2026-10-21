import * as RadixTooltip from '@radix-ui/react-tooltip'
import { type ReactElement, type ReactNode } from 'react'

interface TooltipProps {
  /** Text (or small inline content) shown in the bubble. */
  content: ReactNode
  /** One focusable element, e.g. an icon-only button. It keeps its own aria-label. */
  children: ReactElement
  side?: 'top' | 'right' | 'bottom' | 'left'
  align?: 'start' | 'center' | 'end'
  /** Hover delay in ms before the bubble appears. Keyboard focus shows it at once. */
  delayDuration?: number
}

/**
 * Themed tooltip on Radix: opens on hover and keyboard focus, closes on Escape,
 * flips at viewport edges, and links the bubble to its trigger with
 * aria-describedby. For supplementary hints only — an icon-only button still
 * needs its own `aria-label`. Each tooltip carries its own provider, so no
 * app-level wrapper is needed.
 */
export function Tooltip({
  content,
  children,
  side = 'top',
  align = 'center',
  delayDuration = 300,
}: TooltipProps) {
  return (
    <RadixTooltip.Provider delayDuration={delayDuration}>
      <RadixTooltip.Root>
        <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
        <RadixTooltip.Portal>
          <RadixTooltip.Content
            side={side}
            align={align}
            sideOffset={6}
            collisionPadding={8}
            className="z-50 max-w-xs rounded-md border border-border bg-surface-3 px-2 py-1 text-xs text-text-primary shadow-md"
          >
            {content}
          </RadixTooltip.Content>
        </RadixTooltip.Portal>
      </RadixTooltip.Root>
    </RadixTooltip.Provider>
  )
}
