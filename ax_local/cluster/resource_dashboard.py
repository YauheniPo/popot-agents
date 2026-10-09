"""Provision the resource-only Grafana dashboard for the local cluster."""


def dashboard() -> dict:
    ns = 'namespace=~"$namespace",pod=~"$pod"'
    containers = ns + ',container!="",container!="POD"'
    process = 'role=~"$role",task=~"$task",process=~"$process"'
    # Collector caches gauges briefly after exit. Hide them in current views,
    # while keeping the historical samples available for range queries.
    active = f'(time() - popot_sandbox_process_sampled_at_seconds{{{process}}} < 20)'
    pid = 'role,task,pid,process,process_start'
    panels = []

    def panel(title, expression, unit, legend, description='', *, width=12):
        index = len(panels)
        panels.append({
            'id': index + 1, 'title': title, 'type': 'timeseries',
            'description': description,
            'datasource': {'type': 'prometheus', 'uid': 'popot-prometheus'},
            'gridPos': {'x': 0 if width == 24 else (index % 2) * 12,
                        'y': (index // 2) * 8, 'w': width, 'h': 8},
            'targets': [{'refId': 'A', 'expr': expression, 'legendFormat': legend}],
            'fieldConfig': {'defaults': {'unit': unit, 'min': 0,
                'custom': {'drawStyle': 'line', 'lineWidth': 1, 'fillOpacity': 8,
                           'spanNulls': False}}, 'overrides': []},
            'options': {'legend': {'displayMode': 'table', 'placement': 'bottom',
                                  'calcs': ['lastNotNull', 'max']},
                        'tooltip': {'mode': 'multi', 'sort': 'desc'}},
        })

    panel('Pod CPU (cores)', f'sum by(namespace,pod)(rate(container_cpu_usage_seconds_total{{{containers}}}[$__rate_interval]))',
          'cores', '{{namespace}} / {{pod}}', '1 core = 100% of one CPU.')
    panel('Pod memory (working set)', f'sum by(namespace,pod)(container_memory_working_set_bytes{{{containers}}})',
          'bytes', '{{namespace}} / {{pod}}')
    panel('Container CPU (cores)', f'sum by(namespace,pod,container)(rate(container_cpu_usage_seconds_total{{{containers}}}[$__rate_interval]))',
          'cores', '{{pod}} / {{container}}')
    panel('Container memory (working set)', f'max by(namespace,pod,container)(container_memory_working_set_bytes{{{containers}}})',
          'bytes', '{{pod}} / {{container}}')
    for resource, unit in [('cpu', 'cores'), ('memory', 'bytes')]:
        panel(f'Pod {resource} requests', f'sum by(namespace,pod)(kube_pod_container_resource_requests{{{ns},resource="{resource}"}})', unit, '{{pod}}')
        panel(f'Pod {resource} limits', f'sum by(namespace,pod)(kube_pod_container_resource_limits{{{ns},resource="{resource}"}})', unit, '{{pod}}')
    panel('Sandbox CPU by process (%)', f'100 * rate(popot_sandbox_process_cpu_seconds_total{{{process}}}[$__rate_interval]) and on({pid}) {active}',
          'percent', '{{role}} / {{task}} / {{process}} [{{pid}}]', '100% = one CPU core. Includes the harness and its child processes.')
    panel('Sandbox RSS by process', f'popot_sandbox_process_memory_bytes{{{process}}} and on({pid}) {active}',
          'bytes', '{{role}} / {{task}} / {{process}} [{{pid}}]', 'RSS can count shared pages in multiple processes; do not sum it as unique sandbox memory.')
    panel('Sandbox process count', f'count by(role,task)({active})', 'short', '{{role}} / {{task}}',
          'Sampling runs only while the sandbox is active. Empty results are expected when all tasks are suspended.')
    panel('Container restarts', f'max by(namespace,pod,container)(kube_pod_container_status_restarts_total{{{ns}}})',
          'short', '{{pod}} / {{container}}')
    panel('Role CPU (%)', f'sum by(role)(100 * rate(popot_sandbox_process_cpu_seconds_total{{{process}}}[$__rate_interval]) and on({pid}) {active})',
          'percent', '{{role}}', 'Sum of process CPU for the selected tasks. 100% = one CPU core.')
    panel('Role process RSS (sum)', f'sum by(role)(popot_sandbox_process_memory_bytes{{{process}}} and on({pid}) {active})',
          'bytes', '{{role}}', 'Sum of process RSS, including shared pages more than once; this is not unique sandbox or pod memory.')
    panel('Active tasks by role', f'count by(role)(count by(role,task)({active}))',
          'short', '{{role}}', 'Active sampled sandboxes, not the shared warm worker pods. Role filters apply to sandbox panels; pod/container panels show infrastructure totals.')
    panel('Tasks by role in selected period', f'count by(role)(count by(role,task)(max_over_time(popot_sandbox_process_sampled_at_seconds{{{process}}}[$__range])))',
          'short', '{{role}}', 'Distinct tasks sampled in the selected time range, including suspended and finished work. Older samples without role labels cannot be attributed to a role.')
    panels[-1]['type'] = 'stat'
    panels[-1]['targets'][0].update(instant=True, range=False)
    panels[-1]['options'] = {'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
                            'textMode': 'value_and_name', 'colorMode': 'value'}

    def variable(name, query):
        return {'name': name, 'label': name.title(), 'type': 'query',
                'datasource': {'type': 'prometheus', 'uid': 'popot-prometheus'},
                'query': query, 'refresh': 2, 'multi': True, 'includeAll': True,
                'allValue': '.*', 'current': {'text': 'All', 'value': '$__all'}}

    return {'uid': 'popot-resources', 'title': 'Popot — cluster & sandbox resources',
            'schemaVersion': 39, 'version': 1, 'editable': False,
            'tags': ['popot', 'resources'], 'timezone': 'browser', 'refresh': '15s',
            'time': {'from': 'now-30m', 'to': 'now'},
            'templating': {'list': [
                variable('namespace', 'label_values(kube_pod_info,namespace)'),
                variable('pod', 'label_values(kube_pod_info{namespace=~"$namespace"},pod)'),
                variable('role', 'label_values(popot_sandbox_process_sampled_at_seconds,role)'),
                variable('task', 'label_values(popot_sandbox_process_sampled_at_seconds{role=~"$role"},task)'),
                variable('process', 'label_values(popot_sandbox_process_sampled_at_seconds{role=~"$role",task=~"$task"},process)')]},
            'panels': panels}
