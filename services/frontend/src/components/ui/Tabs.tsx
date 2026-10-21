import * as RadixTabs from '@radix-ui/react-tabs'
import {
  createContext,
  forwardRef,
  useContext,
  type ComponentPropsWithoutRef,
  type ElementRef,
} from 'react'
import { cn } from '../../lib/cn'

/**
 * Tabs built on Radix (roving focus, arrow/Home/End keys, aria-selected and
 * aria-controls wiring). Two looks: `pill` is a segmented control for switching
 * views in place; `underline` sits on a border, for page- or dialog-level tabs.
 * Inactive panels unmount, like a plain conditional render. Semantic tokens only.
 */
type Variant = 'pill' | 'underline'

const VariantContext = createContext<Variant>('pill')

export const Tabs = RadixTabs.Root

const LIST: Record<Variant, string> = {
  pill: 'inline-flex rounded-lg border border-border bg-surface-2 p-0.5',
  underline: 'flex border-b border-border',
}

const TRIGGER: Record<Variant, string> = {
  pill: 'rounded-md px-3 py-1.5 text-sm font-medium text-text-muted transition-colors hover:text-text-secondary data-[state=active]:bg-iris/15 data-[state=active]:text-text-primary',
  underline:
    '-mb-px border-b-2 border-transparent px-4 py-2 text-sm font-medium text-text-muted transition-colors hover:text-text-primary data-[state=active]:border-iris-400 data-[state=active]:text-text-primary',
}

interface TabsListProps extends ComponentPropsWithoutRef<typeof RadixTabs.List> {
  variant?: Variant
}

export const TabsList = forwardRef<ElementRef<typeof RadixTabs.List>, TabsListProps>(
  function TabsList({ variant = 'pill', className, ...rest }, ref) {
    return (
      <VariantContext.Provider value={variant}>
        <RadixTabs.List ref={ref} className={cn(LIST[variant], className)} {...rest} />
      </VariantContext.Provider>
    )
  },
)

export const TabsTrigger = forwardRef<
  ElementRef<typeof RadixTabs.Trigger>,
  ComponentPropsWithoutRef<typeof RadixTabs.Trigger>
>(function TabsTrigger({ className, ...rest }, ref) {
  const variant = useContext(VariantContext)
  return (
    <RadixTabs.Trigger
      ref={ref}
      className={cn(TRIGGER[variant], 'disabled:cursor-not-allowed disabled:opacity-50', className)}
      {...rest}
    />
  )
})

/** A tab's panel. Radix makes it focusable so keyboard users can Tab into it. */
export const TabsContent = RadixTabs.Content
