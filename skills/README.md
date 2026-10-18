# Навыки для агентов

Импортированы 12 навыков из [mattpocock/skills](https://github.com/mattpocock/skills/tree/b0618bc436ad893b3c5e84e55fba86586d34a404/skills)
на коммите `b0618bc436ad893b3c5e84e55fba86586d34a404` (проверен `main` 8 октября 2026).
Исходные файлы скопированы без изменений вместе со
справочниками, шаблонами и `agents/`. Лицензия: [MIT](LICENSE.mattpocock).
Версия и SHA-256 каждого файла: [upstream.json](upstream.json).

## Подборка по ролям

Роли сверены с `config/roles.json`. Один навык может использоваться несколькими
ролями; достаточно одной копии его каталога. Конкретные назначения заданы в `config/roles.json`.

| Каталог в исходном репозитории | Роли | Назначение |
| --- | --- | --- |
| `skills/productivity/grilling` | `planner`, `product_manager`, `product_designer`, `data_analyst`, `analyst`, `local_analyst` | Уточнение требований, решений и предположений |
| `skills/productivity/handoff` | `planner`, `product_manager`, `tech_lead`, `backend_engineer`, `frontend_engineer`, `code_reviewer`, `data_analyst` | Передача контекста следующему агенту |
| `skills/engineering/grill-with-docs` | `planner` | Обсуждение с фиксацией общего словаря и архитектурных решений |
| `skills/engineering/domain-modeling` | `planner`, `product_manager`, `tech_lead`, `data_analyst` | Общий словарь предметной области и проверка модели на сценариях |
| `skills/engineering/to-spec` | `planner`, `product_manager`, `tech_lead` | Спецификация, пользовательские истории и решения по тестированию |
| `skills/engineering/to-tickets` | `planner`, `product_manager`, `tech_lead` | Декомпозиция работы с зависимостями |
| `skills/engineering/prototype` | `product_designer`, `frontend_engineer` | Проверка вариантов интерфейса и моделей состояния прототипами |
| `skills/engineering/codebase-design` | `tech_lead`, `backend_engineer`, `frontend_engineer`, `code_reviewer`, `qa_engineer` | Границы модулей, интерфейсы и тестируемость |
| `skills/engineering/tdd` | `backend_engineer`, `frontend_engineer`, `code_reviewer`, `qa_engineer` | Тестирование поведения через публичные интерфейсы |
| `skills/engineering/diagnosing-bugs` | `backend_engineer`, `frontend_engineer`, `code_reviewer`, `qa_engineer` | Воспроизведение и диагностика ошибок, регрессионная проверка |
| `skills/engineering/code-review` | `planner`, `tech_lead`, `backend_engineer`, `frontend_engineer`, `code_reviewer`, `qa_engineer` | Проверка изменений по требованиям и стандартам кода |
| `skills/engineering/setup-matt-pocock-skills` | `planner`, `product_manager`, `tech_lead`, `backend_engineer`, `frontend_engineer`, `qa_engineer` | Справочники трекера и доменной документации |

Для `chat` и `cron_demo` специальные навыки из этого набора не нужны.

Дополнительно импортирован `engineering/setup-matt-pocock-skills`: на него
ссылаются навыки спецификации, декомпозиции и ревью. Это справочник настройки
трекера и доменной документации; добавление в список не запускает настройку.

Для `planner` выбран набор из восьми навыков. `grill-with-docs` использует
`grilling` и `domain-modeling`, поэтому оба включены в его prompt вместе с
шаблонами словаря и ADR. `to-spec` и `to-tickets` дают спецификацию и задачи с
зависимостями; `code-review` — две оси проверки (стандарты и соответствие spec),
`handoff` — передачу контекста. В роли уже задан локальный режим drafts в
`/workspace/specs`; справочники setup не запускают настройку репозитория или
публикацию в трекер. Внешняя публикация требует разрешения пользователя.

`implement-spec` не подключён: он предполагает общий репозиторий, worktree и
слияние веток. `wayfinder` полезен для больших исследований на много сессий,
но его полный сценарий требует research/prototype-процессов, доступного всем
участникам трекера и отдельных решений с пользователем. Это отдельный режим,
а не обязательный шаг каждой задачи planner. `ask-matt` — маршрутизатор всей
подборки автора; здесь маршрутизацию задают роль и каталог `delegate_task`.

## Конфигурация

В `config/roles.json` укажите пути каталогов относительно `skills/`:

```json
{
  "skills": ["engineering/codebase-design", "engineering/tdd"]
}
```

Это фрагмент роли; остальные поля (`agent`, `instructions`, `tools` и т. д.)
остаются обязательными по существующей схеме. Отсутствие `skills` или `[]`
означает отсутствие дополнительных навыков. Абсолютные пути, `..`, повторы,
символические ссылки и каталоги без непустого `SKILL.md` отклоняются.

При старте worker читает выбранные `SKILL.md` и все Markdown-справочники внутри
их каталогов. Они добавляются к инструкциям роли: для HTTP — в system message,
для CLI — в префикс запроса. Это позволяет читать справочники даже ролям без
файловых инструментов. Скрипты и YAML остаются в образе, но автоматически
не исполняются и не добавляются в prompt.

Лимит суммарного текста навыков, включая заголовки загрузчика:
`limits.skill_prompt_bytes` в `config/runtime.json`, сейчас 262144 байта.
Превышение вызывает явную ошибку; текст не обрезается. Большие списки увеличивают
контекст каждого запроса к модели. Событие `skills_loaded` в stderr контейнера
показывает список и размер загруженного текста.

Загрузчик не добавляет Skill tool. Ссылки на уже включённые навыки означают
использование текста в контексте. Если зависимого навыка нет в выбранном списке,
нужно добавить его явно. Каталог остальных навыков не загружается автоматически.

## Возможности ролей

Навыки не расширяют `tools`, `permissions` или `allowed_roles`. Правила роли
и разрешения пользователя имеют приоритет. Навыки с `disable-model-invocation`
предназначены для явного запроса пользователя. Доступ к трекеру, запуск тестов,
создание файлов и делегирование возможны только при наличии соответствующих
инструментов. Например, у `product_designer`, `frontend_engineer` и `qa_engineer`
сейчас нет файловых инструментов: они могут использовать методику, но не
создавать прототипы и не запускать тесты самостоятельно.

## Обновление и запуск

Каталог копируется в `/app/skills` образов API и worker. Путь к локальному клону
автора на хосте контейнерам не нужен.

AX (из корня репозитория):

```bash
bash ax_local/rebuild-worker.sh
```

Работающий worker сохраняет снимок инструкций до остановки. Для изменения
списка навыков роли начните новую сессию. При восстановлении старой сессии
список берётся из сохранённого `roleConfig`, а тексты — из образа нового worker.
Старые сессии без `skills` продолжают работать без них.

Для обновления исходников скопируйте целиком нужные каталоги из выбранного
коммита upstream, сохраните лицензию и обновите `upstream.json`. Проверяйте
относительные ссылки и зависимости. Не запускайте входящие скрипты при импорте.
Целостность импорта и набор planner проверяются командой
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_skill_import tests.test_planner_role`.
