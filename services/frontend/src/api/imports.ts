import { useMutation } from '@tanstack/react-query'
import { client } from '../lib/client'
import { sha256Hex } from '../lib/hash'

export interface ImportRequest {
  blob_hash?: string
  folder_path?: string
  format?: string
  ontology_id?: string
  dataset_name?: string
  commit_message?: string
  review?: boolean
  split_strategy?: { train_ratio?: number; val_ratio?: number }
}

export interface ImportRun {
  id: string
  status: string
  kind: string
  created_at: string
}

/** Fetch a presigned PUT URL for uploading a zip dataset. */
async function getImportUploadUrl(projectId: string, blobHash: string): Promise<string> {
  const { data } = await client.post<{ upload_url: string }>(
    `/projects/${projectId}/imports/upload-url`,
    { blob_hash: blobHash },
  )
  return data.upload_url
}

/**
 * Upload a zip file, then dispatch the import_dataset → commit_dataset run.
 *
 * Hashes the file client-side first, fetches a presigned PUT URL, PUTs the
 * bytes directly to storage, then calls POST /imports with the hash and config.
 */
export function useImportDataset(projectId: string | undefined) {
  return useMutation({
    mutationFn: async (vars: {
      file: File
      ontologyId: string
      datasetName?: string
      format?: string
      review?: boolean
      splitStrategy?: { train_ratio?: number; val_ratio?: number }
    }): Promise<ImportRun> => {
      if (!projectId) throw new Error('projectId is required')

      const hex = await sha256Hex(vars.file)
      const blobHash = `sha256:${hex}`

      const putUrl = await getImportUploadUrl(projectId, blobHash)
      const put = await fetch(putUrl, { method: 'PUT', body: vars.file })
      if (!put.ok) throw new Error(`Upload failed: ${put.status}`)

      const { data } = await client.post<ImportRun>(`/projects/${projectId}/imports`, {
        blob_hash: blobHash,
        ontology_id: vars.ontologyId,
        dataset_name: vars.datasetName ?? 'Imported Dataset',
        format: vars.format ?? 'auto',
        review: vars.review ?? false,
        split_strategy: vars.splitStrategy,
      } satisfies ImportRequest)
      return data
    },
  })
}

/** Dispatch an import from a server-side folder path (no file upload). */
export function useImportFromFolder(projectId: string | undefined) {
  return useMutation({
    mutationFn: async (vars: {
      folderPath: string
      ontologyId: string
      datasetName?: string
      commitMessage?: string
      format?: string
      review?: boolean
      splitStrategy?: { train_ratio?: number; val_ratio?: number }
    }): Promise<ImportRun> => {
      if (!projectId) throw new Error('projectId is required')
      const { data } = await client.post<ImportRun>(`/projects/${projectId}/imports`, {
        folder_path: vars.folderPath,
        ontology_id: vars.ontologyId,
        dataset_name: vars.datasetName ?? 'Imported Dataset',
        commit_message: vars.commitMessage,
        format: vars.format ?? 'auto',
        review: vars.review ?? false,
        split_strategy: vars.splitStrategy,
      } satisfies ImportRequest)
      return data
    },
  })
}
