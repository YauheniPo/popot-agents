# Локальный вариант на AX

Эта папка запускает тот же HTTP harness и роли через AX `Task`, чтобы сравнить
его с Docker runner в текущей ветке. Исходный `compose.yaml` и Docker runner не
меняются. AX v0.3.1 и совместимый с ним Agent Substrate запускаются в
отдельном локальном кластере `kind-popot-ax` поверх Docker. Compose проект
`popot-ax-local` запускает PostgreSQL, AX API и MCP сервер. Для Claude и Codex
доступен `http://127.0.0.1:8003/mcp`; API на порту 8002 доступен только
контейнерам Compose.

## Где что находится

| Путь | Назначение |
| --- | --- |
| `config.json`, `config.py` | Настройки AX и их проверка при загрузке. |
| `api/server.py` | HTTP API, роли и хранение сессий. |
| `api/ax_runner.py` | Создание AX Task и обмен сообщениями через `ax ssh`. |
| `api/provider_proxy.py` | Доступ Task к выбранному провайдеру без передачи его ключа в Task. |
| `worker/` | Код, выполняемый внутри AX Task. |
| `cluster/` | Подготовка kubeconfig для API контейнера. |
| `docker/` | Dockerfile и списки файлов для сборки API и Task образов. |
| `tests/` | Локальные тесты AX варианта. |

`up.sh` — единая команда для первого и повторного запуска. Файлы `server.py` и
`container_kubeconfig.py` в корне папки сохраняют прежние команды как короткие
переходники к новым модулям.

По умолчанию используются `openrouter` и `openrouter/free` из
`ax_local/config.json`. Для смены модели или провайдера задайте
`AX_LOCAL_PROVIDER` и `AX_LOCAL_MODEL` при запуске. Провайдер берётся из
`config/agents.json`: поддерживаются профили с OpenAI-совместимым HTTP API,
например `openrouter`, `ollama`, `ollama_cloud`, `nous` и `nvidia_nim`.
CLI профили `ollama_claude` и `ollama_claude_local` для AX HTTP harness не подходят.
Все роли из `config/roles.json` сохраняют инструкции и инструменты, но работают
через выбранные провайдер и модель. AX v0.3.1 поддерживает только литералы в
`Task.env`, поэтому ключ провайдера остаётся в API контейнере. В AX Task
передаётся временный токен для ограниченного прокси модели внутри локальной
Docker сети `kind`.

`ax_local/config.json` также задаёт AX context и atespace, имя workspace,
ресурсы и режим debug для Task, таймаут маршрута AX, интервалы ожидания запуска, backend сессий,
порт и ограничения прокси модели. Общие настройки ролей и инструментов остаются в `config/roles.json`,
а общие лимиты и таймауты — в `config/runtime.json`. При запуске через Compose
после правки AX конфига повторите команду запуска ниже. При смене AX context
нужен отдельный локальный кластер; для другого atespace заранее нужен
WorkerPool в соответствующем пространстве.

## Подготовка

Нужны работающий Docker Desktop, `git`, Go 1.27.1+, `kubectl` и Python 3.10+.
Для выбранного провайдера подготовьте его ключ в `.env`, если он требуется.
При отсутствии `ko` bootstrap установит
v0.19.1 в `ax_local/.local/bin`; `kind` ставит скрипт Agent Substrate.
Первый запуск скачивает исходники и контейнерные образы и может занять время.

Из корня репозитория:

```bash
bash ax_local/up.sh
```

При первом запуске команда подготавливает локальный кластер, собирает worker и
поднимает PostgreSQL, API и MCP через Compose. При повторном запуске она
пересобирает worker, обновляет его в локальном registry и пересоздаёт API/MCP.
Используется `POPOT_DB_PASSWORD` из существующего `.env`. Все команды выполняются
через эту точку входа. Она также запускает остановленные локальные контейнеры
`kind-registry` и `popot-ax-control-plane` и выставляет таймаут
`atenet-router` из `ax.route_timeout_seconds` (по умолчанию 330 секунд).
Для настройки роутера скрипт вызывает `kubectl` с явными путём к локальному
`ax_local/.local/kubeconfig` и контекстом `kind-popot-ax`; рабочий контекст
пользователя не меняется.

Для другого провайдера укажите обе переменные и его ключ в `.env`, если нужен.
Например, `nous` использует `NOUS_API_KEY`, а `nvidia_nim` — `NVIDIA_API_KEY`:

```bash
AX_LOCAL_PROVIDER=nous AX_LOCAL_MODEL=ИМЯ_МОДЕЛИ \
  bash ax_local/up.sh
```

