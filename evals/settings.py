"""One configuration for standalone evals and model/parameter matrices."""
import json
from pathlib import Path
import re

from popot_agents.runtime_config import RUNTIME, validate_model_parameters, validate_workflow_policy


def validate_config(settings):
    if not isinstance(settings, dict) or set(settings) - {'parallel_models'} != {
            'baseline', 'variants', 'repetitions', 'workflow',
            'request_timeout_seconds', 'inspection_timeout_seconds'}:
        raise ValueError('evals config has invalid x-evals fields')
    parallel = settings.get('parallel_models', 1)
    if type(parallel) is not int or not 1 <= parallel <= 5:
        raise ValueError('x-evals.parallel_models must be between 1 and 5')
    variants = settings['variants']
    if not isinstance(variants, list) or not variants:
        raise ValueError('x-evals.variants must be a nonempty list')
    names = set()
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != {'name', 'model', 'parameters'}:
            raise ValueError('Each x-evals variant needs name, model and parameters')
        name = variant['name']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,29}', name)
                or name in names):
            raise ValueError('x-evals variant names must be unique safe directory names')
        names.add(name)
        if not isinstance(variant['model'], str) or not variant['model'].strip():
            raise ValueError('x-evals variant model must be nonempty')
        validate_model_parameters(variant['parameters'])
    if not isinstance(settings['baseline'], str) or settings['baseline'] not in names:
        raise ValueError('x-evals.baseline must name one of the variants')
    if type(settings['repetitions']) is not int or not 1 <= settings['repetitions'] <= 3:
        raise ValueError('x-evals.repetitions must be between 1 and 3')
    validate_workflow_policy(settings['workflow'], RUNTIME['timeouts']['max_turn_seconds'])
    for key in ('request_timeout_seconds', 'inspection_timeout_seconds'):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f'x-evals.{key} must be positive')
    if settings['request_timeout_seconds'] <= settings['workflow']['total_seconds']:
        raise ValueError('x-evals.request_timeout_seconds must exceed workflow.total_seconds')
    return settings


def load_config(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name('config.json')
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or {'x-baseline', 'x-experiments'} & document.keys():
        raise ValueError('Move x-baseline and x-experiments settings into x-evals')
    return validate_config(document.get('x-evals'))
