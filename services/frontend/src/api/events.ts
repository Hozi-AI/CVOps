import { useInfiniteQuery } from '@tanstack/react-query'
import { client } from '../lib/client'
import type { CursorPage } from './samples'

export interface ActivityEventOut {
  id: string
  created_at: string
  actor_id: string | null
  actor_type: string | null
  actor_email: string | null
  entity_type: string
  entity_id: string
  action: string
  payload: Record<string, unknown> | null
}

export interface EventFilters {
  entity_type?: string
  action?: string
}

export function useEvents(filters: EventFilters = {}) {
  return useInfiniteQuery<CursorPage<ActivityEventOut>>({
    queryKey: ['events', filters],
    queryFn: async ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '50' })
      if (pageParam) params.set('cursor', pageParam as string)
      if (filters.entity_type) params.set('entity_type', filters.entity_type)
      if (filters.action) params.set('action', filters.action)
      const { data } = await client.get<CursorPage<ActivityEventOut>>(`/events?${params}`)
      return data
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}
