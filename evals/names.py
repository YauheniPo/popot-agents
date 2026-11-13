"""Human-readable experiment names with a shared execution identifier."""
import re
import base64
from datetime import datetime, timezone


def experiment_name(model, parameters, kind, run_id):
    return '-'.join((model.rsplit('/', 1)[-1], parameters.get('reasoning_effort', 'default'), kind, run_id))


def from_config(config, kind):
    run_id = config.get('run_id') or re.sub(
        r'^matrix-(\d{8}T\d{6}-[a-f0-9]+)-.*$', r'\1', config['experiment_name'])
    profile = next(iter(config['profiles'].values()))
    return experiment_name(config['primary_model'], profile.get('model_parameters', {}), kind, run_id)


def rename(api, config, experiment_id):
    identity = base64.b64decode(experiment_id, validate=True).decode()
    number = identity.removeprefix('Experiment:')
    if identity == number or not number.isdecimal():
        raise ValueError('Expected a native Phoenix Experiment ID')
    name = from_config(config, 'judge-v2' if config.get('evaluation') else 'evals') + '-' + number
    api.query('mutation($input:PatchExperimentInput!){patchExperiment(input:$input){__typename}}',
              {'input': {'experimentId': experiment_id, 'name': name}})
    result = api.query('query($id:ID!){node(id:$id){... on Experiment{name}}}', {'id': experiment_id})
    if (result.get('node') or {}).get('name') != name:
        raise RuntimeError('Phoenix experiment rename was not confirmed')
    return name


def dataset_label(api, dataset_id):
    dataset = api.query('query($id:ID!){node(id:$id){... on Dataset{name createdAt}}}',
                        {'id': dataset_id})['node']
    created = datetime.fromisoformat(dataset['createdAt']).astimezone(timezone.utc)
    return dataset['name'] + ' · ' + created.strftime('%Y-%m-%d %H:%M:%S UTC')