Для провайдера по умолчанию можно задать только `AX_LOCAL_MODEL`.
`OLLAMA_MODEL` здесь не используется. Python пакет на хосте устанавливать не
нужно. Команда запуска сама готовит kubeconfig для API контейнера при первом
старте.

Ранее запущенный на хосте `python3 -m ax_local.server` можно остановить через
`Ctrl+C`: Compose запускает собственный API контейнер.

Bootstrap фиксирует AX на `v0.3.1`, Substrate — на commit из его `go.mod`,
строит runner под архитектуру машины и сохраняет digest образа в
`ax_local/.local/worker-image`. Он использует свой `KUBECONFIG` и готовит
копию для API контейнера. API контейнер подключается к Docker сети `kind` и
к Kubernetes API узла `popot-ax-control-plane`, без Docker socket. Если имя `popot-ax` или
`kind-registry` занято несовместимым локальным ресурсом, скрипт останавливается.
Состояние `.local/` игнорируется Git.

Прокси в API контейнере использует endpoint из профиля и принимает запросы
только с токеном текущего запуска. Для локального Ollama проверьте, что он
слушает адрес, доступный Docker контейнерам через `host.docker.internal`.

## Проверка и сравнение

Проверьте контейнеры и вызов существующего MCP инструмента:

```bash
docker compose --env-file .env -f ax_local/compose.yaml ps db api mcp-server
curl --fail-with-body -sS http://127.0.0.1:8003/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"run_task","arguments":{"role":"local_analyst","task":"Сколько будет 17 * 19?"}}}'
```

Логи API и MCP при проверке:

```bash
docker compose --env-file .env -f ax_local/compose.yaml logs -f api mcp-server
```

Первый `run_task` ждёт готовности AX Task до `ax.startup_timeout_seconds` из
`ax_local/config.json`; `curl` до ответа ничего не выводит. Если ожидание
затянулось, проверяйте фазу Task в другом терминале, пока запрос выполняется:

```bash
docker compose --env-file .env -f ax_local/compose.yaml exec -T api \
  ax --context=kind-popot-ax --atespace=ate-demo-counter get tasks
docker compose --env-file .env -f ax_local/compose.yaml logs --since=10m api \
  | rg 'AX task|POST /tasks'
```

После ошибки API удаляет AX Task, поэтому поздний `get tasks` может вернуть
пустой список. В сообщении об ошибке и логах API остаются последняя фаза и
причина из статуса AX; исходные значения окружения Task не выводятся.

Подключение клиентов:

```bash
codex mcp add popot-ax --url http://127.0.0.1:8003/mcp
claude mcp add --transport http popot-ax http://127.0.0.1:8003/mcp
```

Для сравнения запустите текущий Docker вариант через
`docker compose up --build -d mcp-server` и подключите его MCP endpoint на
`http://127.0.0.1:8001/mcp`. Используйте одинаковую роль, провайдера и модель.
MCP инструменты `run_task`, `send_message`, `list_chats` и
`get_chat` одинаковы для обоих вариантов. Сохраните `sessionId` из ответа и
сравните первый ответ, продолжение чата, восстановление после перезапуска,
ошибки модели и работу инструментов. AX использует отдельную базу сессий.
Поле `container` в AX ответе содержит имя AX Task; `workspace` будет `null`,
потому что AX workspace находится внутри actor.
После смены `AX_LOCAL_PROVIDER` создавайте новый чат: сохранённые сессии
предыдущего провайдера доступны при возврате к его конфигурации.

## Ограничения опыта

- Адаптер вызывает `ax ssh` для каждого сообщения. Для длинной истории он
  передаёт запрос несколькими частями, так как AX v0.3.1 не пересылает stdin
  в `ax ssh`. Это проверяет AX Task lifecycle, но добавляет накладные расходы
  CLI и туннеля к задержке ответа.
- При idle timeout и удалении чата API удаляет AX Task и восстанавливает
  историю из PostgreSQL при следующем запросе, как Docker вариант. Файлы AX
  workspace после удаления Task не обещают сохраниться; отдельно сравнивайте
  сценарий с файлами до удаления задачи.
- Используется demo WorkerPool Agent Substrate `ate-demo-counter`, чтобы AX
  Task получил локальную ёмкость. Кластер служит только для разработки.
- Этот вариант не переносит существующие сессии из Docker базы и не меняет
  текущий `compose.yaml`.

Источники: [AX v0.3.1](https://github.com/google/ax/tree/v0.3.1),
[контракт runner](https://github.com/google/ax/blob/v0.3.1/docs/runner.md),
[локальный kind для Agent Substrate](https://github.com/agent-substrate/substrate/blob/main/README.md#quickstart-development).
