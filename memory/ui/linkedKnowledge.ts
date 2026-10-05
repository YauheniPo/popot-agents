// Operates on ONE content-layer payload, never the merged governance graph.
// Only provenance edges are followed; semantic neighbors are not evidence.
interface GraphNode {
  id: string;
  stage?: string;
  name?: string;
  [key: string]: unknown;
}
interface GraphLink { source: string; target: string; [key: string]: unknown }
interface GraphPayload { nodes: GraphNode[]; links: GraphLink[] }
export interface KnowledgeRecord {
  id: string;
  text: string;
  documentId: string;
  documentName: string;
  sourceUri: string;
  createdAt: string;
  recordedAt: string;
  summaries: string[];
}

const string = (value: unknown): string => typeof value === 'string' ? value : '';
function parsedObject(value: unknown): Record<string, unknown> {
  try {
    const result = typeof value === 'string' ? JSON.parse(value) : value;
    return result && typeof result === 'object' && !Array.isArray(result) ? result : {};
  } catch { return {}; }
}
function sourceUri(node?: GraphNode): string {
  const metadata = parsedObject(node?.external_metadata);
  return string(parsedObject(metadata._cognee).source_uri);
}
export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function linkedKnowledge(entityId: string, graph?: GraphPayload | null): KnowledgeRecord[] {
  if (!graph || !graph.nodes.some(node => node.id === entityId)) return [];
  const byId = new Map(graph.nodes.map(node => [node.id, node]));
  const neighbors = (id: string): GraphNode[] => {
    const ids = new Set<string>();
    for (const link of graph.links) {
      if (link.source === id) ids.add(link.target);
      if (link.target === id) ids.add(link.source);
    }
    return [...ids].map(key => byId.get(key)).filter((node): node is GraphNode => Boolean(node));
  };
  const direct = neighbors(entityId);
  const documents = new Set(direct.filter(node => node.stage === 'document').map(node => node.id));
  const chunks = new Map(direct.filter(node => node.stage === 'chunk').map(node => [node.id, node]));
  // Some ingestors attach entities directly to a document, rather than a chunk.
  for (const documentId of documents) {
    for (const node of neighbors(documentId)) if (node.stage === 'chunk') chunks.set(node.id, node);
    for (const node of graph.nodes) if (node.stage === 'chunk' && node.document_id === documentId) chunks.set(node.id, node);
  }
  return [...chunks.values()].map(chunk => {
    const nearby = neighbors(chunk.id);
    const document = byId.get(string(chunk.document_id)) ?? nearby.find(node => node.stage === 'document');
    const text = string(chunk.text);
    const timestamp = typeof chunk.t_created === 'number' ? new Date(chunk.t_created) : null;
    const summaries = graph.nodes.filter(node => node.stage === 'summary' &&
      (node.source_chunk_id === chunk.id || nearby.some(other => other.id === node.id)))
      .map(node => string(node.text)).filter(Boolean);
    return {
      id: chunk.id, text,
      documentId: document?.id ?? '', documentName: string(document?.name) || string(chunk.document_name),
      sourceUri: sourceUri(chunk) || sourceUri(document),
      createdAt: timestamp && Number.isFinite(timestamp.getTime()) ? timestamp.toISOString() : '',
      recordedAt: string(parsedObject(text).recorded_at),
      summaries: [...new Set(summaries)],
    };
  });
}
