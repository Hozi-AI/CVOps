import { useEffect, useState } from 'react'

interface Props {
  blobUrl: string
  annotations?: Array<{ char_start: number; char_end: number; class_key: string }>
}

export function SampleViewerText({ blobUrl, annotations = [] }: Props) {
  const [text, setText] = useState<string | null>(null)

  useEffect(() => {
    fetch(blobUrl)
      .then((r) => r.text())
      .then(setText)
  }, [blobUrl])

  if (text === null) {
    return <div className="text-text-muted text-sm">Loading…</div>
  }

  if (annotations.length === 0) {
    return (
      <pre className="whitespace-pre-wrap rounded bg-surface-2 p-3 font-mono text-sm text-text-primary">
        {text}
      </pre>
    )
  }

  const spans: React.ReactNode[] = []
  let cursor = 0
  const sorted = [...annotations].sort((a, b) => a.char_start - b.char_start)
  for (const ann of sorted) {
    if (ann.char_start > cursor) spans.push(text.slice(cursor, ann.char_start))
    spans.push(
      <mark
        key={`${ann.char_start}-${ann.char_end}`}
        className="rounded bg-iris-500/30 px-0.5 text-text-primary"
        title={ann.class_key}
      >
        {text.slice(ann.char_start, ann.char_end)}
      </mark>,
    )
    cursor = ann.char_end
  }
  if (cursor < text.length) spans.push(text.slice(cursor))

  return (
    <pre className="whitespace-pre-wrap rounded bg-surface-2 p-3 font-mono text-sm text-text-primary">
      {spans}
    </pre>
  )
}
