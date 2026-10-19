// How users log in, baked in at build time from VITE_AUTH_MODE (Tilt and the
// compose build set it from AUTH_MODE in manifests/.env). There is no default:
// an unset or unknown value yields null, and the app shows a configuration
// error instead of guessing.
export type AuthMode = 'local' | 'none'

export function parseAuthMode(raw: string | undefined): AuthMode | null {
  return raw === 'local' || raw === 'none' ? raw : null
}

export const authMode = parseAuthMode(import.meta.env.VITE_AUTH_MODE)

/** AUTH_MODE=none: no login page and no logout; the API is the dev user. */
export const isNoAuth = authMode === 'none'
