import axios from 'axios'
import { isEdgeAuth, startEdgeLogin } from './edgeAuth'

export const client = axios.create({ baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1' })

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

let refreshing = false
const waitQueue: Array<(token: string | null) => void> = []

client.interceptors.response.use(
  (res) => res,
  async (err) => {
    const orig = err.config
    if (err.response?.status !== 401 || orig._retry) return Promise.reject(err)
    // Edge auth has no refresh token: the session lives in oauth2-proxy's cookie.
    if (isEdgeAuth) return startEdgeLogin()
    orig._retry = true

    if (refreshing) {
      return new Promise((resolve, reject) => {
        waitQueue.push((token) => {
          if (!token) return reject(err)
          orig.headers.Authorization = `Bearer ${token}`
          resolve(client(orig))
        })
      })
    }

    refreshing = true
    try {
      const rt = localStorage.getItem('refresh_token')
      if (!rt) throw new Error('no refresh token')
      const { data } = await axios.post(
        `${import.meta.env.VITE_API_BASE_URL ?? '/api/v1'}/auth/refresh`,
        { refresh_token: rt },
      )
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('refresh_token', data.refresh_token)
      waitQueue.splice(0).forEach(fn => fn(data.access_token))
      orig.headers.Authorization = `Bearer ${data.access_token}`
      return client(orig)
    } catch {
      waitQueue.splice(0).forEach(fn => fn(null))
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      window.location.href = '/login'
      return Promise.reject(err)
    } finally {
      refreshing = false
    }
  },
)
