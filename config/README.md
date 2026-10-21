# Настройки локального проекта

Секреты хранятся в корневом `.env`, примеры переменных — в `.env.example`.
В JSON записываются настройки и имена переменных окружения, без значений ключей.

| Файл | Что менять |
| --- | --- |
| [monitoring.json](monitoring.json) | Настройки Grafana/Prometheus и секции `resources` для Grafana, Prometheus, kube-state-metrics и OTEL |
| [holmes.json](holmes.json) | Бюджет шагов и токенов, параметры LLM, ресурсы Job, deadline, время хранения завершённых Job и адрес шлюза Holmes |
| [omniroute/combos.json](omniroute/combos.json) | Состав, порядок бесплатных моделей и бюджеты fallback для `ax-free`, `ax-code-free`, `ax-harness-free`, `ax-ops-free` |
| [omniroute/settings.json](omniroute/settings.json) | Настройки шлюза, провайдеры, cooldown/circuit breaker и разрешения ключа AX |
| [agents.json](agents.json) | Профили провайдеров, endpoint, имя переменной модели, параметры запросов |
| [roles/](roles/) | Роли, инструкции, инструменты, память, делегирование, Harness² и бюджеты выполнения |
| [runtime.json](runtime.json) | Общие таймауты, ограничения сообщений/инструментов, память и разрешённые имена env |
| [../ax_local/config.json](../ax_local/config.json) | Контекст kind, namespace/workers, ресурсы AX Task, прокси и backend сессий |
| [../pyproject.toml](../pyproject.toml) | Версии образов и зависимостей, включая OmniRoute, Cognee, Holmes, Grafana и kube-state-metrics |

Каждая роль хранится в `roles/<название_роли>.json`, например `roles/planner.json`.
Имя роли берётся из имени файла. В JSON лежат только её настройки (`agent`,
`instructions`, `tools` и другие поля), без внешнего ключа роли и поля `name`.

[Схема ролей](roles/index.html) показывает обязанности, инструменты, навыки и
разрешённые связи делегирования. Откройте HTML в браузере и выберите роль.

Модели выбираются в `.env` через `OMNIROUTE_MODEL`, `OMNIROUTE_CODE_MODEL`,
`OMNIROUTE_OPS_MODEL`, `HARNESS2_IMPROVER_MODEL` и `HOLMES_MODEL`.
`project_operator` и Holmes по умолчанию используют `ax-ops-free`.

## Применение изменений

Все команды выполняются из корня репозитория; для AX нужен Python 3.11+.

**Мониторинг** — после правок `monitoring.json`:

```bash
python3 -m ax_local.cluster.resource_monitoring install
```

Установщик проверяет конфигурацию Prometheus через `promtool`, обновляет
ConfigMap/deployment и дожидается готовности Grafana, Prometheus и
kube-state-metrics. Если заданы ресурсы OTEL, проверяйте его rollout отдельно.
У каждого сервиса CPU/память лежат рядом с остальными настройками, в `resources`.
Сохранены текущие значения кластера: Prometheus имеет лимит памяти 512Mi и
requests 100m/128Mi; у OTEL `resources: {}`. Пропущенный лимит означает отсутствие
этого ограничения; пустой блок сохраняет работу без явно заданных ресурсов.
Заданные значения проверяются: 25m–4 CPU и 32Mi–4Gi, requests ≤ limits.
Оператор при изменении ресурсов требует полный блок requests/limits с CPU/памятью.
Настройки Grafana и kube-state-metrics должны оставаться в файле.

**Holmes** — `holmes.json` читается при создании нового исследования через роль
или CLI. Настройки и runner передаются в Job через ConfigMap; отдельная сборка
образа для изменения этих значений не нужна. Уже работающие исследования
лучше завершить до обновления общих ConfigMap. Подготовить окружение без LLM:

```bash
python3 -m ax_local.cluster.holmes --prepare
```

`max_steps` ограничивает цикл инструментов; `max_output_tokens` передаётся
в native `OVERRIDE_MAX_OUTPUT_TOKEN`, `llm_args` — аргументы клиента LLM.
Модель и ключ берутся из `.env`, а не из `holmes.json`.

**OmniRoute** — после правок `omniroute/settings.json` и `omniroute/combos.json`:

```bash
bash ax_local/compose.sh run --rm omniroute-init
```

Bootstrap использует секреты из `.env` и сверяет результат с JSON. Повторный
запуск применяет конфигурацию репозитория, включая сохранённые оператором комбо.

**Роли, профили, runtime, `.env`** — перезапустите API/MCP со свежим worker:

```bash
bash ax_local/rebuild-worker.sh
```

**Версии сервисов или полное окружение**:

```bash
bash ax_local/up.sh
```

Топология Docker, публикация портов и healthchecks остаются в
`ax_local/compose.yaml` и `memory/compose*.yaml`; Grafana публикуется port-forward
на `127.0.0.1:3002`. PVC и данные сервисов сохраняются отдельно от JSON.
Определение панелей Grafana остаётся в `ax_local/cluster/resource_dashboard.py`.

Роль `project_operator` может менять только состав управляемых комбо и ресурсы
поддерживаемых deployment, а также перезапускать их. Она показывает план и
применяет его после `/apply change-...` в том же чате. При изменении ресурсов
оператор сохраняет остальные настройки сервисов в `monitoring.json`. Остальные
настройки редактируются пользователем; оператору доступны только перечисленные операции.
