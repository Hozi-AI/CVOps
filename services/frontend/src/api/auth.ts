import { useQuery } from '@tanstack/react-query'
import { client } from '../lib/client'
import { edgeSignOut, isEdgeAuth } from '../lib/edgeAuth'

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface UserOut {
  id: string
  email: string
  org_id: string
}

export function saveTokens(tokens: TokenResponse) {
  localStorage.setItem('access_token', tokens.access_token)
  localStorage.setItem('refresh_token', tokens.refresh_token)
}

export function clearTokens() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
}

// VITE_AUTH_MODE=none: no login at all; the API treats every request as the
// single dev user (local dev only, see AUTH_MODE=none in the API config).
export const isNoAuth = import.meta.env.VITE_AUTH_MODE === 'none'

export function isAuthenticated() {
  // Under edge auth nginx only serves the app to a logged-in session.
  if (isEdgeAuth || isNoAuth) return true
  return !!localStorage.getItem('access_token')
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username: email, password })
  const { data } = await client.post<TokenResponse>('/auth/token', body, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
  return data
}

export async function register(
  email: string,
  password: string,
  org_name?: string,
): Promise<TokenResponse> {
  const { data } = await client.post<TokenResponse>('/auth/register', { email, password, org_name })
  return data
}

export async function logout() {
  if (isEdgeAuth) return edgeSignOut()
  const refresh_token = localStorage.getItem('refresh_token')
  try {
    await client.post('/auth/revoke', { refresh_token })
  } finally {
    clearTokens()
  }
}

export function useMe() {
  return useQuery<UserOut>({
    queryKey: ['me'],
    queryFn: async () => {
      const { data } = await client.get<UserOut>('/auth/me')
      return data
    },
    enabled: isAuthenticated(),
    staleTime: 5 * 60 * 1000,
  })
}
