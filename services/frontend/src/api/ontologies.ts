import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'

export interface Ontology {
  id: string
  org_id: string
  name: string
  version: number
  created_at: string
}

export interface LabelClass {
  id: string
  ontology_id: string
  class_key: string
  display_name: string
  color: string
  sort_order: number
}

export function useOntologies() {
  return useQuery<Ontology[]>({
    queryKey: ['ontologies'],
    queryFn: async () => {
      const { data } = await client.get<Ontology[]>('/ontologies')
      return data
    },
  })
}

export function useCreateOntology() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string }) => {
      const { data } = await client.post<Ontology>('/ontologies', body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useUpdateOntology(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string }) => {
      const { data } = await client.patch<Ontology>(`/ontologies/${ontologyId}`, body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useDeleteOntology() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (ontologyId: string) => {
      await client.delete(`/ontologies/${ontologyId}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useLabelClasses(ontologyId: string | undefined) {
  return useQuery<LabelClass[]>({
    queryKey: ['label-classes', ontologyId],
    queryFn: async () => {
      const { data } = await client.get<LabelClass[]>(`/ontologies/${ontologyId}/classes`)
      return data
    },
    enabled: !!ontologyId,
  })
}

export function useCreateLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: {
      class_key: string
      display_name: string
      color: string
      sort_order: number
    }) => {
      const { data } = await client.post<LabelClass>(`/ontologies/${ontologyId}/classes`, body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}

export function useUpdateLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      classId,
      body,
    }: {
      classId: string
      body: { display_name?: string; color?: string; sort_order?: number }
    }) => {
      const { data } = await client.patch<LabelClass>(
        `/ontologies/${ontologyId}/classes/${classId}`,
        body,
      )
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}

export function useDeleteLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (classId: string) => {
      await client.delete(`/ontologies/${ontologyId}/classes/${classId}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}
