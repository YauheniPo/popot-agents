"""Bound review work between explicit, model-reported progress checkpoints."""
import json

from popot_agents.tools import ToolInputError


NAME = 'review_checkpoint'
INTERVAL = 6
FIELDS = ('covered_files', 'decisions', 'remaining_work')
SCHEMA = {'type': 'function', 'function': {
    'name': NAME,
    'description': 'Record reviewed paths, accepted/rejected candidates with evidence, and concrete remaining work. '
        'Paths and decisions accumulate: send only additions, citing new evidence and the earlier '
        'decision when superseding it. remaining_work replaces the pending list. Include publication and read-back '
        'in remaining_work. When nothing remains, pass [] to end tool use and return the final answer.',
    'parameters': {'type': 'object', 'properties': {
        key: {'type': 'array', 'maxItems': 100,
              'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}} for key in FIELDS},
        'required': list(FIELDS), 'additionalProperties': False}}}


class ReviewProgress:
    def __init__(self, previous=None):
        self.snapshot = previous
        self.rounds = 0
        # A resumed task may contain a new user requirement; retain decisions,
        # but do not let a previous completion prevent new work.
        self.finished = False

    @property
    def due(self):
        return self.rounds >= INTERVAL

    def record(self, arguments, *, timeout_seconds, call_id):
        if set(arguments) != set(FIELDS) or any(
                not isinstance(arguments[key], list) or len(arguments[key]) > 100 or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 2000
                    for item in arguments[key]) for key in FIELDS):
            raise ToolInputError('Checkpoint requires covered_files, decisions and remaining_work '
                                 'as bounded lists of nonempty strings.')
        previous = self.snapshot or {}
        self.snapshot = {key: list(dict.fromkeys([
            *(previous.get(key, []) if key != 'remaining_work' else []),
            *(item.strip() for item in arguments[key])])) for key in FIELDS}
        self.rounds = 0
        self.finished = not self.snapshot['remaining_work']
        return json.dumps({'reported_progress': self.snapshot, 'tool_work_finished': self.finished,
                           'notice': 'Model-reported progress, not independent proof of completion.'})

    def focus(self):
        return ('Continue the concrete remaining work in the checkpoint below. Do not restart '
                'a completed area or reopen a rejected candidate without citing new evidence. '
                'Read the relevant source or run one discriminating check before speculating; '
                'do not enumerate hypothetical defects already rejected. If remaining work is '
                'done, record [] immediately, without inventing one more optional pass. '
                'Paths and decisions are cumulative model reports, not verification receipts.\n'
                + json.dumps(self.snapshot, ensure_ascii=False))
