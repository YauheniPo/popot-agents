# Baseline HTTP harness

Опциональные живые прогоны AX с настоящей моделью. Runner и оценщик находятся
отдельно от runtime: они не меняют рабочие роли, основной API или harness.
Результаты проверяются по файлам, SHA-256 и наблюдаемым событиям. Модель-судья
не используется.

## Настройки

[config.json](config.json) содержит три части:

- `services.phoenix` — параметры сервиса Phoenix.
- `x-tracing` — постоянный экспорт обычных задач AX в проект `popot-runtime`.
- `x-baseline` — модель, число повторов, бюджеты workflow, лимит допуска
  корневых задач и HTTP-таймауты runner. Префикс `x-` — штатное расширение
  Compose: Docker пропускает эту секцию, runner читает и проверяет её.

`--model` и `--repetitions` переопределяют значения JSON для конкретного запуска.
Допустимы 1–3 повтора. `request_timeout_seconds` должен превышать
`workflow.total_seconds`. Runner выполняет сценарии последовательно;
`max_concurrent` задаёт допуск сервера, не запускает сценарии параллельно.
После изменения JSON снова скопируйте `evals` в API-контейнер перед новым запуском.
В каталоге результата отдельный `config.json` сохраняет фактические настройки,
включая CLI-переопределения, снимки ролей и SHA-256 исходной конфигурации.

Набор сценариев задаётся в [cases.json](cases.json), `--case` выбирает поднабор.
Параметры LLM-запросов берутся из профилей [../config/agents.json](../config/agents.json),
права и инструменты — из исходных ролей, настройки sandbox и WorkerPool —
из [../ax_local/config.json](../ax_local/config.json). Изоляция Cognee и
отключение Harness² остаются частью фиксированного протокола ниже.

## Постоянные трейсы AX

`x-tracing.enabled` включает экспорт всех выполняемых root-задач, делегированных
ролей и cron без sampling и независимо от открытого клиента. Отдельный
`x-tracing.project` задаёт один постоянный проект для рабочих агентов.
Baseline использует свой API и сохраняет прежний отдельный импорт экспериментов.

Root и дочерние роли представлены `AGENT` spans, реальные вызовы native/MCP tools —
`TOOL`. `LLM` spans создаются в защищённом API-прокси по фактическому ответу
провайдера: модель, HTTP-статус и числовой usage, включая запросы памяти и Harness².
Отсутствующий usage остаётся неизвестным. Текст запросов/ответов, reasoning,
аргументы tools и секреты в этот экспорт не входят. Времена tools наблюдаются
на стороне API; LLM длительность измеряется в прокси.

Phoenix Session объединяет всю задачу: `session.id = popot.root_session_id`
у корневого агента, всех дочерних ролей и их LLM/TOOL spans.
ID конкретного агента сохраняется в `popot.agent_session_id`, а его родителя —
в `popot.parent_session_id`. Когда родитель уже завершён или API перезапущен,
дочерняя роль получает root ID из сохранённых метаданных делегирования.
Продолжения могут создавать новые traces; общий `session.id` собирает их в одну
Session. Это группировка диалога с данным root ID; новый root ID создаёт другую Session.
Старые spans автоматически не перегруппировываются.

Атрибуты `popot.role` и `popot.agent_profile` различают роли и профили.
`popot.skills.configured` показывает конфигурацию роли; `popot.skills.loaded` — инструкции, загрузку
которых подтвердил worker. Успешный `read_file` по пути выбранного skill получает
`popot.skill.reference_read`. Skills — инструкции: эти метки не доказывают
выполнение моделью всех требований skill. Новая сессия после rebuild получает
актуальную загрузку skills; старые worker могут не отправлять эту метку.

В Phoenix откройте Projects → `popot-runtime` → Sessions и выберите root ID,
чтобы увидеть все traces задачи. Traces покажет иерархию ролей и вызовов,
Dashboard — стандартные графики latency, ошибок, LLM/tools и токенов.
Spans по умолчанию использует фильтр `parent_id is None`. Для наблюдения за
работающими агентами очистите его: AGENT span отправляется после завершения роли,
а закрытые LLM/TOOL spans поступают во время её работы и уже относятся к общей Session.
Это не таблица CODE evaluations baseline. Специализированные графики по skills
не создаются автоматически; их имена доступны в атрибутах spans.

