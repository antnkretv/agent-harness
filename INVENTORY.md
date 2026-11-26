# Инвентаризация: harness Beerent и комплект

Документ о происхождении комплекта: какие части harness репозитория `beerent-ru/beerent` найдены,
что из них вошло в комплект и в каком виде, что осталось в Beerent и почему. Пофайловое
соответствие держит [`kit.json`](kit.json): поле `origin` у файла комплекта - файл Beerent, из
которого взята или переписана версия комплекта. Связи с установленным деревом нет: payload -
единственный источник файлов комплекта, а Beerent ставит его как любой проект.

## Найдено и для чего

Таблица фиксирует исходные пути Beerent на коммите `962c332d96f63435a153cb84967707fa165d2b2a`.
Это происхождение комплекта; действующие пути установленного слоя находятся в `.harness/`.

| Часть | Где в Beerent | Назначение | Модуль комплекта |
| --- | --- | --- | --- |
| правила агентов | `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.codex/`, `.cursor/` | точка входа агента, хуки, запреты, адаптеры сред | ядро; адаптеры - `agents` |
| процесс | `CONTRIBUTING.md`, `REVIEW.md`, `docs/platform/*.md` | коммиты, ветки, ревью, стандарты документации, комментариев и тестов | ядро |
| слой платформы | `.harness/` | манифест проекта, реестр правил, конвейер issue-dev, запреты, промпты, планки ревью, общие хуки, читатели манифеста | ядро; хук текстов - `forge` |
| гейты | `.github/scripts/`, `.githooks/`, `.vale/`, `.vale.ini`, `.gitleaks.toml`, `.editorconfig` | проверки дерева, git-хуки, правила письма, скан секретов | ядро, `githooks`, `prose`, `secrets`, `shell`; гейты площадки - `forge`, `tracker`, `review` |
| CI | `.github/workflows/`, `.github/actions/` | Check, ревью тремя проходами, агент задач, разбор алертов, релиз, связь ветки с задачей; раннеры по переменным `*_RUNS_ON`, сессия агента действием `agent-session` | `forge/github`, `forge/gitlab`, `runners`, `review`, `issue-agent`, `alert-triage`, `release`, `tracker`; шаги переписаны скриптами поверх `forge.py` и `tracker.py`, сессию ведет `agent-run.py` |
| диагностика стенда | `infra/mcp-production/` | MCP чтения production: исходники, телеметрия, хост, журналы | `stands/mcp-stand`: голый инструмент, экземпляр на стенд, хосты и контейнеры - в конфиге экземпляра |
| SDD | `.specify/`, `.agents/skills/speckit-*`, `specs/README.md` | Spec Kit и надстройки трека, рынка и уточнения | `sdd` |
| скиллы | `.agents/skills/` | issue-dev, review, architecture-review, code-simplifier, docs-text и вендорные поставки | ядро; поставки Aspire, Grafana, документации .NET и draw.io - пакеты модуля `skills` |
| анализаторы .NET | `backend/tools/analyzers/`, `backend/Directory.Build.*` | политика сборки и правила Roslyn | вернулись в Beerent; в комплекте - журнал диагностик `backend/dotnet` |
| раскладка проектов | `backend/ProjectLayout.props` | виды проектов и модули | вернулась в Beerent |
| архитектурные тесты | `backend/Tests/Architecture.Tests/` | NetArchTest по модулям backend | вернулись в Beerent; комплект зовет точку входа `architecture` |
| ESLint | `.config/eslint-boundaries.js` | границы слоев; правила TypeScript задает проект экспортом `required` | расширение `node` модуля стороны |
| окружение | `infra/provisioning/`, `infra/runners/` | подготовка сессий агентов, образ раннеров | ядро; установщик инструмента - модуль или стек, которому он нужен |
| LSP агентов | `infra/provisioning/scripts/lsp-mcp.mjs`, `ensure-lsp.sh`, `.lsp-mcp.json`, сервер `lsp` в `.mcp.json`, `.cursor/mcp.json`, `.codex/config.toml` | мост MCP к языковым серверам C# и TypeScript | `stack/dotnet` и `stack/node`: серверы своего стека, `.lsp-mcp.json` собирается из фрагментов стеков |
| серверы MCP агентов | `.codex/config.toml` `[mcp_servers.*]` | документация библиотек, поиск, шлюз Docker, образцы интерфейсов, Aspire | `mcp`: расширение на сервер, без учетных данных |
| глоссарий | `docs/platform/standards/product-terms.md` | одно понятие - один термин во всех текстах; столбец `Проверка` дает правила Vale для строк реестров, документации и комментариев | ядро: заготовка с одной строкой-примером; механическая проверка требует `prose/vale`, термины Beerent не переносятся |
| решения | `docs/platform/adr/` | ADR платформы, backend и приложений | в архив не входят: требования решений записаны в документах поставки, процесс ADR проекта - `docs/platform/adr/README.md` |

