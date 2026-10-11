// Presentation only: preserve graph IDs, edges and the query cache objects.
interface Node { id: string; stage?: string; name?: string; [key: string]: unknown }

function metadata(node?: Node): Record<string, unknown> {
  try {
    const raw = node?.external_metadata;
    const value = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return value?.popot_memory ?? {};
  } catch { return {}; }
}

export function knowledgeLabels<T extends { nodes: Node[] }>(graph: T): T {
  const byId = new Map(graph.nodes.map(node => [node.id, node]));
  const title = (node?: Node): string => {
    const value = metadata(node).title;
    return typeof value === 'string' ? value : '';
  };
  const documentTitle = (node?: Node): string => title(node) ||
    title(byId.get(String(node?.document_id ?? '')));
  return { ...graph, nodes: graph.nodes.map(node => {
    let name = '';
    if (node.stage === 'document') name = title(node);
    if (node.stage === 'chunk') {
      const label = documentTitle(node);
      if (label) name = `Fragment · ${label}`;
    }
    if (node.stage === 'summary') {
      const label = documentTitle(byId.get(String(node.source_chunk_id ?? '')));
      if (label) name = `Summary · ${label}`;
    }
    return name ? { ...node, name } : node;
  }) };
}
