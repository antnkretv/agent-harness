# Документация {{project.name}}

Документы методологии harness - конвейеры, SDD, шаблоны, промпты, окружение и параметры - перечисляет
`.harness/docs/README.md`, их ставит `python3 .harness/ah.py install`. Ниже - документы проекта.

<!-- project:begin -->
## Стандарты и правила

| Документ | О чем |
| --- | --- |
| [текст для человека](platform/standards/text.md) | сообщения, ошибки, PR, логи и правила письма для всех стеков |
| [стандарт документации](platform/standards/documentation.md) | где живет какой документ и как он пишется |
| [слой комментариев](platform/standards/code-comments.md) | когда комментарий нужен и как его проверяет гейт |
| [критерий полезности тестов](platform/standards/test-usefulness.md) | какой тест пишется, а какой нет |
| [глоссарий](platform/standards/product-terms.md) | одно понятие - один термин во всех текстах проекта |
| [архитектурные гейты](platform/architecture/gates.md) | границы слоев и проверки по стекам |
| [карта областей](platform/architecture/area-map.md) | сопоставление пути с областью и потребители общего слоя |
{{#if module:release/tag-flow}}| [выкладка](platform/delivery/deploy.md) | контракт сборки и выкладки проекта |
{{/if}}{{#if module:alert-triage}}| [алерты](platform/operations/alerting.md) | правила, маршрутизация и цели надежности проекта |
{{/if}}| [дашборды](platform/operations/dashboards.md) | панели и источники данных проекта |
| [локальный стек](platform/operations/local-stack.md) | подготовка, запуск и проверка окружения проекта |
| [решения (ADR)](platform/adr/README.md) | реестр архитектурных решений |
| [фичи](features/README.md) | индекс продуктовых фич |

Реестр спек - [`specs/README.md`](../specs/README.md), конституция SDD -
[`.specify/memory/constitution.md`](../.specify/memory/constitution.md).

## Приложения

| Каталог | Стек | Документ | Гейт |
| --- | --- | --- | --- |
{{harness.appRows}}

Стек, команды разработки и карта архитектуры каждого приложения - в его документе.
<!-- project:end -->
