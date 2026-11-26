# Документация {{project.name}}

<!-- harness:begin core -->
## Методология разработки

Правила, процессы и проверки приходят с комплектом harness и одинаковы во всех проектах на нем.

| Документ | О чем |
| --- | --- |
| [harness](platform/agents/harness.md) | установленный слой: ядро и модули, настройка, роли файлов, приемка, обновление |
| [параметры harness](platform/agents/harness-parameters.md) | манифест проекта, модули и их настройки, внешние зависимости, настройки поставщика кода |
| [конвейеры и гейты](platform/delivery/ci-cd.md) | Check, гейт проекта, релиз, раннеры, исполнители агентов, события GitLab, проходы ревью, агент задач, разбор алертов |
{{#if module:release/tag-flow}}| [выкладка](platform/delivery/deploy.md) | контракт сборки и выкладки проекта |
{{/if}}{{#if module:alert-triage}}| [алерты](platform/operations/alerting.md) | правила, маршрутизация и цели надежности проекта |
{{/if}}| [дашборды](platform/operations/dashboards.md) | панели и источники данных проекта |
| [локальный стек](platform/operations/local-stack.md) | подготовка, запуск и проверка окружения проекта |
| [карта областей](platform/architecture/area-map.md) | сопоставление пути с областью и потребители общего слоя |
| [архитектурные гейты](platform/architecture/gates.md) | границы слоев и проверки по стекам |
| [окружение агента](platform/agents/environment.md) | подготовка сессии агента и раннера |
| [постановка исполнителю](platform/agents/delegated-task.md) | разделы задания координатора агенту-исполнителю |
| [промпты исполнителей](platform/agents/prompts.md) | где лежат промпты, как конвейер их собирает и где проверяется ответ, как проект добавляет свои правила |
| [шаблоны публикуемых текстов](platform/agents/templates.md) | где лежат шаблоны, как они рендерятся и как добавить новый |
| [термины harness](platform/agents/terms.md) | значения понятий, которыми пользуются правила, промпты и сообщения harness |
| [SDD workflow](platform/delivery/sdd.md) | конвейер Spec Kit, артефакты трека, verify |
| [стандарт документации](platform/standards/documentation.md) | где живет какой документ и как он пишется |
| [корневые документы](platform/standards/root-entry-docs.md) | `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` |
| [текст для человека](platform/standards/text.md) | сообщения, ошибки, PR, логи и правила письма для всех стеков |
| [слой комментариев](platform/standards/code-comments.md) | когда комментарий нужен и как его проверяет гейт |
| [глоссарий](platform/standards/product-terms.md) | одно понятие - один термин во всех текстах проекта |
| [критерий полезности тестов](platform/standards/test-usefulness.md) | какой тест пишется, а какой нет |
| [решения (ADR)](platform/adr/README.md) | реестр архитектурных решений |
| [фичи](features/README.md) | индекс продуктовых фич |

Реестр спек - [`specs/README.md`](../specs/README.md), конституция SDD -
[`.specify/memory/constitution.md`](../.specify/memory/constitution.md).
<!-- harness:end core -->

<!-- project:begin -->
## Приложения

| Каталог | Стек | Документ | Гейт |
| --- | --- | --- | --- |
{{harness.appRows}}

Стек, команды разработки и карта архитектуры каждого приложения - в его документе.
<!-- project:end -->
