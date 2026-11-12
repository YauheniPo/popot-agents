# Настройки локального проекта

Секреты хранятся в корневом `.env`, примеры переменных — в `.env.example`.
В JSON записываются настройки и имена переменных окружения, без значений ключей.

| Файл | Что менять |
| --- | --- |
| [monitoring.json](monitoring.json) | Настройки Grafana/Prometheus и секции `resources` для Grafana, Prometheus, kube-state-metrics и OTEL |
| [holmes.json](holmes.json) | Бюджет шагов и токенов, параметры LLM, ресурсы Job, deadline, время хранения завершённых Job и адрес шлюза Holmes |
| [omniroute/combos.json](omniroute/combos.json) | Состав, порядок моделей и бюджеты fallback для `ax-free` и `ax-ollama` |
| [omniroute/settings.json](omniroute/settings.json) | Настройки шлюза, лимиты логирования, провайдеры, cooldown/circuit breaker и разрешения ключа AX |
| [agents.json](agents.json) | Профили провайдеров, endpoint, имя переменной модели, параметры запросов |
| [roles/](roles/) | Роли, инструкции, инструменты, память, делегирование, Harness² и бюджеты выполнения |
| [prompts/](prompts/) | Markdown-шаблоны общих инструкций: делегирование, ревью, отчёт, память, Harness2, skills и Holmes |
| [runtime.json](runtime.json) | Роль MCP по умолчанию (`mcp.default_role`), лимиты координации (`workflow`), общие таймауты, ограничения сообщений/инструментов и доказательств, разрешённые имена env |
| [../ax_local/config.json](../ax_local/config.json) | Контекст kind, namespace/workers, ресурсы AX Task, прокси и backend сессий |
| [../evals/config.json](../evals/config.json) | Phoenix, baseline и `x-tracing`: постоянный проект трейсов AX, endpoint, очередь и повторы доставки |
| [../memory/config.json](../memory/config.json) | Сервисы Cognee, UI и шлюза; `x-agent`: recall/capture, summary, журнал и таймауты памяти |
| [../pyproject.toml](../pyproject.toml) | Версии образов и зависимостей, включая Phoenix, OmniRoute, Cognee, Holmes, Grafana и kube-state-metrics |

Каждая роль хранится в `roles/<название_роли>.json`, например `roles/planner.json`.
Имя роли берётся из имени файла. В JSON лежат только её настройки (`agent`,
`instructions`, `tools` и другие поля), без внешнего ключа роли и поля `name`.

[Схема ролей](roles/index.html) показывает обязанности, инструменты, навыки и
разрешённые связи делегирования. Откройте HTML в браузере и выберите роль.

Модели выбираются в `.env` через `OMNIROUTE_FREE_MODEL`, `OMNIROUTE_CLOUD_MODEL`,
`HARNESS2_IMPROVER_MODEL` и `HOLMES_MODEL`.
`project_operator` и Holmes по умолчанию используют `ax-free`.
`tech_lead` использует `omniroute_cloud`, как остальные инженерные роли.
Он делегирует доступ к репозиторию и выполнение команд специалистам;
его собственные инструменты и разрешения остаются ограниченными координацией.

## Документация Context7

`tech_lead`, `backend_engineer`, `frontend_engineer`, `qa_engineer` и `code_reviewer`
получают HTTPS MCP `https://mcp.context7.com/mcp` с allowlist из
`resolve-library-id` и `query-docs`. По умолчанию используется публичный доступ без
ключа с лимитами Context7. Сервер помечен `optional: true`: ошибка подключения
или таймаут не останавливают остальные инструменты. Runtime сообщает об отсутствии
документации; следующая попытка discovery в той же живой сессии возможна после
`timeouts.mcp_discovery_seconds`. Обязательные MCP-серверы сохраняют прежнее поведение.

Агент сначала проверяет версию зависимости проекта, затем ищет библиотеку и
документацию для этой версии. Если нужной версии нет, он явно сообщает об этом и
проверяет установленные исходники или официальную документацию разрешёнными средствами.

Для собственного API-ключа добавьте его имя в `runtime.json → role_env_names`
и `env_names` нужных ролей, задайте значение в `.env`, затем добавьте
`"bearer_token_env": "CONTEXT7_API_KEY"` в их подключение Context7.
Значение ключа в JSON не записывается. При указанном `bearer_token_env` клиент
требует выбранный ключ и не переключается на анонимный доступ.

