// Edge auth (VITE_AUTH_MODE=proxy): nginx + oauth2-proxy own the login session
// via a same-origin cookie, so the SPA holds no tokens. See docs/edge-auth.md.
export const isEdgeAuth = import.meta.env.VITE_AUTH_MODE === 'proxy'

function go(path: string): Promise<never> {
  window.location.href = path
  // Never settles: the page is unloading, so callers must not carry on
  // (e.g. navigate to /login) in the meantime.
  return new Promise<never>(() => {})
}

/** Session gone (401): restart the login flow, coming back to this page. */
export function startEdgeLogin(): Promise<never> {
  const here = window.location.pathname + window.location.search
  return go(`/oauth2/start?rd=${encodeURIComponent(here)}`)
}

/** Clears the oauth2-proxy cookie and (via its backend logout) the IdP session. */
export function edgeSignOut(): Promise<never> {
  return go('/oauth2/sign_out?rd=%2F')
}
