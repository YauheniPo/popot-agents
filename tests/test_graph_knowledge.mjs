import { describe, test } from 'node:test';
import assert from 'node:assert/strict';
import { linkedKnowledge, safeSourceUrl } from '../memory/ui/linkedKnowledge.ts';

const entity = { id: 'e', stage: 'entity', name: 'Cognee' };
const nodes = [entity, { id: 'other', stage: 'entity' },
  { id: 'c', stage: 'chunk', text: 'Verified knowledge', document_id: 'd', t_created: 1791106937469 },
  { id: 'd', stage: 'document', name: 'decision.txt', external_metadata: '{"_cognee":{"source_uri":"https://example.com/source"}}' },
  { id: 's', stage: 'summary', text: 'Extracted summary', source_chunk_id: 'c' },
  { id: 'unrelated', stage: 'chunk', text: 'Another project' },
];
const links = [{ source: 'c', target: 'e', relation: 'contains' },
  { source: 'c', target: 'e', relation: 'contains' },
  { source: 'c', target: 'd', relation: 'is_part_of' },
  { source: 's', target: 'c', relation: 'made_from' },
  { source: 'e', target: 'other', relation: 'uses' },
  { source: 'unrelated', target: 'other', relation: 'contains' }];

describe('Linked knowledge', () => {
  test('follows entity -> containing chunk -> source document, once, without semantic expansion', () => {
    const result = linkedKnowledge('e', { nodes, links });
    assert.equal(result.length, 1);
    assert.equal(result[0].id, 'c');
    assert.equal(result[0].text, 'Verified knowledge');
    assert.equal(result[0].documentName, 'decision.txt');
    assert.equal(result[0].sourceUri, 'https://example.com/source');
    assert.equal(result[0].createdAt, '2026-10-04T09:42:17.469Z');
    assert.deepEqual(result[0].summaries, ['Extracted summary']);
  });

  test('supports reversed links and document-linked entities without inventing a text match', () => {
    assert.equal(linkedKnowledge('e', { nodes, links: [{ source: 'e', target: 'c', relation: 'mentions' }] }).length, 1);
    const result = linkedKnowledge('e', { nodes, links: [{ source: 'e', target: 'd', relation: 'mentioned_in' }] });
    assert.equal(result[0].text, 'Verified knowledge');
    assert.deepEqual(linkedKnowledge('missing', { nodes, links }), []);
    assert.deepEqual(linkedKnowledge('e', { nodes, links: [] }), []);
  });

  test('missing text, missing endpoints and malformed metadata remain explicit', () => {
    const result = linkedKnowledge('e', { nodes: [entity, { id: 'c', stage: 'chunk', external_metadata: 'broken' }],
      links: [{ source: 'c', target: 'e' }, { source: 'absent', target: 'e' }] });
    assert.equal(result.length, 1);
    assert.equal(result[0].text, '');
    assert.equal(result[0].sourceUri, '');
  });

  test('JSON memory keeps evidence and historical dates; unsafe source links are never clickable', () => {
    const text = JSON.stringify({ recorded_at: '2026-10-04T10:46:34+00:00', memories: [{ text: 'Decision', evidence_quote: 'Exact evidence' }] });
    const result = linkedKnowledge('e', { nodes: [entity, { id: 'c', stage: 'chunk', text }], links: [{ source: 'c', target: 'e' }] });
    assert.equal(result[0].recordedAt, '2026-10-04T10:46:34+00:00');
    assert.equal(result[0].text, text);
    assert.equal(safeSourceUrl('https://example.com/pr/1'), 'https://example.com/pr/1');
    for (const url of ['javascript:alert(1)', 'data:text/html,hi', 'file:///app/secret', 'https://user:password@example.com', '//example.com']) {
      assert.equal(safeSourceUrl(url), null);
    }
  });
});
