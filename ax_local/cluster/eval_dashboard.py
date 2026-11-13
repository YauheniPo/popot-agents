"""Provisioned experiment overview; Phoenix retains detailed task evidence."""
from evals.metrics import METRICS


def dashboard():
    datasource = {'type': 'prometheus', 'uid': 'popot-prometheus'}
    scope = 'series=~"${series:regex}"'
    selected = scope + ',experiment=~"${experiment:regex}"'
    paired = scope + ',experiment=~"${experiment:regex}",baseline=~"${baseline:regex}"'

    def names(selector):
        # Labels survive in history after a rename. Pick one latest alias and
        # normalize its timestamp to 1 before multiplying measured values.
        latest = (f'clamp_max(topk by(series,experiment) (1, last_over_time(popot_eval_info{{{selector},'
                  'metric="display_name_updated_at_seconds",display_name!=""}[$__range])), 1)')
        return (f'({latest} or on(series,experiment) '
                f'((last_over_time(popot_eval_info{{{selector},metric="configuration",display_name!="",series_display_name!=""}}[$__range]) '
                f'or on(series,experiment) last_over_time(popot_eval_info{{{selector},metric="configuration",display_name!="",series_display_name=""}}[$__range]) '
                f'or on(series,experiment) label_replace(last_over_time(popot_eval_info{{{selector},metric="configuration",display_name=""}}[$__range]),'
                '"display_name","$1","experiment","(.+)")) '
                f'or on(series,experiment) label_replace(max by(series,experiment) '
                f'(last_over_time(popot_eval_progress{{{selector},metric="planned_runs"}}[$__range])), '
                '"display_name","$1","experiment","(.+)")))')

    def named(expression):
        # Info aliases preserve the stored keys and old dashboard links.
        return (f'(({expression}) * on(series,experiment) group_left(display_name) '
                f'(max by(series,experiment,display_name) ({names(scope)}))) '
                f'or on(series,experiment,metric) label_replace(({expression}),'
                '"display_name","$1","experiment","(.+)")')
    panels = [{
        'id': 1, 'title': 'Как читать сравнение', 'type': 'text',
        'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 4},
        'options': {'mode': 'markdown', 'content':
            'Зелёное **Δ > 0 = лучше**, красное **Δ < 0 = хуже**. Знак уже учитывает направление '
            'метрики: снижение токенов/времени даёт положительное Δ. Нет данных — не ноль. '
            'Сравниваются одинаковые кейсы и повторы; неполные серии не получают оценку улучшения. '
            'Самооценка агента отделена от проверенного результата и judge. '
            'Показаны наблюдаемые различия, не статистическая значимость. '
            'Полный ответ, проверки и trace доступны в Phoenix. История ограничена retention Prometheus.'}}]

    def panel(title, expression, unit='short', *, delta=False):
        panel = {'id': len(panels) + 1, 'title': title, 'type': 'stat', 'datasource': datasource,
            'targets': [{'refId': 'A', 'expr': named(expression), 'instant': True,
                         'legendFormat': '{{display_name}}'}],
            'options': {'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
                        'textMode': 'value_and_name', 'colorMode': 'value', 'graphMode': 'none'},
            'fieldConfig': {'defaults': {'unit': unit, 'noValue': 'нет данных',
                'thresholds': {'mode': 'absolute', 'steps': [
                    {'color': 'red' if delta else 'blue', 'value': None},
                    {'color': 'text', 'value': 0}, {'color': 'green', 'value': 1e-9}]}}, 'overrides': []}}
        panels.append(panel)
        if not delta:
            panel['fieldConfig']['defaults']['thresholds']['steps'] = [{'color': 'blue', 'value': None}]
        return panel

    for metric, label in [('planned_runs', 'План кейсов'), ('completed_runs', 'Завершено кейсов'),
                          ('progress', 'Прогресс серии')]:
        panel(label, f'last_over_time(popot_eval_progress{{{selected},metric="{metric}"}}[$__range])',
              'percentunit' if metric == 'progress' else 'short')
    panel('Сопоставимость: 1 = да, 0 = нет',
          f'last_over_time(popot_eval_comparison{{{paired},metric="comparable"}}[$__range])')
    verdict = panel('Итог сравнения',
          f'last_over_time(popot_eval_comparison{{{paired},metric="verdict"}}[$__range])')
    verdict['fieldConfig']['defaults']['mappings'] = [{'type': 'value', 'options': {
        '-1': {'text': 'ХУЖЕ', 'color': 'red'}, '0': {'text': 'БЕЗ ИЗМЕНЕНИЙ', 'color': 'text'},
        '1': {'text': 'ЛУЧШЕ', 'color': 'green'}, '2': {'text': 'КОМПРОМИСС', 'color': 'yellow'},
        '3': {'text': 'НЕДОСТАТОЧНО ДАННЫХ', 'color': 'text'}}}]
    metric_panels = {}
    for name, (direction, label, unit) in METRICS.items():
        raw = panel(label + {'MAXIMIZE': ': больше — лучше ↑', 'MINIMIZE': ': меньше — лучше ↓',
                             'NONE': ' (самоотчёт)'}[direction],
                    f'last_over_time(popot_eval_value{{{selected},metric="{name}"}}[$__range])', unit)
        metric_panels[name] = [raw]
        raw['description'] = ('Среднее по всем case/repetition завершённой серии; пропуск любого значения '
                              'оставляет метрику неизвестной. Меньше запросов не доказывает качество.')
        if name in {'prompt_tokens', 'completion_tokens'}:
            raw['description'] += (' В старых экспериментах при неизвестном total_tokens это могла быть '
                                   'только сумма известных вызовов; её недостаточно для сравнения полного расхода.')
        if direction != 'NONE':
            delta = panel('Δ улучшения · ' + label + ': больше — лучше',
                          f'last_over_time(popot_eval_improvement{{{paired},metric="{name}"}}[$__range])',
                          unit, delta=True)
            metric_panels[name].append(delta)
            formula = 'baseline − эксперимент' if direction == 'MINIMIZE' else 'эксперимент − baseline'
            delta['description'] = ('Δ улучшения = ' + formula + '. Положительное значение — лучше, '
                                    'отрицательное — хуже, ноль — без изменений. Это изменение с учётом '
                                    'направления метрики, в её исходных единицах. Для процентов — '
                                    'разница долей, не относительный рост. Снижение затрат оценивайте '
                                    'вместе с качеством результата.')
            if name == 'total_tokens':
                delta['description'] += ' Пример: baseline 1000, эксперимент 800 → Δ улучшения +200.'
    panels.append({'id': len(panels) + 1, 'title': 'Параметры экспериментов', 'type': 'table',
        'datasource': datasource,
        'targets': [{'refId': 'A', 'expr': names(selected + ',phoenix_url!=""'),
                     'instant': True, 'format': 'table'}],
        'fieldConfig': {'defaults': {}, 'overrides': [{'matcher': {'id': 'byName', 'options': 'phoenix_url'},
            'properties': [{'id': 'links', 'value': [{'title': 'Открыть Phoenix', 'url': '${__value.raw}', 'targetBlank': True}]}]}]},
        'transformations': [{'id': 'organize', 'options': {'excludeByName': {'Time': True, 'Value': True, 'experiment': True},
            'indexByName': {'display_name': 0, 'model': 1, 'harness': 2, 'parallel_models': 3,
                            'parameters': 4, 'source_revision': 5},
            'renameByName': {'parallel_models': 'Параллельных моделей'}}}]})
    sections = [('Обзор запуска', [panels[1:4], panels[4:6]])]
    for title, metrics in [
        ('Качество результата', ['objective_score', 'functional_success', 'false_completion']),
        ('Оценки judge и агента', ['judge_score', 'self_score']),
        ('Токены', ['total_tokens', 'prompt_tokens', 'completion_tokens', 'judge_tokens']),
        ('Время выполнения', ['latency_seconds']),
        ('Действия harness', ['steps', 'llm_requests', 'tool_calls', 'coordination_turns']),
        ('Ошибки и повторы', ['request_errors', 'tool_errors', 'format_errors', 'model_retries']),
    ]:
        sections.append((title, [metric_panels[name] for name in metrics]))
    sections.append(('Параметры и ссылки', [[panels[-1]]]))
    # Keep panel IDs stable for saved links; category rows get new IDs.
    layout, y = [panels[0]], 4
    for index, (title, rows) in enumerate(sections):
        layout.append({'id': len(panels) + index + 1, 'title': title, 'type': 'row',
                       'collapsed': False, 'panels': [],
                       'gridPos': {'x': 0, 'y': y, 'w': 24, 'h': 1}})
        y += 1
        for row in rows:
            height = 8 if row[0]['type'] == 'table' else 6
            width = 24 // len(row)
            for column, item in enumerate(row):
                item['gridPos'] = {'x': column * width, 'y': y, 'w': width, 'h': height}
                layout.append(item)
            y += height
    experiments = names(scope)
    baselines = (experiments + ' and on(series,experiment) label_replace('
                 f'last_over_time(popot_eval_comparison{{{scope},metric="comparable"}}[$__range]),'
                 '"experiment","$1","baseline","(.+)")')
    suites = ('max by(series,series_display_name) (last_over_time(popot_eval_info{series_display_name!=""}[$__range])) '
              'or on(series) label_replace(max by(series) (last_over_time(popot_eval_progress[$__range])), '
              '"series_display_name","popot-harness-local-$1","series","(.{12}).*")')
    return {'uid': 'popot-evals', 'title': 'Popot Evals — сравнение моделей и параметров',
            'schemaVersion': 39, 'version': 1, 'refresh': '15s', 'timezone': 'utc',
            'tags': ['popot', 'evals'], 'time': {'from': 'now-7d', 'to': 'now'},
            'templating': {'list': [
                {'name': name, 'label': label, 'type': 'query', 'datasource': datasource,
                 'query': query, 'refresh': 2, 'multi': multi, 'includeAll': multi,
                 'regex': ('/series_display_name="(?<text>[^"]+)|series="(?<value>[^"]+)/g' if name == 'series' else
                           '/display_name="(?<text>[^"]+)|experiment="(?<value>[^"]+)/g'),
                 'allValue': '.*' if multi else None}
                for name, label, query, multi in [
                    ('series', 'Набор задач', 'query_result(' + suites + ')', False),
                    ('experiment', 'Эксперименты', 'query_result(' + experiments + ')', True),
                    ('baseline', 'Baseline', 'query_result(' + baselines + ')', False)]]},
            'panels': layout}
