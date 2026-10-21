import { webcrypto } from 'node:crypto'
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from './server'

// jsdom does not implement crypto.subtle, and whether the Node WebCrypto global
// survives into each jsdom worker is non-deterministic. Pin it so the
// secure-context hash path (lib/hash.ts) is the one tests exercise everywhere,
// instead of falling back to file.stream() which jsdom also lacks.
if (typeof globalThis.crypto?.subtle === 'undefined') {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true })
}

// Node's undici `fetch` reads a Blob/File request body by calling `.stream()`,
// which jsdom's Blob does not implement — so `fetch(url, { body: file })`
// (the image-upload PUT) throws "object.stream is not a function" under test.
// Real browsers implement Blob.stream(); this only patches the jsdom gap.
if (typeof Blob.prototype.stream !== 'function') {
  Blob.prototype.stream = function (this: Blob): ReadableStream<Uint8Array<ArrayBuffer>> {
    const bytes = this.arrayBuffer()
    return new ReadableStream<Uint8Array<ArrayBuffer>>({
      async start(controller) {
        controller.enqueue(new Uint8Array(await bytes))
        controller.close()
      },
    })
  }
}

// jsdom 29 does not always expose localStorage as a usable global — under an
// opaque origin it surfaces as an empty `{}` (defined, but `getItem`/`clear`
// are not functions) rather than undefined. Provide a minimal in-memory Storage
// whenever the global is missing OR non-functional, so app code
// (lib/client.ts, api/auth.ts) and tests can use `localStorage` unconditionally.
if (typeof globalThis.localStorage?.getItem !== 'function') {
  class MemoryStorage implements Storage {
    private store = new Map<string, string>()
    get length() {
      return this.store.size
    }
    clear() {
      this.store.clear()
    }
    getItem(key: string) {
      return this.store.has(key) ? this.store.get(key)! : null
    }
    key(i: number) {
      return Array.from(this.store.keys())[i] ?? null
    }
    removeItem(key: string) {
      this.store.delete(key)
    }
    setItem(key: string, value: string) {
      this.store.set(key, String(value))
    }
  }
  const mem = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: mem, configurable: true })
  if (typeof window !== 'undefined') {
    Object.defineProperty(window, 'localStorage', { value: mem, configurable: true })
  }
}

// MSW lifecycle. onUnhandledRequest 'error' forces every test to declare the
// network it depends on — an unmocked call fails loudly rather than hitting a
// real host.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))

afterEach(() => {
  server.resetHandlers()
  cleanup()
  localStorage.clear()
})

afterAll(() => server.close())

// jsdom lacks matchMedia; some UI components probe it. Provide a no-op stub.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList
}

// jsdom lacks ResizeObserver; Radix's floating primitives (Tooltip, Popover)
// and Recharts' ResponsiveContainer construct one. A no-op stub is enough —
// nothing in jsdom has a layout to observe.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
