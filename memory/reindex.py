"""Back up and reindex legacy Popot notes, preserving datasets and Skill nodes.

Run inside Cognee; no mutation without --apply. Reuse the same backup path to
resume an interrupted run. Keep agents idle while applying the migration.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib import error, request

try:
    from cognee_memory import CogneeMemory, memory_document
except ImportError:
    from popot_agents.worker.cognee_memory import CogneeMemory, memory_document


def save_archive(path, archive):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(archive, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def apply_archive(archive, path, client, delete):
    for dataset in archive['datasets']:
        for doc in dataset['documents']:
            memory_document(doc['text'], dataset['name'])
    # Persist every source before the first destructive API call.
    save_archive(path, archive)
    for dataset in archive['datasets']:
        # Delete legacy documents before adding replacements: old provenance
        # must not later delete entities shared with the rebuilt graph.
        for doc in dataset['documents']:
            if not doc.get('deleted'):
                delete(dataset['id'], doc['row']['id'])
                doc['deleted'] = True
                save_archive(path, archive)
        for doc in dataset['documents']:
            if doc.get('imported'):
                continue
            print(f"Indexing {dataset['name']} / {doc['row']['id']}", flush=True)
            result = client.remember(doc['text'], dataset['name'], run_in_background=False)
            if result.get('status') != 'completed':
                raise RuntimeError('Cognee indexing did not complete; backup retained')
            doc['imported'] = True
            save_archive(path, archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', action='append', required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    client = CogneeMemory('http://127.0.0.1:8000', os.environ['DEFAULT_USER_PASSWORD'], timeout=600)
    datasets = client._request('/api/v1/datasets')

    def raw(path, method='GET'):
        req = request.Request(client.api_url + path, method=method,
                              headers={'Authorization': 'Bearer ' + client.token})
        try:
            with client.opener.open(req, timeout=600) as response:
                data = response.read(100001)
                if len(data) > 100000:
                    raise ValueError('Legacy note exceeds the memory input bound')
                return data.decode('utf-8')
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            if method == 'DELETE' and code == 404:
                return ''  # A prior run may have ended after this delete.
            raise RuntimeError(f'Cognee migration request failed (HTTP {code})') from None

    names = set(args.dataset)
    if not names.issubset({d['name'] for d in datasets}):
        raise ValueError('Requested dataset does not exist')
    if args.backup.exists():
        archive = json.loads(args.backup.read_text())
        if {d['name'] for d in archive['datasets']} != names:
            raise ValueError('Backup belongs to different datasets')
    else:
        archive = {'version': 1, 'datasets': []}
        for dataset in datasets:
            if dataset['name'] not in names:
                continue
            entry = {**dataset, 'documents': [], 'graph': client._request(
                '/api/v1/datasets/' + dataset['id'] + '/graph')}
            offset = 0
            while True:
                rows = client._request(f"/api/v1/datasets/{dataset['id']}/data?limit=100&offset={offset}")
                for row in rows:
                    meta = row.get('externalMetadata') or {}
                    if (row['name'] != 'memory' and not row['name'].startswith('memory_')) or (
                            isinstance(meta, dict) and meta.get('popot_memory', {}).get('version') == 2):
                        continue
                    text = raw(f"/api/v1/datasets/{dataset['id']}/data/{row['id']}/raw")
                    memory_document(text, dataset['name'])
                    entry['documents'].append({'row': row, 'text': text})
                if len(rows) < 100:
                    break
                offset += len(rows)
            archive['datasets'].append(entry)
        save_archive(args.backup, archive)
    count = sum(len(d['documents']) for d in archive['datasets'])
    print(f'Backup: {args.backup}; legacy documents: {count}', flush=True)
    if args.apply:
        apply_archive(archive, args.backup, client, lambda d, item:
                      raw(f'/api/v1/datasets/{d}/data/{item}', 'DELETE'))
        print('Reindex completed', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid echoing upstream bodies or source content on failure.
        print(f'Reindex failed: {type(exc).__name__}; backup retained', file=sys.stderr)
        raise SystemExit(1)