Раскладка конфигурации платформы - [что объявляет конвейер](payload/.harness/README.md#что-объявляет-конвейер).
Карта `.harness/project/area-map.json` сохраняет сопоставление по префиксам и список потребителей.

## Что вошло в комплект

Состав файлов по модулям задает [`kit.json`](kit.json), поле `module`.

**Из Beerent** - файл с полем `origin`: взят из Beerent как есть, с фактами Beerent, вынесенными в
манифест и параметры, или переписан по его правилу. **Новое** - части, которых у Beerent не было в
общем виде.

Число файлов в `kit.json` по путям модулей, которые ставят заготовки эксплуатации и выкладки.
Файл с несколькими путями `module` посчитан по первому; остальные модули в таблицу не входят.
Строки сверяет с `kit.json` команда `kit.py check`.

| Путь модуля | Из Beerent | Новое | Всего |
| --- | --- | --- | --- |
| `core` | 113 | 116 | 229 |
| `release/tag-flow` | 4 | 8 | 12 |
| `alert-triage` | 1 | 6 | 7 |

Общие зависимости runner `review_policy.py` и `review_verdict.py` входят в ядро;
модуль `review` ставит обвязку CI. Причина разделения - [заметки к файлам](MAINTAINING.md#payloadharnessscriptsreviewreview_policypy).

### Параметризация

- Имя, ветки и префиксы веток задач - ключи `project.*` и `git.*` манифеста; тексты с ними
  (`release.yml`, `CONTRIBUTING.md`, `AGENTS.md`, `issue-dev`, конституция) рендерятся
  установщиком.
- Состав приложений, их сторона, стек и команды проверки - `apps[]`; карта областей, таблица
  областей `AGENTS.md` и профили `CONTRIBUTING.md` собираются из него.
- Технологии Beerent - модули и расширения каталога `modules.json`: решение .NET и его сборка -
  настройки `backend/dotnet`, репозиторий GitHub - `forge/github`, задачи - `tracker/github-issues`,
  исполнитель ревью, агента задач и разбора алертов - расширения `review`, `issue-agent` и
  `alert-triage`, пул раннеров по виду задания - `runners`, стенды - записи `stands/mcp-stand`. Разбор алертов -
  модуль `alert-triage`: адрес Grafana, секрет и заголовок маршрута раннера, файл словаря путей,
  окружение и переменная раннеров отбора, метки ключа и канал ссылки - его настройки. Невключенный модуль
  выключает свои записи реестра правил с причиной и заменой из каталога.
- Пути сгенерированного, машинного текста и вендорных скиллов - `docs.*`.
- Серверы MCP агентов Beerent - расширения модуля `mcp`, мост LSP - стеки `dotnet` и `node`: установщик
  пишет их в конфиг каждого включенного агента, а `.lsp-mcp.json` несет только серверы включенных стеков.

### Адаптеры и общий модуль

- Гейты, которые в Beerent называли пути backend и приложений буквально, читают манифест:
  `check-text-registry.py`, `check-docs.py`, `check-doc-symbols.py`, `check-build-diagnostics.py`,
  `check-diagnostic-integrity.py`, `generated_sources.py`, `pre-push`, хук старта сессии.
- Набор шагов Check Beerent стал одним исполнителем гейтов
  [`check-all.py`](payload/.harness/scripts/check/check-all.py): воркфлоу ставит тулчейн и зовет его, гейты
  берутся из реестра правил, а пропавший или молча выключенный модуль - ошибка.
- Политика сборки, раскладка проектов и правила анализаторов были адаптером `.harness/dotnet/` и
  вернулись в Beerent (раздел ниже).
- Границы слоев фронтендов Beerent держали правила, написанные под раскладку каждого приложения;
  в комплекте их место занял `layerBoundaries` с объявлением слоев в конфиге приложения.
- Гейт закрепления версий `check-tool-pins.py` оставил общую часть: центральный каталог NuGet,
  `.node-version` и `engines`, digest чужой оснастки в воркфлоу с секретом агента, числа версий в
  документах.

### Правила анализаторов и сборки .NET

Анализаторы, арх-тесты, раскладка и политика сборки .NET были в комплекте адаптером
`.harness/dotnet/` и вернулись в Beerent: правила кода - собственность проекта, в комплекте остался
журнал диагностик для гейта `build-diagnostics`. Соответствие прежних кодов BEE нынешним BRN -
[правила .NET backend](../docs/platform/architecture/dotnet-rules.md#прежние-коды).

## Что осталось в Beerent

| Часть | Почему не в комплекте | Чем закрыть в проекте |
| --- | --- | --- |
| самопроверки `.harness/tests/` | тесты и фикстуры самого harness, а не правила проекта | идут до выпуска в `kit.py check` в установленном дереве smoke |
| анализаторы `Beerent.Analyzers`, их корпус, слоевые арх-тесты, раскладка и политика сборки .NET | правила кода и архитектуры Beerent | проект в `backend/`, подключение - точки входа `analyze` и `architecture` ([правила .NET backend](../docs/platform/architecture/dotnet-rules.md)) |
| выкладка: `build-and-push.yml`, `deploy-all.yml`, `stand.yml`, `frontend-build.yml`, `check-deploy-env.py`, `check-deploy-services.py`, `setup-buildx` | контур выкладки и стенды Beerent: Kubernetes, образы, топология сервисов | `project-deploy.yml` комплекта - точка подключения своей выкладки к релизу |
| `e2e.yml`, `integration-suites.py`, `tests-executed.py` | наборы интеграционных и сквозных тестов Beerent и их фильтры | команда `check` приложения; пустой прогон архитектурных тестов отказывает сам |
| `agent-telemetry.yml` | телеметрия агентов на инфраструктуре Beerent | опрос замера автономии - на стороне проекта ([platform/0141](../docs/platform/adr/platform/0141-agent-telemetry-and-autonomy.md)) |
| `verify-review-runner.py` | ручная проверка адаптера Codex на раннерах Beerent | - |
| `check-prompts.py` | раскладка промптов модели в модулях backend Beerent | правило версии промпта - [platform/0140](../docs/platform/adr/platform/0140-root-text-registries.md), гейт проект пишет под свою раскладку |
| `backend/Tests/Architecture.Tests` | композиция хостов, реестры EventId, границы вендоров, спецификации предикатов, слои и правила модулей Beerent | точка входа `architecture` приложения backend |
| шаг Check `Unused methods and types`: `jb inspectcode` (UnusedType, UnusedMember методов) и `roslynator find-symbol --unused` по реестрам `*Messages.cs` и `ErrorCodes.cs` | инструменты JetBrains и Roslynator стоят в манифесте инструментов решения Beerent, маски исключений и реестров - имена Beerent; в `.config/dotnet-tools.json` комплект владеет только записью `csharpier`, остальные инструменты манифеста - проектные | проект ставит инструменты в свой манифест и вызывает поиск неиспользуемого из `check` приложения стека `dotnet` |
| сборка образа приложения в Check (`image_check`): `docker build --output type=cacheonly` для приложения с `Dockerfile.dockerignore` | соглашение выкладки Beerent: контекст сборки в корне, `NODE_VERSION` из `.node-version`, `Dockerfile.dockerignore` рядом с приложением | сборка образа в `check` приложения или в выкладке проекта (`project-deploy.yml`) |
| пакеты анализаторов сторонних поставщиков: `Meziantou.Analyzer` с правилами MA0042, MA0100, MA0129, MA0182 и `Microsoft.CodeAnalysis.BannedApiAnalyzers` со списком `BannedSymbols.Application.txt` (часы и время только через внедренные `TimeProvider` и `IClock`) | это анализаторы с правилами и списками Beerent, а не политика комплекта: комплект пакетов в решение не подключает | проект подключает пакет в своем `Directory.Build.props` и список запретов рядом с ним; правило ослабления политики BRNB001 действует и на эти анализаторы |
| `infra/provisioning/` (подъем стека, Aspire, Logto, конвертер документов, docker), `infra/runners/` | окружение и раннеры продукта Beerent | `.harness/provisioning/` комплекта ставит тулчейн методологии; свои раннеры подключаются модулем `runners` ([ci-cd](payload/docs/platform/delivery/ci-cd.md#переключатели-раннеров)) |
| скиллы продукта: `beerent-backend`, `bee-pms-frontend`, `ui-text`, `mui`; поставка `grafana-dashboards` | факты домена и стека Beerent; `mui` переписан под PMS, а у него и у `grafana-dashboards` в Beerent нет лицензии и источника поставки. Aspire, Grafana LGTM, `microsoft-docs`, `dotnet-inspect` и `drawio-skill` идут пакетами модуля `skills` | раздел "Подключенные поставки" индекса скиллов и `docs.vendoredSkills` |
| ADR областей `backend`, `pms`, `booking` и продуктовые ADR `platform` | решения продукта Beerent | реестр решений проекта начинается после решений методологии |
| факты `docs/platform/`: правила и адресаты алертов, панели и запросы дашбордов, образы и порядок выкладки, команды и сервисы локального стека; события, композиция хостов, реестр требований, строки глоссария | документы продукта Beerent; формы документов эксплуатации, выкладки и глоссария входят в комплект как заготовки | проект заполняет заготовки; гейт реестра требований берет реестр проекта, когда он появится |
| `.harness/project/manifest.json`, `.specify/feature.json`, `.cursor/` запуск стека, модель и плагины `.codex/config.toml` | состояние и окружение Beerent; серверы MCP в конфигах агентов ставят `stands/mcp-stand`, модуль `mcp` и стеки `dotnet` и `node` (`lsp`) | манифест проекта пишет `ah.py configure` по ответам владельца |
