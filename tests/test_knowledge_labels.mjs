import assert from 'node:assert/strict';
import { knowledgeLabels } from '../memory/ui/knowledgeLabels.ts';
import { linkedKnowledge, safeSourceUrl } from '../memory/ui/linkedKnowledge.ts';

const record = { recorded_at: '2026-10-10', agent_model: 'example',
  references: ['https://github.com/a/b/pull/70'], memories: [{ text: 'Retry waits 60 seconds.' }] };
const metadata = JSON.stringify({ popot_memory: { version: 2, title: 'PR #70 retry policy', record } });
const raw = { nodes: [
  { id: 'e', stage: 'entity', name: 'HTTP retries' },
  { id: 'd', stage: 'document', name: 'memory_deadbeef', external_metadata: metadata },
  { id: 'c', stage: 'chunk', document_id: 'd', text: 'Retry waits 60 seconds.', external_metadata: metadata },
  { id: 's', stage: 'summary', source_chunk_id: 'c', text: 'Retries' },
], links: [{ source: 'c', target: 'e' }, { source: 'c', target: 'd' }, { source: 's', target: 'c' }] };
const graph = knowledgeLabels(raw);
assert.equal(graph.nodes[1].name, 'PR #70 retry policy');
assert.equal(graph.nodes[2].name, 'Fragment · PR #70 retry policy');
assert.equal(graph.nodes[3].name, 'Summary · PR #70 retry policy');
assert.equal(graph.nodes[0].name, 'HTTP retries');
assert.equal(raw.nodes[1].name, 'memory_deadbeef', 'must not mutate React Query cache');
assert.deepEqual(graph.links, raw.links, 'preserve graph identities and edges');
const knowledge = linkedKnowledge('e', graph)[0];
assert.equal(knowledge.recordedAt, '2026-10-10');
assert.equal(knowledge.sourceUri, 'https://github.com/a/b/pull/70');
assert.deepEqual(knowledge.provenance, record);
assert.equal(safeSourceUrl('https://github.com/a/b/pull/70,'), 'https://github.com/a/b/pull/70');
const legacy = { nodes: [{ id: 'd', stage: 'document', name: 'README.md' }], links: [] };
assert.equal(knowledgeLabels(legacy).nodes[0].name, 'README.md');
console.log('Knowledge labels and provenance checks passed');