`limits.artifact_bytes` ограничивает суммарные исходные байты экспортируемых
артефактов (по умолчанию 1 000 000). Текст дополнительно ограничен суммарным
`limits.tool_output_chars`; подробности передачи — в
[AX operations](../ax_local/README.md#автономное-выполнение).

## Применение изменений

Все команды выполняются из корня репозитория; для AX нужен Python 3.11+.

**Phoenix** — секреты `PHOENIX_SECRET`, `PHOENIX_ADMIN_SECRET` и
`PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` задаются в корневом `.env`.
Настройки сервиса находятся в `evals/config.json`, образ с digest —
в `pyproject.toml → tool.popot.build.PHOENIX_IMAGE`.
`x-tracing` в том же JSON управляет постоянным экспортом всех рабочих ролей
в отдельный проект. После его изменения пересоберите API/worker через
`bash ax_local/rebuild-worker.sh`; подробности — в [гайде](../evals/README.md#постоянные-трейсы-ax).
Email для входа — `admin@localhost`; пароль — значение
`PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`. Это initial password: его изменение
в конфиге не сбрасывает пароль уже существующего пользователя.
Для прямого чтения Phoenix ролью `project_operator` используется отдельный
пользователь `VIEWER` и его API key в `PHOENIX_API_KEY`. Этот ключ передаётся
worker через `env_names`; `PHOENIX_ADMIN_SECRET` служит экспорту в API.
Встроенный MCP и его инструменты описаны в [AX operations](../ax_local/README.md#project-operator-единая-роль-для-анализа-проекта).

```bash
bash compose.sh --env-file .env -f evals/compose.yaml up -d
```

**Cognee** — после правок `memory/config.json → services`:

```bash
bash memory/up.sh
```

Оба Compose-файла подключают соседний `config.json` штатным `extends`.
Пароли остаются в корневом `.env`. Параметры интеграции агентов с Cognee,
включая таймауты `memory_*`, находятся в `memory/config.json → x-agent`.
После их изменения пересоберите worker/API/MCP через `bash ax_local/rebuild-worker.sh`
и начните новую сессию. Для прямых Python-запусков другой файл выбирается
через `POPOT_MEMORY_CONFIG` до старта процесса.
Общие `tool_evidence_chars` и `tool_evidence_count` остаются в `runtime.json → limits`:
они используются и для итоговых отчётов при отключённом Cognee.

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

Топология Docker и хранилища остаются в `ax_local/compose.yaml`,
`memory/compose*.yaml` и `evals/compose.yaml`. Порты и healthchecks памяти,
параметры Phoenix вынесены в соседние `config.json`; Grafana публикуется port-forward
на `127.0.0.1:3002`. PVC и данные сервисов сохраняются отдельно от JSON.
Определение панелей Grafana остаётся в `ax_local/cluster/resource_dashboard.py`.

Роль `project_operator` может менять только состав управляемых комбо и ресурсы
поддерживаемых deployment, а также перезапускать их. Она показывает план и
применяет его после `/apply change-...` в том же чате. При изменении ресурсов
оператор сохраняет остальные настройки сервисов в `monitoring.json`. Остальные
настройки редактируются пользователем; оператору доступны только перечисленные операции.

Каждая роль координирует задачу автоматически, включая дочерние роли; переключателя
enable/disable нет. Общие лимиты берутся из `runtime.json → workflow`; необязательный блок
`"coordination": {"turn_seconds": 900}` в роли переопределяет `max_turns`,
`total_seconds`, `turn_seconds`, `max_no_progress`, `verification_reserve_seconds`, `max_child_recoveries`. `max_concurrent` остаётся общим
лимитом корневых задач сервера. Дочерние координаторы наследуют deadline родителя.
Каждая роль делегирует только специалистам своего `allowed_roles`, согласно обязанностям; циклы запрещены,
глубина ограничена A2A. Инструменты и внешние разрешения ролей сохраняются.

`workflow.verification_reserve_seconds` задаёт резерв на проверку и исправления
внутри общего времени, по умолчанию 1800 секунд. Для коротких задач он ограничен
четвертью `total_seconds`: `min(verification_reserve_seconds, total_seconds / 4)`.
При стандартных 7200 секундах и 8 ходах первичная реализация получает окно до 5400-й секунды,
а оставшиеся 1800 секунд — независимые review/QA и исправления. Граница фиксируется
при создании workflow run и не сдвигается при продолжении; старые сохранённые
запуски без этой границы не получают её задним числом. Отдельный резерв
`model.final_answer_reserve_seconds` остаётся 120 секунд.

`workflow.max_child_recoveries` разрешает до двух восстановлений той же дочерней
сессии после лимита ходов в пределах исходного deadline. Для второй попытки нужны
новые квитанции проверок: изменившаяся ревизия, команда или результат. Повтор того
же теста с тем же результатом и новая формулировка отчёта не открывают попытку.
Лимиты отсутствия продвижения и внешние разрешения сохраняются.
Внутри хода по умолчанию доступны 60 раундов инструментов, верхняя граница роли —
120. MCP ждёт до 14400 секунд, включая очередь и подготовку AX; это не продлевает
двухчасовой deadline самой задачи. Роли с явными меньшими лимитами сохраняют их.
