# Скиллы комплекта

Скилл - инструкция под задачу определенного рода. Правило одно: при релевантной задаче агент **читает и следует** указанному `SKILL.md` (явная ссылка пользователя тоже ок).

Все скиллы проекта живут в одном экземпляре в `.agents/skills/`, независимо от области и используемого ИИ-агента. Точка входа агента - [AGENTS.md](../AGENTS.md); правила письма для документации - [standards/documentation](../../docs/platform/standards/documentation.md).

Каталог не привязан к конкретному агенту. Проектные правила перечислены в разделе `rules.own` манифеста [`.harness/project/manifest.json`](../project/manifest.json) ([как снять правило](../README.md#как-снять-правило)); скилл из раздела "Проектные скиллы" без записи в реестре отклоняет `check-docs.py --area agents`.

Скиллы методологии приходят с комплектом harness и обновляются вместе с ним: факты проекта они не повторяют, а читают из манифеста [`.harness/project/manifest.json`](../project/manifest.json) и карты областей [`.harness/project/area-map.json`](../project/area-map.json). Свое правило пишется в скилл проекта или в `docs/`, а не дописывается в тело скилла методологии. Контракт слоя - [`.harness/README.md`](../README.md).

## Вендорные скиллы

Тело обновляется поставкой и локальных добавлений не сохраняет.

### Spec Kit

Поставка - `specify init --here --integration claude --script sh` версии из `.specify/init-options.json` в чистом каталоге, дальше трехстороннее слияние. Конвейер и два трека - [docs/platform/delivery/sdd.md](sdd.md). Надстройка `speckit-market` (рыночная сверка перед `clarify`) вендорной не является и перечислена среди проектных. Там же `speckit-clarify`: в нем снят лимит поставочного тела в пять вопросов, обновление поставки по нему - слияние, а не накат. Слиянием же обновляются адаптированные шаблоны `plan`, `spec` и `tasks` в `.specify/templates/`, шаг `clarify` в `.specify/workflows/speckit/workflow.yml`, строка про `feature.json` в `.specify/.gitignore` (указатель трека проект хранит в git) и тон `speckit-analyze`, `speckit-converge` и `speckit-tasks`: нажим капсом снят, уровни серьезности, нормы конституции и требование исполнить хук остались. Остальное берется из рендера поставки как есть, манифесты `.specify/integrations/` - тоже.

| Скилл | Когда опираться |
| --- | --- |
| [speckit-specify](../../.agents/skills/speckit-specify/SKILL.md) | Спека фичи из описания |
| [speckit-plan](../../.agents/skills/speckit-plan/SKILL.md) | План реализации / design-артефакты |
| [speckit-tasks](../../.agents/skills/speckit-tasks/SKILL.md) | `tasks.md` по плану |
| [speckit-analyze](../../.agents/skills/speckit-analyze/SKILL.md) | Кросс-проверка spec / plan / tasks |
| [speckit-implement](../../.agents/skills/speckit-implement/SKILL.md) | Выполнение задач из `tasks.md` |
| [speckit-converge](../../.agents/skills/speckit-converge/SKILL.md) | Добор недоделанного после `implement`: остаток дописывается в `tasks.md` |
| [speckit-checklist](../../.agents/skills/speckit-checklist/SKILL.md) | Чеклист по требованиям фичи |
| [speckit-constitution](../../.agents/skills/speckit-constitution/SKILL.md) | Конституция проекта; шаблоны и команды читают ее сами |

`speckit-taskstoissues` исключен из поставки.
При обновлении не восстанавливать скилл и его запись в манифесте интеграции.

Вендорные скиллы стека и инструментов (документация платформы, наблюдаемость, диаграммы) комплект ставит необязательными пакетами модуля `skills`: пакет выбирается при настройке harness, его разделы ниже. Скилл вне пакетов проект подключает сам: каталог копией поставки в `.agents/skills/<имя>/`, строка в раздел "Подключенные поставки" в конце этого файла (раздел заводится с первой такой поставкой) и имя в `docs.vendoredSkills` манифеста - тогда гейты не требуют от них правил проекта.
{{#if module:skills/aspire}}
### Aspire

Поставка - бандл microsoft/aspire-skills (`npm run bundle:skills` на теге релиза), лицензия MIT: каталоги `skills/*` без `evals`, хуки телеметрии бандла не ставятся. С поставкой намеренно расходятся примеры экспериментальных API: ручное подавление диагностики запрещают [гейты](../../docs/platform/architecture/gates.md), поэтому вместо `#pragma warning disable` текст ведет к согласию на экспериментальный API по правилам .NET проекта или к ресурсу `AddDockerfile`. Обновление по этим местам - слияние, а не накат.

| Скилл | Когда опираться |
| --- | --- |
| [aspire](../../.agents/skills/aspire/SKILL.md) | **Роутер** Aspire: детект AppHost => нужный подскилл |
| [aspire-init](../../.agents/skills/aspire-init/SKILL.md) | Первый Aspire в репо без AppHost (`aspire init` / `new`) |
| [aspireify](../../.agents/skills/aspireify/SKILL.md) | Проводка ресурсов в существующий AppHost после init |
| [aspire-orchestration](../../.agents/skills/aspire-orchestration/SKILL.md) | `aspire start/stop/wait`, file locks, порты, hot reload |
| [aspire-deployment](../../.agents/skills/aspire-deployment/SKILL.md) | Publish / deploy / destroy (Compose, K8s, Azure, AWS) |
| [aspire-monitoring](../../.agents/skills/aspire-monitoring/SKILL.md) | Локальные логи / otel / `describe` / dashboard |
{{/if}}{{#if module:skills/grafana}}
### Наблюдаемость

Поставка - `skills/grafana-core` и `skills/grafana-lgtm` в grafana/skills, лицензия Apache-2.0.

| Скилл | Когда опираться |
| --- | --- |
| [opentelemetry](../../.agents/skills/opentelemetry/SKILL.md) | Инструментирование сервиса, экспорт метрик / логов / трасс по OTLP |
| [prometheus](../../.agents/skills/prometheus/SKILL.md) - [promql](../../.agents/skills/promql/SKILL.md) | Метрики и запросы PromQL: rate, гистограммы, кардинальность |
| [loki](../../.agents/skills/loki/SKILL.md) | Логи и LogQL |
| [tempo](../../.agents/skills/tempo/SKILL.md) | Трассы и TraceQL |
| [grafana-oss](../../.agents/skills/grafana-oss/SKILL.md) - [dashboarding](../../.agents/skills/dashboarding/SKILL.md) | Настройка Grafana OSS, провижининг источников и дашбордов |
| [alerting-irm](../../.agents/skills/alerting-irm/SKILL.md) | Алерты, маршрутизация уведомлений, дежурства, SLO |
{{/if}}{{#if module:skills/dotnet-docs}}
### Документация .NET

Поставка: `microsoft-docs` - `skills/microsoft-docs` в github/awesome-copilot, лицензия MIT; `dotnet-inspect` - вывод `dnx dotnet-inspect -y -- skill`, лицензия инструмента MIT: текст отвечает версии инструмента, которую запускает `dnx`.

| Скилл | Когда опираться |
| --- | --- |
| [microsoft-docs](../../.agents/skills/microsoft-docs/SKILL.md) | Актуальная документация .NET / Aspire / Azure / Agent Framework |
| [dotnet-inspect](../../.agents/skills/dotnet-inspect/SKILL.md) | Поверхность .NET API в NuGet / diff версий пакетов |
{{/if}}{{#if module:skills/drawio}}
### Диаграммы draw.io

Поставка - `skills/drawio-skill` в Agents365-ai/drawio-skill, лицензия MIT (`LICENSE` в каталоге скилла; индекс фигур - Apache-2.0, `data/SHAPE-INDEX-NOTICE.md`). С поставкой намеренно расходятся фронтматтер без `allowed-tools` (права агента задают настройки агента, а не скилл) и `references/style-extraction.md` без названия модели.

| Скилл | Когда опираться |
| --- | --- |
| [drawio-skill](../../.agents/skills/drawio-skill/SKILL.md) | Редактируемые диаграммы `.drawio`: C4, BPMN, UML, ERD, импорт из кода и IaC, сверка и ревью диаграмм |
{{/if}}
## Скиллы методологии

### Методология

| Скилл | Когда опираться |
| --- | --- |
| [architecture-review](../../.agents/skills/architecture-review/SKILL.md) | Независимое сценарное ревью PR, ADR, RFC, спецификации или существующей архитектуры |
| [review](../../.agents/skills/review/SKILL.md) | Ревью PR на шаге review issue-dev: удаленный круг и локальный fallback |
| [code-simplifier](../../.agents/skills/code-simplifier/SKILL.md) | Упрощение диффа перед кругом ревью (issue-dev, стадия simplify) |
| [issue-dev](../../.agents/skills/issue-dev/SKILL.md) | Конвейер через issues репозитория: захват задачи, рабочая ветка, черновой PR со связью с задачей, ревью меткой, Draft и Ready |
{{#if module:release/tag-flow}}| [release](../../.agents/skills/release/SKILL.md) | Выпуск релиза по просьбе человека: срез, Ready, ожидание выката, итог и правка сбоя |
{{/if}}| [speckit-market](../../.agents/skills/speckit-market/SKILL.md) | Рыночная сверка спеки перед `clarify`: что рынок умеет по этой функции, чем заказ расходится с практикой, какие вопросы задать |
| [speckit-clarify](../../.agents/skills/speckit-clarify/SKILL.md) | Уточнение дыр в спеке: спрашивает все, что меняет работу. Форк поставочного скилла - потолок в пять вопросов снят |
| [log-inventory](../../.agents/skills/log-inventory/SKILL.md) | Инвентаризация реестров сообщений: журналы, ошибки, словари интерфейса и `messages.env` - выгрузка, разбор по группам необходимости и уровню, удаление шума |
| [docs-text](../../.agents/skills/docs-text/SKILL.md) | Правила письма docs / комментарии / сообщения; механический слой - Vale (`HumanizedText`, [`.vale.ini`](../../.vale.ini)), гейт - [`vale-lint.py`](../scripts/check/vale-lint.py). Скиллы, `.specify/` и `specs/` под правила не попадают - это машинный текст |
| [code-docs](../../.agents/skills/code-docs/SKILL.md) | XML-doc, JSDoc и TSDoc: контракт, границы и условия результата по коду; механический слой - гейт [`check-comments.py`](../scripts/check/check-comments.py) и Vale |
