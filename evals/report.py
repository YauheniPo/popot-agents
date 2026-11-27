"""Join owned gateway measurements to a baseline and render its observed results."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from statistics import median

from evals import save
from evals.baseline import evaluate, summarize
from popot_agents.artifacts import validate_artifacts


def enrich(report, calls, directory, config):
    # Older captures stored evaluator failures in the scored results.
    measured = []
    for row in report['results']:
        if row.get('result', {}).get('runner_error'):
            report.setdefault('incomplete', []).append(row)
            continue
        measured.append(row)
        if row['case_id'] == 'delegation':
            children = [t['sessionId'] for t in row['result'].get('saved_chat', {}).get('work', {}).get('tasks', [])
                        if t.get('parentSessionId') == row['session_id'] and re.fullmatch(r'[a-f0-9]{16}', t['sessionId'])]
            delivered = False
            for child in children:
                saved = json.loads((directory / 'sessions' / (child + '.json')).read_text())
                meta = saved.get('roleConfig', {}).get('_ax_delegation', {})
                if meta.get('parent_session_id') != row['session_id']:
                    continue
                artifacts = validate_artifacts(meta.get('outcome', {}).get('artifacts', []))
                delivered |= any(a.get('name') == 'handoff.txt' and a.get('content') == 'CHILD_HANDOFF_OK\n' for a in artifacts)
            row['observed'].update(child_ids=children, child_artifact=delivered)
        own = [c for c in calls if c['root_session_id'] == row['session_id']]
        row['gateway_calls'] = own
        events = [json.loads(x) for x in (directory / row['trace_file']).read_text().splitlines()]
        row['event_counts'] = dict(Counter(e.get('event') for e in events))
        counts = row['event_counts']
        row['tokens_known'] = sum(c['tokens'] for c in own if c['tokens'] is not None)
        row['usage_complete'] = (bool(own) and all(c['tokens'] is not None for c in own)
                                 and len(own) == counts.get('model_request', 0) == counts.get('model_response', 0)
                                 and not any(counts.get(k) for k in ('model_http_error', 'model_timeout', 'model_transport_error')))
        row['tokens'] = row['tokens_known'] if row['usage_complete'] else None
        model = config['primary_model']
        provider, _, short_model = model.partition('/')
        row['model_pin_verified'] = bool(own) and all(
            c.get('requested_model') == model and c.get('provider') == provider and
            (c.get('model') == short_model or c.get('error')) for c in own)
        row.update(evaluate(row['case_id'], row['outcome'], events, row['observed']))
        save(directory / (Path(row['trace_file']).stem + '.json'), row)
    report['results'] = measured
    report['summary'] = summarize(measured)
    report['summary']['model_pin_verified'] = all(r['model_pin_verified'] for r in measured) if measured else None
    report['summary']['gateway_calls'] = len(calls)
    report['summary']['observed_models'] = dict(Counter(c.get('model') for c in calls))
    return report


def render(report, config):
    summary = report['summary']
    memory_enabled = config.get('memory_mode', 'enabled') != 'disabled'
    lines = ['# Baseline текущего HTTP harness', '',
             f"Исходная ревизия: `{config['source_revision']}`.",
             f"Worker image: `{config['worker_image']}`.",
             f"Запрошен прямой маршрут модели: `{config['primary_model']}`. Сверка фактической модели приведена ниже.", '',
             'Это controlled baseline локальных сценариев: отдельный AX API, исходный HTTP harness, ',
             'нативные инструменты ролей и локальный Git. Внешние MCP исключены; Harness² выключен.',
             ('Cognee включён; начальная память каждого запуска пуста.' if memory_enabled else
              'Cognee MCP и его credentials исключены из всех ролей, включая дочерние. Автоматические recall, capture и журнал Cognee отключены.'),
             'Рабочая конфигурация проекта не менялась.', '',
             f"Запусков: **{summary['runs']}**, прошли весь контракт: **{summary['passed']}**; ",
             f"прошли функциональные проверки: **{summary.get('functional_passed')}**; ",
             f"ложных завершений: **{summary['false_completions']}**.",
             f"Токены найденных LLM-вызовов{' (включая извлечение памяти)' if memory_enabled else ''}: **{summary['tokens_known']}**.",
             'Это нижняя граница: для неуспешных или незавершённых model requests usage может отсутствовать.',
             f"Проверка закреплённой модели по gateway: **{summary.get('model_pin_verified')}**.", '',
             '| Сценарий | Полный успех | Функционально | Медиана, с | Токены |',
             '|---|---:|---:|---:|---:|']
    for case_id in dict.fromkeys(r['case_id'] for r in report['results']):
        rows = [r for r in report['results'] if r['case_id'] == case_id]
        tokens = str(sum(r['tokens_known'] for r in rows))
        if not all(r['usage_complete'] for r in rows):
            tokens = '≥' + tokens
        lines.append(f"| {case_id} | {sum(r['passed'] for r in rows)}/{len(rows)} | "
                     f"{sum(r['functional_passed'] for r in rows)}/{len(rows)} | "
                     f"{median(r['elapsed_seconds'] for r in rows):.1f} | {tokens} |")
    lines += ['', '## Непрошедшие проверки', '']
    for row in report['results']:
        if row['passed']:
            continue
        failed = ', '.join(k for k, v in row['checks'].items() if not v)
        lines.append(f"- `{row['case_id']}/{row['repetition']}`: {failed}; "
                     f"outcome `{row['outcome'].get('status')}`, reason `{row['outcome'].get('reason', '')}`; "
                     f"session `{row['session_id']}`.")
    lines += ['', '## Границы измерения', '',
              '- Небольшое число повторов — начальная точка сравнения, не статистическая оценка качества модели.',
              '- Время включает создание/допуск worker, инструменты, делегирование, сохранение и проверки оценщика.',
              '- Ожидаемые ошибки чтения включены в число tool_error; их нельзя считать регрессиями по одному счётчику.',
              '- Spans импортированы после выполнения. Их время — наблюдение оценщика; исходные JSONL сохранены.',
              '- Нет измерений переключения моделей, внешнего GitHub, настоящего падения процесса API и публикации PR.',
              '- Стоимость неизвестна: провайдерские токены не являются подтверждённым денежным счётом.', '']
    if report.get('measurement_note'):
        lines += ['## Сбор данных', '', report['measurement_note'], '',
                  'Оригинальные отчёты, конфигурации и версии runner сохранены в `provenance/`. ',
                  'Реальные неудачи агентов сохранены в 12 измерениях; переиграны только отсутствующие измерения.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('gateway', type=Path)
    args = parser.parse_args()
    config = json.loads((args.directory / 'config.json').read_text())
    report = enrich(json.loads((args.directory / 'report.json').read_text()),
                    json.loads(args.gateway.read_text()), args.directory, config)
    save(args.directory / 'report.json', report)
    (args.directory / 'summary.md').write_text(render(report, config))
