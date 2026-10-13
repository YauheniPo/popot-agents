import type { KnowledgeRecord } from './linkedKnowledge';
import { safeSourceUrl } from './linkedKnowledge';

function readableText(text: string): string {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}

export default function KnowledgePanel({ records }: { records: KnowledgeRecord[] }) {
  return (
    <section aria-label="Saved knowledge" className="mt-4 border-t border-[#2A3652] pt-3">
      <h3 className="font-semibold text-[#43D9E8]">Saved knowledge · {records.length}</h3>
      <p className="mt-1 text-xs text-[#7E8CA6]">Source excerpts linked to this entity in the selected brain.</p>
      {records.length === 0 && <p className="mt-2 text-xs">No linked text fragments in the loaded graph.</p>}
      <div className="mt-2 flex flex-col gap-3">
        {records.map(record => {
          const sourceUrl = safeSourceUrl(record.sourceUri);
          return (
            <article key={record.id} className="rounded-lg border border-[#2A3652] p-3">
              <div className="break-words text-xs font-semibold">{record.documentName || 'Source document'}</div>
              {(record.recordedAt || record.createdAt) && <div className="mt-1 text-xs text-[#7E8CA6]">
                {record.recordedAt ? 'Recorded: ' : 'Ingested: '}{record.recordedAt || record.createdAt}
              </div>}
              {record.sourceUri && <div className="mt-1 break-all text-xs">
                Source: {sourceUrl ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="text-[#43D9E8] underline">{record.sourceUri}</a> : <span>{record.sourceUri}</span>}
              </div>}
              <pre className="mt-2 whitespace-pre-wrap break-words font-sans text-xs leading-relaxed">{record.text ? readableText(record.text) : 'Text is unavailable in the loaded graph.'}</pre>
              {record.summaries.length > 0 && <details className="mt-2 text-xs text-[#7E8CA6]">
                <summary className="cursor-pointer">Extracted summaries · {record.summaries.length}</summary>
                <p className="mt-1">Automatically extracted; verify against the source excerpt above.</p>
                {record.summaries.map((text, i) => <p key={i} className="mt-2 whitespace-pre-wrap break-words">{text}</p>)}
              </details>}
              <details className="mt-2 text-xs text-[#7E8CA6]">
                <summary className="cursor-pointer">Graph provenance</summary>
                <p className="mt-1 break-all">Fragment: {record.id}</p>
                {record.documentId && <p className="break-all">Document: {record.documentId}</p>}
              </details>
            </article>
          );
        })}
      </div>
    </section>
  );
}
