import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'

export interface Project {
  id: string
  org_id: string
  name: string
  task_type: string
  modality: string
  default_ontology_id: string | null
  default_ingest_workflow_id: string | null
  created_at: string
}

export function useProjects() {
  return useQuery<Project[]>({
    queryKey: ['projects'],
    queryFn: async () => {
      const { data } = await client.get<Project[]>('/projects/')
      return data
    },
  })
}

export function useProject(id: string | undefined) {
  return useQuery<Project>({
    queryKey: ['project', id],
    queryFn: async () => {
      const { data } = await client.get<Project>(`/projects/${id}`)
      return data
    },
    enabled: !!id,
  })
}

export function useCreateProject() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string; task_type?: string; modality?: string }) => {
      const { data } = await client.post<Project>('/projects/', body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }),
  })
}

export function useUpdateProject(id: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name?: string; task_type?: string; modality?: string; default_ingest_workflow_id?: string | null; default_ontology_id?: string | null }) => {
      const { data } = await client.patch<Project>(`/projects/${id}`, body)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['projects'] })
      qc.invalidateQueries({ queryKey: ['project', id] })
    },
  })
}

export function useDeleteProject() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      await client.delete(`/projects/${id}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }),
  })
}