Роль `project_operator` читает Phoenix напрямую из worker через встроенный MCP
`/mcp` с отдельным пользовательским ключом `VIEWER` в `PHOENIX_API_KEY`.
Ей доступны изучение GraphQL-схемы и выполнение запросов чтения. Сервис работает
с `PHOENIX_ENABLE_MCP_CODE_MODE=false`, чтобы роль выбирала отдельные инструменты.
Настройка и правила анализа описаны в [AX operations](../ax_local/README.md#project-operator-единая-роль-для-анализа-проекта).

Секрет берётся из корневого `.env` по имени `x-tracing.api_key_env`; он остаётся
в API-контейнере. Из API Phoenix доступен через `x-tracing.endpoint`.
Сетевая отправка идёт отдельным потоком. Закрытые spans сначала сохраняются
в `x-tracing.outbox_directory`, named volume `phoenix-outbox`. После ошибки
отправка повторяется через `retry_seconds`; очередь переживает перезапуск API.
Не удаляйте volume при пересоздании контейнеров. Повтор после потерянного ACK
не создаёт второй span. Невозможность записи на диск отмечается событием
`phoenix_export_failed`; работа агента продолжается. Незавершённые spans при
аварийном завершении процесса не восстанавливаются из памяти.

Применить код и настройки:

```bash
bash compose.sh --env-file .env -f evals/compose.yaml up -d
bash ax_local/rebuild-worker.sh
```

Ошибки доставки видны в логах API как `phoenix_export_failed` без payload и ключей.
Все span-данные в очереди содержат только метаданные; файлы создаются с правами `0600`.

## Протокол

- По умолчанию шесть сценариев из [cases.json](cases.json), по два повтора, последовательно.
- По умолчанию закреплён прямой маршрут `ollama-cloud/deepseek-v4.1-flash:1017`.
  Параметры запросов сохраняются из исходных профилей ролей.
- Контролируемый локальный набор инструментов: native tools, локальный Git
  и разрешённое делегирование. GitHub/Fetch/Context7 MCP исключены из
  экспериментальных снимков: исходный пробный запуск остановился на недоступном
  GitHub discovery до первого LLM-запроса. Harness² выключен для сравнения
  основных исполнителей без дополнительного оптимизатора.
- Cognee MCP и переменные `COGNEE_API_URL`/`COGNEE_PASSWORD` удалены из снимков
  всех ролей, включая дочерние. Это также выключает автоматические recall,
  capture и журнал Cognee. Новые `baseline-*` brains не создаются.
  Рабочие роли проекта сохраняют свою память. Метка `[project: baseline-...]`
  остаётся идентификатором сценария без обращения к Cognee.
- Workflow по умолчанию: 600 секунд, два coordination turns по 300 секунд, резерв проверки
  60 секунд. Используются штатные WorkerQueue/WorkerPool с лимитом из
  `ax_local/config.json`; одновременно выполняется один корневой сценарий.
- Не изменять harness или сценарии посередине измерения. Версии и SHA-256
  исходников, ролей, skills и конфигурации сохраняются в `config.json`.

Текущий протокол помечен `memory_mode: disabled` в конфигурации и Phoenix.
Исторический `baseline-20261111` выполнен с включённой памятью; его результаты
сохранены отдельно. Для сравнения harness используйте новую серию без памяти
на обоих исполнителях. Проверки самой памяти требуют отдельной серии с
изолированным хранилищем Cognee.

`passed` означает выполнение функциональных проверок и контракта outcome.
`functional_passed` показывает проверки результата отдельно от состояния
workflow. `false_completion` фиксирует `completed` при проваленной объективной
проверке. Ожидаемый `blocked` в сценарии отсутствующего задания — успех.
Все корректно измеренные прогоны, включая неудачи агентов, остаются в знаменателе.
При ошибке самого оценщика серия останавливается: исходный отчёт сохраняется,
незавершённые измерения учитываются отдельно. Пустые метрики не заменяются нулём.

## Выполнение

Нужен уже работающий локальный AX и доступ к указанной модели через OmniRoute.
Команды выполняются из корня проекта. Идентификатор каталога должен быть новым.

```bash
docker exec popot-ax-local-api-1 python -c "from pathlib import Path; Path('/app/evals').mkdir(exist_ok=True)"
docker cp evals/. popot-ax-local-api-1:/app/evals/
git rev-parse HEAD
docker exec -i popot-ax-local-api-1 python -u -m evals.baseline --output /tmp/baseline-unique-id --repetitions 2 --source-revision <HEAD_FROM_PREVIOUS_COMMAND>
docker cp popot-ax-local-api-1:/tmp/baseline-unique-id reports/evals/
```

Прогоны создают собственный API/proxy/A2A процесс с отдельным файловым session
store. Worker удаляются после сохранения результатов. `sessions/` и JSONL
сохраняют доказательства дочерних результатов и восстановления.
Остановленный или неудачный запуск не переигрывается автоматически.
Для продолжения после устранения ошибки сбора используйте новый каталог,
`--repetition-start` и повторяемый `--case`, сохранив исходный batch и причину
исключения измерения. Не заменяйте реальные неудачи агентов удачными повторами.
Точные wire-кадры сохраняются в приватных `*.wire.ndjson` с правами `0600`;
JSONL для анализа исключает reasoning и повторно маскирует поля событий.

### Метрики шлюза

```bash
docker cp evals/gateway.mjs popot-ax-local-omniroute-1:/tmp/baseline-gateway.mjs
docker cp reports/evals/baseline-unique-id/report.json popot-ax-local-omniroute-1:/tmp/baseline-report.json
docker exec popot-ax-local-omniroute-1 node /tmp/baseline-gateway.mjs /tmp/baseline-report.json > reports/evals/baseline-unique-id/gateway.json
python3 -m evals.report reports/evals/baseline-unique-id reports/evals/baseline-unique-id/gateway.json
```

Экспортируются только вызовы с root session ID данного baseline. Коллектор не
выгружает заголовки, запросы и ответы. Токены берутся из фактического usage;
`tokens_known` — сумма найденного usage, `tokens` остаётся неизвестным при
ошибке или незавершённом model request. Это учёт наблюдаемого расхода, не счёт
провайдера. Неизвестная стоимость остаётся неизвестной. `summary.md` разделяет
функциональные проверки и завершение workflow.

## Phoenix

Образ Phoenix с версией и digest закреплён в
[../pyproject.toml](../pyproject.toml), `tool.popot.build.PHOENIX_IMAGE`.
Настройки сервиса находятся в [config.json](config.json): публикация порта,
авторизация, телеметрия и лимит памяти. [compose.yaml](compose.yaml) подключает
их штатным `extends`, без отдельного загрузчика. Endpoint публикуется только на
`127.0.0.1:6006`; аутентификация включена, данные находятся в named volume.
Секреты хранятся в общем корневом `.env`, исключённом из Git, с правами `0600`.
Пример переменных — в [../.env.example](../.env.example). Требуются три разных случайных
значения: `PHOENIX_SECRET`, `PHOENIX_ADMIN_SECRET` и
`PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`.
Email для входа — `admin@localhost`, пароль — значение
`PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD`. Для существующего хранилища изменение
initial password в `.env` не сбрасывает пароль пользователя.

```bash
bash compose.sh --env-file .env -f evals/compose.yaml up -d
docker exec popot-evals-phoenix-1 python -c "from pathlib import Path; Path('/tmp/popot-eval-code').mkdir(exist_ok=True)"
docker cp evals popot-evals-phoenix-1:/tmp/popot-eval-code/
docker cp reports/evals/baseline-unique-id popot-evals-phoenix-1:/tmp/
docker exec -i -w /tmp/popot-eval-code popot-evals-phoenix-1 python -u -m evals.phoenix /tmp/baseline-unique-id
docker cp popot-evals-phoenix-1:/tmp/baseline-unique-id/phoenix.json reports/evals/baseline-unique-id/phoenix.json
```

SDK уже установлен в закреплённом Phoenix image; зависимости рабочего
приложения не меняются. [phoenix.py](phoenix.py) сохраняет dataset version,
experiment, runs и CODE evaluations, связывая каждый прогон с trace ID.
`phoenix.json` — журнал успешного импорта; повторный вызов продолжает импорт,
не запускает агентов повторно. Конфигурация должна совпадать с журналом.
Для качества смотрите оценки `success`, `functional_success` и
`false_completion`: число successful runs в Phoenix означает успешную запись
результата эксперимента, включая измеренные неудачи агентов.
Расход токенов в CODE evaluations называется `total_tokens`: имя `tokens`
совпадает со встроенной колонкой Phoenix 21 и ломает List view сравнения.
Поле `tokens` в локальном отчёте и output прогона сохраняет прежнее имя.

Трейсы каждого эксперимента сохраняются в его собственный проект `Experiment-*`,
который Phoenix исключает из общего списка Projects. Пустой проект `default`
не содержит baseline. Открывайте связанный trace из результатов эксперимента.
Dashboard проекта показывает данные трейсов и их аннотаций; оценки `success`
и `functional_success` записаны как CODE evaluations эксперимента и доступны
в его сравнении, а не как аннотации spans на dashboard.

Spans восстанавливаются после выполнения: модельные запросы используют время,
usage и модель из OmniRoute; инструменты и сессии — время наблюдения API.
Границы незавершённых операций явно помечены. Исходные JSONL сохраняются.
Это не автоматическая OpenTelemetry-инструментация всех функций runtime.

## Проверки оценщика

```bash
python3 -m pytest -q tests/test_baseline.py
node --test tests/baseline-gateway.mjs
```

Контракт экспорта проверяется с SDK из закреплённого Phoenix image, без
обращений к модели. Сначала скопируйте `evals` по инструкции выше, затем:

```bash
docker exec popot-evals-phoenix-1 python -c "from pathlib import Path; Path('/tmp/popot-eval-tests').mkdir(exist_ok=True)"
docker cp tests/test_phoenix_export.py popot-evals-phoenix-1:/tmp/popot-eval-tests/
docker exec -w /tmp/popot-eval-code popot-evals-phoenix-1 python -m unittest discover -s /tmp/popot-eval-tests -p test_phoenix_export.py -v
```

Небольшое число повторов даёт начальную точку сравнения, но не доказывает статистическое
превосходство модели или harness. Проверки внешнего GitHub, падения процесса
API, переключения моделей и публикации PR в этот набор не входят.
