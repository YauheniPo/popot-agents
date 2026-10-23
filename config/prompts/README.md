# Инструкции runtime

Тексты сообщений модели хранятся в обычных Markdown-файлах. Один файл — одно
сообщение или переиспользуемый блок. Правила можно редактировать без Python,
JSON-экранирования и дополнительных библиотек шаблонизации.

| Каталог | Что менять | Основные файлы |
| --- | --- | --- |
| `delegation/` | Выбор специалиста, передача контекста, продолжение задачи | [instructions.md](delegation/instructions.md), [handoff.md](delegation/handoff.md), [review_routing.md](delegation/review_routing.md) |
| `review/` | Проверка исходников, публикация, checkpoint и продолжение ревью | [source_and_publication.md](review/source_and_publication.md), [verification.md](review/verification.md), [checkpoint_instructions.md](review/checkpoint_instructions.md), [focus.md](review/focus.md) |
| `reporting/` | Финальный ответ, требования к evidence, исправление формата | [result_contract.md](reporting/result_contract.md), [final.md](reporting/final.md), [repair.md](reporting/repair.md) |
| `memory/` | Использование исторической памяти и извлечение новых записей | [lifecycle.md](memory/lifecycle.md), [extract.md](memory/extract.md) |
| `harness2/` | Планирование, улучшение инструкций и выбор результата | [planning.md](harness2/planning.md), [improver.md](harness2/improver.md), [selection.md](harness2/selection.md) |
| `skills/` | Общие правила использования подключённых skills | [references.md](skills/references.md) |
| `holmes/` | Диагностика AX и финальный отчёт Holmes | [diagnosis.md](holmes/diagnosis.md), [final_report.md](holmes/final_report.md) |

Инструкции конкретной роли по-прежнему находятся в `config/roles/<role>.json`.
Числовые лимиты и разрешения по-прежнему применяются настройками runtime/ролей
и проверками Python. Python
выбирает нужные сообщения, подставляет контекст, проверяет результаты и
управляет исполнением. Изменение текста не меняет разрешения инструментов,
схему JSON-ответа или условие остановки цикла. Технические описания аргументов
инструментов и диагностические сообщения остаются рядом с кодом их проверки.

## Подстановки

Поддерживаются только именованные маркеры `{{name}}`, например:

```markdown
You are {{role}}.
Available delegation capability:
{{description}}
```

Имена чувствительны к регистру и должны совпадать с аргументами в месте вызова
`render_prompt()`. Выражения, условия, циклы и включение файлов не поддерживаются.
Обычные JSON-скобки не требуют экранирования. Подставленный пользовательский текст
не обрабатывается повторно как шаблон.

| Шаблон | Параметры |
| --- | --- |
| `delegation/instructions` | `role`, `review_routing`, `description` |
| `delegation/handoff` | `handoff`, `context` |
| `reporting/final` | `result_contract` |
| `reporting/repair` | `reason`, `result_contract` |
| `memory/extract` | `max_memories` |
| `harness2/improver` | `comparison` |
| `harness2/selection` | `subject` |

Остальные файлы не содержат параметров. Отсутствующий или пустой файл, пропущенный
параметр и лишний аргумент вызывают явную ошибку; встроенной запасной копии текста нет.
Пробельные символы по краям файла удаляются, внутреннее Markdown-форматирование сохраняется.

## Проверка и применение

```bash
python3 -m pytest -q tests/test_prompts.py
bash ax_local/rebuild-worker.sh
```

Шаблоны включены в образы worker и API. После редактирования пересоберите локальный
runtime и начните новую задачу. Сохранённые сессии могут содержать старые сообщения
и результаты Harness2; перенос не переписывает их историю.

Holmes получает тексты в `settings.json` существующего ConfigMap при подготовке
нового исследования. Его внешний образ менять не нужно. Если исследование
запускается через API, сначала пересоберите локальный API командой выше.

`tests/fixtures/prompt_baseline.json` фиксирует тексты до переноса, игнорируя
различия в пробелах и переносах строк. При намеренном изменении политики обновляйте
соответствующую контрольную сумму вместе с проверками нового поведения; не
перегенерируйте весь baseline только ради зелёного теста.
