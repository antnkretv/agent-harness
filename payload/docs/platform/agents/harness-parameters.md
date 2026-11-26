# Параметры harness: манифест, модули, зависимости

> **Тип (Diataxis):** Справочник

Справочник манифеста [`.harness/project/manifest.json`](../../../.harness/project/manifest.json) и модулей harness.
Устройство слоя, настройка и обновление - [harness.md](harness.md). Манифест строит
`configure` установщика комплекта из ответов владельца; `apply` и `verify` проверяют его по каталогу модулей
`.harness/state/modules.json` и отказывают с перечнем ошибок до записи первого файла.

## Манифест проекта

| Ключ | Что задает | Кто читает |
| --- | --- | --- |
| `project.name` | имя проекта в текстах процесса, конституции и сообщениях хуков | установщик, `commit-format.sh`, документы |
| `project.gitConfigPrefix` | префикс ключей `branch.<ветка>.<префикс>-pr` и `-issue` в git config | конвейер (`task-worktree.py`, `pipeline-run.py`, `forge.py open-change`), `commit-msg`, `pre-push` |
| `git.baseBranch` | ветка разработки: база PR, сравнения гейтов и ревью | гейты, ревью, `issue-dev` |
| `git.releaseBranch` | ветка релиза, отличная от базовой | модуль `release`, `issue-dev`, `forge-setup.py` |
| `git.branchConvention` | конвенция имен веток агента и человека, вопрос `configure`: `conventional` (`{тип}/{задача}-{слаг}`) или `gitflow` (`{поток}/{задача}-{слаг}`) - [`CONTRIBUTING.md` §2](../../../CONTRIBUTING.md#2-ветки) | выделение копии, `commit-msg`, `pre-push`, связь PR с задачей, привязка ветки |
| `docs` | пути документации и классы текста ([раздел](#раздел-docs)) | гейты документов, текста и комментариев |
| `apps[]` | приложения ([раздел](#приложение-apps)); пустой список допустим | гейты, области, хуки |
| `modules` | модули, их расширения и настройки ([раздел](#модули)) | установщик, гейты, хуки, воркфлоу |
| `agents` | версии CLI и именованные исполнители процессов ([раздел](#agents)) | установщик, запуск сессий, круг ревью |
| `rules` | необязательно: снятые правила комплекта `disabled` и свои правила проекта `own` ([как снять правило](../../../.harness/README.md#как-снять-правило)) | `check-all.py`, `verify`, гейты документов и копий |

Обязательны все ключи, кроме `docs` и `rules`. Без `docs` читатель берет значение по умолчанию, которое `configure` и
пишет. У идентичности запасных значений нет
([`.harness/README.md`](../../../.harness/README.md#два-разных-отказа)).

### Приложение `apps[]`

| Поле | Обязательно | Что задает |
| --- | --- | --- |
| `id` | да | имя области в карте и профиле; `[a-z0-9-]`, уникально, не `docs` и не `agents` - это служебные области |
| `path` | да | каталог приложения от корня репозитория без `./` и `/` в конце; `.` - корень |
| `side` | да | сторона `backend` или `frontend`; ее модуль включен, а у стека с адаптером включено расширение `<сторона>/<стек>` |
| `stack` | да | `node`, `dotnet`, `python`, `go`, `jvm` или `other` |
| `check` | да | полный гейт приложения для CI и ручного запуска, строка Bash; у `node` - `npm run <скрипт>` со скриптом из `scripts` в `package.json` приложения, у другого стека `npm run` не принимается |
| `doc` | нет | документ приложения для таблицы областей |
| `skill` | нет | скилл приложения для таблицы областей |
| `entries` | нет | точки входа проверок приложения по ролям ([раздел](#точки-входа-приложения)); у стека без адаптера роль `architecture` - гейт его границ |
| `configs` | нет | пути файлов конфигурации проверок от корня: их правку `diagnostic-integrity` сверяет с базой наравне с конфигами, которые узнает по имени |
| `architectureWaiver` | вместо `entries.architecture`; у стека `node` - вместо слоев `layerBoundaries` | причина, по которой архитектурного гейта или слоев у приложения нет; означает пустую роль `architecture` |

### Точки входа приложения

`entries` - объект, ключ которого - роль, значение - `{"command": "<команда Bash>", "cwd": "<каталог от корня>"}`;
`cwd` по умолчанию - `path` приложения. Через эти точки входа комплект зовет проверки, которые принадлежат
проекту: правила линтеров, анализаторов и архитектуры живут в его конфигах, комплект только запускает их.

| Роль | Что запускает |
| --- | --- |
| `format` | проверку формата без правки файлов |
| `lint` | линтер с правилами проекта |
| `analyze` | анализ типов и сборку с анализаторами |
| `architecture` | архитектурные тесты и гейт границ |
| `test` | тесты приложения |

`check-all.py` передает каждой точке входа базу сравнения в `HARNESS_BASE`, как и `check`. Роль,
команда которой входит звеном `&&` в `check` и идет в каталоге приложения, уже выполнена командой
`check` и второй раз не запускается. Пустая роль - норма: `verify` и `check-all.py` пишут о ней
"не настроено", а не "прошло". `configure` предлагает точки входа по `scripts` в `package.json`
приложения (`format:check`, `lint`, `typecheck`, `lint:architecture`, `test` или `test:unit`) и по решению .NET
(`analyze` - сборка, `test` - тесты), а `configs` - по файлам `Directory.Build.*`, `.editorconfig`,
`.vale.ini` и `eslint.config.*` в каталоге приложения и над ним. `verify` проверяет, что рабочий
каталог есть, у `npm run` есть скрипт, а первая программа команды находится в `PATH` или в рабочем
каталоге; сборку и тесты он не запускает.

### Стеки приложений

| Стек | Адаптер | Требования к проекту |
| --- | --- | --- |
| `node` | расширение `node` модуля стороны | корневые `package.json` с workspaces и lock-файл, `.node-version`, `eslint.config` приложения подключает конфиги `.config/`, ESLint и `@typescript-eslint/parser` в devDependencies |
| `dotnet` | расширение `dotnet` модуля `backend` | `global.json` и `Directory.Packages.props` у решения, журнал диагностик сборки в SARIF; C# в раскладке CSharpier - манифест `.config/dotnet-tools.json` и настройки `.vscode` ставит комплект, раскладку держит гейт `dotnet-format` ([ci-cd](../delivery/ci-cd.md#форматирование-c)) |
| `python`, `go`, `jvm`, `other` | нет | `entries.architecture` или `architectureWaiver` ([гейт стека](../architecture/gates.md#стек-без-адаптера)) |

Стек `dotnet` или `node` ставит агентам разработки языковой сервер своего стека: мост MCP
[`lsp-mcp.mjs`](../../../.harness/provisioning/scripts/lsp-mcp.mjs), установщик
[`ensure-lsp.sh`](../../../.harness/provisioning/scripts/ensure-lsp.sh) и
[`.lsp-mcp.json`](../../../.lsp-mcp.json) с серверами включенных стеков: `csharp-ls` у `dotnet`,
`typescript-language-server` у `node`. Сервер `lsp` попадает в конфиг MCP каждого включенного агента
([`mcp`](#mcp)), а серверы ставят старт сессии и setup script облачного агента. Первый запуск моста
доставляет недостающее сам, поэтому подключение сервера ждет до пяти минут: `MCP_TIMEOUT` в
`.claude/settings.json`, `startup_timeout_sec` в `.codex/config.toml`. Мосту нужен Node.js, серверу
C# - .NET SDK проекта.

### Раздел `docs`

| Ключ | По умолчанию | Что задает |
| --- | --- | --- |
| `docs.index` | `docs/README.md` | индекс документации, от которого гейт ищет достижимость |
| `docs.instructionRoots` | `[""]` | каталоги с собственными `AGENTS.md` и `CLAUDE.md` |
| `docs.machineTextPrefixes` | агент-активы, `.specify/`, `specs/`, промпты | машинный текст: правила письма к нему не применяются |
| `docs.liveDocPrefixes` | `docs/features/` | живые документы фич |
| `docs.typedPrefixes` | `docs/` | технические документы, которые объявляют тип Diataxis строкой под заголовком H1; ADR, `README.md`, имя с датой на конце, `historicalPrefixes` и машинный текст не проверяются ([`documentation.md`](../standards/documentation.md#тип-документа)) |
| `docs.historicalPrefixes` | нет | датированные записи: исследования, журналы решений; ссылки в них - состояние на дату записи. [`check-adr-refs.py`](../../../.harness/scripts/check/check-adr-refs.py) не сверяет ссылки на ADR, [`check-tool-pins.py`](../../../.harness/scripts/check/check-tool-pins.py) - версии в Markdown. Исключение из гейта не снимает [правило снимков](../standards/documentation.md#живой-документ-и-снимок) |
| `docs.generatedSegments`, `docs.generatedPrefixes` | `node_modules`, `bin`, `obj`, `dist`, миграции | сгенерированное: гейты автора его не разбирают |
| `docs.vendoredSkills` | нет | подключенные поставки скиллов, тело которых правится обновлением; скиллы включенных пакетов модуля [`skills`](#skills) `configure` вписывает сам, приемка отказывает манифесту без них |
| `docs.payloadPrefixes` | нет | деревья поставки для других репозиториев: их проверяют гейты того дерева, а не этого |
| `docs.checks` | нет | проверки документов, которые проект включает в гейте `docs-structure`: `index` - достижимость из `docs.index`, `names` - имена файлов, `adr-headers` - шапка ADR, `types` - тип Diataxis по `docs.typedPrefixes`, `status-lexicon` - словарь статусов в инструкциях, `size` - размер документа |
| `docs.comments` | нет | правила гейта `comment-layer`: пределы `plain` и `doc` (`lines`, `words`, `chars`), `density` (`max`, `floor`), `constantFloor`, шаблоны `history`, `failure`, `moodGuard` и `traceability`. Ключи задаются вместе: неполный набор, в том числе без поля `plain`, `doc` или `density`, - отказ с именами недостающих; без ключа гейт пишет "не настроено" ([стандарт комментариев](../standards/code-comments.md#8-порог-объема)) |

`configure` спрашивает `generatedSegments`, `generatedPrefixes`, `machineTextPrefixes`,
`liveDocPrefixes`, `historicalPrefixes` и `vendoredSkills` вопросами `docs.<ключ>`: детектор
добавляет к значению по умолчанию найденные каталоги сгенерированного кода, каталоги датированных записей и
скиллы с `LICENSE` в `.agents/skills/`, кроме скиллов пакетов `skills`. Остальные ключи задает ответ `docs` - объект JSON.

## Настройки окружения

Настройки окружения - переменные, которые человек задает своей машине: токены поставщика кода и трекера,
выбор исполнителя и интерпретатора. Их значения не входят в манифест и в git.

- [`.harness/settings.defaults.env`](../../../.harness/settings.defaults.env) - файл комплекта: все
  настройки с значениями по умолчанию, строка `ИМЯ=значение`. `apply` пишет его версией комплекта.
- `.harness/settings.env` - файл человека вне git: строку `/.harness/settings.env` держит блок комплекта
  `.gitignore`. Его один раз создает `apply` копией значений по умолчанию и дальше не трогает. Рабочая
  копия задачи получает ссылку на файл основного клона ([`task-worktree.py`](../../../.harness/README.md#авторская-рабочая-копия)).
- Порядок чтения: переменная окружения, затем `settings.env`, затем `settings.defaults.env`. Пустое значение
  настройку не задает. Кавычки не снимаются: значение идет до конца строки.
- Читает файлы общий загрузчик [`python.sh`](../../../.harness/lib/python.sh), а под Windows -
  [`python.cmd`](../../../.harness/hooks/python.cmd): настройки получают все скрипты комплекта на Python и
  скрипты bash, которые подключают его реестр сообщений.
- Агентам чтение `settings.env` закрыто: правило `deny` в `.claude/settings.json` и `.cursor/cli.json` и
  отказ хука [`agent-gate.py`](../../../.harness/hooks/agent-gate.py) на команду оболочки с путем файла.

| Настройка | Модуль | Что задает |
| --- | --- | --- |
| `AGENT_PROVIDER` | ядро | исполнитель локального круга ревью и сессий `agent-run.py` вместо провайдера авторской сессии |
| `HARNESS_PYTHON` | ядро | интерпретатор Python скриптов комплекта вместо найденного на PATH |
| `GH_TOKEN` | `forge/github` | токен GitHub для вызовов поставщика кода |
| `GITLAB_TOKEN` | `forge/gitlab` | токен GitLab с правом `api` для вызовов поставщика кода |
| `JIRA_TOKEN` | `tracker/jira` | токен Jira; секрет с другим именем из манифеста задается строкой с этим именем |
| `RELEASE_TOKEN` | `release/tag-flow` | токен среза и обратного слияния, если `release/tag-flow.token` называет его |

Секрет модуля, которому манифест дал свое имя, проект добавляет в `settings.env` строкой с этим именем.

## Модули

Каталог `.harness/state/modules.json` объявляет модули, их расширения, схему настроек и тексты
выключения ([ядро и модули](harness.md#ядро-и-модули)); раздел `modules` манифеста хранит выбор
проекта:

```json
"modules": {
  "backend": { "extensions": { "dotnet": { "solution": "src/App.slnx" } } },
  "notifications": { "settings": { "events": { "release": ["forge"] } }, "extensions": { "forge": {} } },
  "release": { "off": "релиз идет вне конвейера harness" }
}
```

- Включенный модуль - объект с необязательными `settings` и `extensions`; ключ `extensions` -
  выбранное расширение, значение - его настройки. Число расширений задает каталог: `select` с
  `min` и `max` или слоты, у каждого свои границы.
- Выключенный модуль - `{ "off": "<причина>" }`. Модуль, которого в разделе нет, тоже выключен, но
  если его технология найдена в дереве, `verify` требует причину.
- Включенные пути: `core`, `<модуль>`, `<модуль>/<расширение>`, а у расширений модулей сторон
  `backend` и `frontend` - еще `stack/<расширение>`. Путь ставит файлы комплекта и включает записи
  реестра правил. Запись реестра невключенного пути пишется с
  `enabled: false`, `disabledReason` и `replacement` из текстов выключения каталога и `disabledBy` - самим
  невключенным путем.
- Приемка сверяет раздел с каталогом: модуль и расширение известны, их число в границах, пути
  `requires` включены, сторона `side` и стеки `stacks` модуля включены; у настройки ключ известен,
  форма и источник допустимы, обязательный факт задан. Путь из `wants` модуля включен, либо его
  модуль выключен с причиной проекта, отличной от причины каталога: в ней проект называет, почему
  механическая часть проверки не нужна и что ее заменяет.

### Значение настройки

| Запись | Источник | Когда допустима |
| --- | --- | --- |
| значение | литерал в манифесте | у каждой настройки, если каталог не сузил `sources` |
| `{ "env": "ИМЯ", "default": <значение> }` | переменная окружения; `default` - значение без нее | `env` в `sources` настройки |
| `{ "secret": "ИМЯ" }` | секрет площадки CI по имени | `secret` в `sources` настройки |

Значение секрета в манифесте не хранится. Манифест называет только имя секрета, значение задают в
настройках площадки, а имена сверяет `forge-setup.py`. Имя переменной и секрета -
`[A-Z][A-Z0-9_]*`.

### Вид и форма

Вид настройки решает, кто дает значение. `decision` - решение владельца: какие решения `configure`
спрашивает, сводит в вопрос `defaults` или пишет без вопроса - [настройка](harness.md#настройка).
`fact` - факт дерева:
`configure` ищет кандидатов детектором каталога и спрашивает по правилу `rule`, пустой ответ на
необязательный текстовый факт в манифест не пишет. Обязательный факт без значения - отказ приемки.

| Форма | Значение |
| --- | --- |
| `text` | строка; шаблон `pattern` каталога, если он задан |
| `texts` | строка или список строк |
| `records` | список объектов строковых полей; обязательные и необязательные поля называет каталог |
| `choice` | одно значение из `values` каталога |
| `choices` | список значений из `values` |
| `flag` | `true` или `false` |
| `object` | объект |

Путь в настройке - от корня репозитория без ведущего `/`.

### `backend`

Бэкенд проекта, расширение - его стек. Модуль стороны: форма проекта (`shape`) решает, есть ли
бэкенд, а включенный без расширения бэкенд идет стеком без адаптера.

| Расширение | Что ставит |
| --- | --- |
| `backend/dotnet` | адаптер .NET `.harness/dotnet/`: журнал диагностик SARIF `Harness.Build.targets` с импортом в ближайший к решению `Directory.Build.targets`, гейты `build-diagnostics` и `dotnet-format`; анализаторы, арх-тесты и политику сборки проект подключает сам и зовет точками входа `analyze` и `architecture` |
| `backend/node` | адаптер node, как у фронтенда; бэкенд на node - явный выбор при обеих сторонах |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `backend/dotnet.solution` | `fact` | `text` | решение `.sln` или `.slnx`, которое собирает CI; обязательно. По нему Check и сессия агента ставят SDK, `apply` пишет импорт журнала диагностик, `ensure-repo-deps.sh` зовет `dotnet restore` |
| `backend/dotnet.configuration` | `decision` | `text` | конфигурация, которую собирает `check` приложения: в ней гейт `build-diagnostics` читает журнал диагностик |
| `backend/dotnet.suppressions` | `decision` | `records` | пары `suppressor` - `rule` программных подавителей из пакетов: их подавления засчитывает `check-build-diagnostics.py`; подавители из исходников решения он находит сам |
| `backend/node.rules` | `decision` | `texts` | модули обязательных правил ESLint, как у `frontend/node.rules` |

### `frontend`

Фронтенд проекта, расширение - его стек; модуль стороны, как `backend`.

| Расширение | Что ставит |
| --- | --- |
| `frontend/node` | границы слоев ESLint `.config/eslint-boundaries.js`, хук ESLint по файлу, гейт `node-adapter` ([gates.md](../architecture/gates.md#node)) |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `frontend/node.rules` | `decision` | `texts` | модули от корня, чей экспорт `required` гейт `node-adapter` держит на каждом исходнике приложения стороны; конфиг ESLint приложения обязан их импортировать. Пусто - гейт сверяет только слои |

### `forge`

Поставщик кода: репозиторий, его CI и запросы на слияние. Модуль ставит хук текстов
`forge-text-lint.py` и `pr-threads.py`. Выключен - нет CI, PR и меток: гейты дерева
запускаются вручную через `check-all.py`, круг ревью - локальный.

| Расширение | Что ставит |
| --- | --- |
| `forge/github` | GitHub и GitHub Actions: `check.yml`, действия тулчейна, шаблон PR, гейт имен шагов воркфлоу, адаптер `github_adapter.py` |
| `forge/gitlab` | GitLab и GitLab CI/CD: задача `harness-check` в `.gitlab/ci/harness.yml`, задача событий `harness-event` в `.gitlab/ci/events.yml`, блок `include` в `.gitlab-ci.yml`, шаблон MR, адаптер `gitlab_adapter.py` |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `forge/github.repo` | `fact` | `text` | репозиторий `owner/name` из `git remote get-url origin`; обязательно |
| `forge/github.url` | `decision` | `text` | адрес сервера: `https://github.com` или адрес GitHub Enterprise |
| `forge/gitlab.project` | `fact` | `text` | путь проекта `group/name` или `group/subgroup/name` из `git remote get-url origin`; обязательно |
| `forge/gitlab.url` | `fact` | `text` | адрес сервера из `git remote get-url origin`; без него - `https://gitlab.com` |

Гейт shell конвейеров `check-workflow-shell.py` и `forge-setup.py` ставятся с любым расширением.
`forge/gitlab` требует переменную CI/CD `GITLAB_TOKEN` - токен проекта или группы с правом `api` - к
первому заданию конвейера комплекта, которое пишет на площадку: событию, публикации круга, закрытию
задач; без нее эти операции отказывают. Check (`harness-check`) секретов не требует. Образ задачи `harness-check` задает переменная
`HARNESS_CI_IMAGE` из `.gitlab/ci/harness.yml`; проект со своим списком `stages` держит в нем
стадию `test`, с модулем `release` - и стадию `deploy`.

### `runners`

Раннеры CI: на каких машинах идут задания конвейера; требует `forge`. Расширение - вид раннеров
площадки, настройки - пул по виду задания: метки `runs-on` у GitHub Actions, теги `tags` у GitLab
Runner. Установщик пишет пул в файлы CI списком; строка - пул из одного элемента. Выключен - задания идут на `ubuntu-latest` у GitHub и на
раннерах без тегов у GitLab.

| Расширение | Что ставит |
| --- | --- |
| `runners/github-actions` | метки `runs-on` заданий GitHub Actions; требует `forge/github` |
| `runners/gitlab-runner` | теги `tags` заданий GitLab CI; требует `forge/gitlab` |

Файлы и задания каждого вида - [таблица видов](../delivery/ci-cd.md#переключатели-раннеров).
Пул вида `review`, `agent`, `triage` и `release` `configure` спрашивает, только когда включен модуль этой
роли - `review`, `issue-agent`, `alert-triage`, `release` (поле `requires` настройки); иначе остается значение
по умолчанию.

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `runners/github-actions.check` | `decision` | `texts` | метки заданий вида `check`; по умолчанию `["ubuntu-latest"]` |
| `runners/github-actions.review` | `decision` | `texts` | метки заданий вида `review`, круга ревью |
| `runners/github-actions.agent` | `decision` | `texts` | метки заданий вида `agent`, агента задач |
| `runners/github-actions.triage` | `decision` | `texts` | метки заданий вида `triage`, разбора алертов |
| `runners/github-actions.release` | `decision` | `texts` | метки заданий вида `release` |
| `runners/github-actions.variables` | `decision` | `object` | переменные репозитория с метками `runs-on` в JSON по виду задания, например `{"triage": "TRIAGE_RUNS_ON"}`: заданная переменная заменяет метки вида |
| `runners/gitlab-runner.check` | `decision` | `texts` | теги заданий вида `check`; пусто - раннер без тегов |
| `runners/gitlab-runner.review` | `decision` | `texts` | теги заданий вида `review`, круга ревью |
| `runners/gitlab-runner.agent` | `decision` | `texts` | теги заданий вида `agent`, агента задач |
| `runners/gitlab-runner.release` | `decision` | `texts` | теги заданий вида `release` |

Codex с входом ChatGPT требует свой раннер: `CODEX_HOME/auth.json` лежит в его томе.

### `tracker`

Трекер задач, отдельно от поставщика кода. Модуль ставит гейт связи PR с задачей
`check-pr-issue-link.py` и, с поставщиком кода, [задание закрытия задач](../delivery/ci-cd.md#закрытие-задач)
после мержа в `git.baseBranch`. Выключен - ветки задач и связь PR с задачей не проверяются, а задачи после
мержа не закрываются.

| Расширение | Что ставит |
| --- | --- |
| `tracker/github-issues` | GitHub Issues: форма задачи `.github/ISSUE_TEMPLATE.md`, `link-branch.py` и, при `forge/github`, воркфлоу привязки ветки `link-branch.yml` |
| `tracker/jira` | Jira Cloud или Jira Data Center: форма задачи `.harness/issue-template.md`, адаптер `jira_adapter.py` |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `tracker/github-issues.repo` | `fact` | `text` | репозиторий задач `owner/name`; обязательно, обычно тот же, что у поставщика кода |
| `tracker/jira.url` | `decision` | `text` | адрес Jira: `https://<сайт>.atlassian.net` или адрес Jira Data Center; обязательно |
| `tracker/jira.projects` | `decision` | `texts` | ключи проектов Jira, задачи которых ведет проект; обязательно, первый - проект новых задач |
| `tracker/jira.user` | `decision` | `text` | учетная запись Jira Cloud - почта владельца классического токена API, значение или переменная окружения; у адреса `*.atlassian.net` обязательно; пусто - персональный токен Jira Data Center в заголовке `Bearer` |
| `tracker/jira.token` | `decision` | `text` | секрет токена: API Jira Cloud или персональный Jira Data Center; по умолчанию `JIRA_TOKEN` |
| `tracker/jira.issueType` | `decision` | `text` | тип задач, которые заводит конвейер; по умолчанию `Task` |
| `tracker/jira.doneTransition` | `decision` | `text` | имя перехода, которым задание закрытия закрывает задачу; пусто - единственный переход задачи в статус категории `done` |

Задача трекера Jira - ключ `PROJ-123`, ветка задачи по конвенции `git.branchConvention`, например
`feat/PROJ-123-слаг` или `feature/PROJ-123-слаг`. Связь запроса с задачей - ключ в описании: `Closes PROJ-123` закрывает задачу
после мержа запроса в `git.baseBranch`, `Refs PROJ-123` ее называет. Закрывает задачу не поставщик кода, а
[задание закрытия](../delivery/ci-cd.md#закрытие-задач) переходом `tracker/jira.doneTransition`, а без этой
настройки - единственным переходом задачи в статус категории `done`. Ветку и запрос Jira
связывает с задачей сама по ключу; `link-branch.py` с Jira не ставится.

### `agents`

Агенты разработки - ответ на вопрос "Какие модели вы используете для разработки?"; модуль обязателен,
расширений одно или несколько: установщик ставит файлы каждого выбранного агента. Локальный круг ревью
идет агентом авторской сессии, вне сессии агента - первым выбранным; модель проходов - модель комплекта
для этого провайдера (`KINDS` в `agent_provider.py`).

| Расширение | Что ставит |
| --- | --- |
| `agents/claude-code` | `CLAUDE.md`, `.claude/` и правила `.agents/rules/`: настройки, хук старта сессии; хук текстов модуля `forge` подключают настройки. Ссылки `.claude/skills` и `.claude/rules/harness-*.md` на `.agents/` - [ссылки агентов](environment.md#ссылки-агентов) |
| `agents/codex` | `.codex/`: агент упрощения, хуки; общие запреты команд в `rules/harness.rules`. Скиллы Codex читает из `.agents/skills` сам |
| `agents/cursor` | `.cursor/` и правила `.agents/rules/`: окружение облачного агента, хуки, инструкции ботов; ссылки `rules/harness-*.mdc` на `.agents/rules/` - [ссылки агентов](environment.md#ссылки-агентов); общие запреты CLI в `cli.json`, shell-проверка IDE/cloud хуком `preToolUse` с отказом при сбое. Скиллы Cursor читает из `.agents/skills` сам |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `agents.cli` | `decision` | `object` | корневой объект `agents.cli` проекта: `claudeVersion`, `codexVersion`, `cursorVersion`, `nodeVersion`; версии CLI и Node для их установки |
| `agents.tools` | `decision` | `object` | корневая карта `agents.tools` проекта: записи `issue-agent` и `alert-triage` с полями `allowedTools` и `disallowedTools` - разрешенные и запрещенные инструменты агента в CI через запятую. Модель и усилие не настраиваются: модель задает комплект по провайдеру (`KINDS` в `agent_provider.py`), усилие - по процессу (`efforts` методологии) |
| `agents.findings` | `decision` | `choice` | попутная находка вне постановки: `same-pr` (по умолчанию) - чинится в этом PR, `separate-pr` - отдельным PR, `report` - называется в PR без починки, `ignore` - не трогается; дефект, внесенный самим PR, чинится в нем при любом значении ([CONTRIBUTING §10](../../../CONTRIBUTING.md#10-находки-по-пути)) |
| `agents.review` | `decision` | `choice` | круги ревью сессии конвейера без флага `--review`: `ci` (по умолчанию) - круги в CI, локальный круг заменяет недоступный и Ready не дает; `local` - круги идут локально и доводят PR до Ready. Читается с базовой ветки задачи. Выключенный модуль `review` на той же базе дает локальный круг и эту настройку не читает |
| `agents.pushback` | `decision` | `choice` | спор агента с постановщиком задачи: `full` - каждое утверждение проверяется, агент называет пробелы и спорные места в архитектуре, требованиях, производительности и сложности кода и предлагает лучшее решение; `critical` (по умолчанию) - оспариваются только отклонения от архитектуры, требований и логики; `none` - агент не спорит. Тем же значением шаг `speckit-market` решает, оспаривать ли ответы `clarify`. Уточнение по вопросу запрашивается один раз: выбор человека закрывает вопрос |
| `agents.envScript` | `fact` | `text` | скрипт проекта от корня репозитория, который по `--export` печатает команды `export` окружения запуска стека. Его подключает после общего тулчейна [`session-env.sh`](../../../.harness/provisioning/scripts/session-env.sh): в файлах старта Bash, в файле окружения удаленной сессии и при запуске серверов MCP с командой; пусто - только общий тулчейн |

`agents.cli` и `agents.tools` - решения `static` ([настройка](harness.md#настройка)); `--reopen agents`
спрашивает их как JSON-объекты, `pack` переносит их в архив под решения. Они лежат в корне манифеста; выбор агента остается
в `modules.agents`, агента CI - в `modules.ci-agent`. Процессы и промпты ссылаются полем `runner` на
исполнителя: `review-code`, `review-requirements`, `review-architecture`, `issue-agent`, `alert-triage`.
Пул машин CI задает отдельный модуль `runners`.

### `ci-agent`

Агент в CI - ответ на вопрос "Какую модель вы используете в CI?"; требует `forge`. Один ответ задает
исполнителя ревью, агента задач и разбора алертов в CI; модель - модель комплекта для провайдера
(`KINDS` в `agent_provider.py`), усилие - по процессу (`efforts` методологии).
Выключен - агентов в CI нет: модули `review`, `issue-agent` и `alert-triage` выключаются, ревью и
задачи ведет агент разработки локально, алерты разбирает дежурный. Установщик подставляет провайдера
в файлы CI.

| Расширение | Что ставит |
| --- | --- |
| `ci-agent/claude-code` | исполнитель Claude Code CLI; секрет `CLAUDE_CODE_OAUTH_TOKEN` |
| `ci-agent/codex` | исполнитель Codex CLI; вход - настройка `ci-agent/codex.auth`; запреты сессии собираются при запуске |
| `ci-agent/cursor` | исполнитель Cursor Agent CLI; секрет `CURSOR_API_KEY`; запреты сессии собираются при запуске |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `ci-agent/codex.auth` | `decision` | `choice` | вход Codex: `api-key` - секрет `CODEX_API_KEY`, его сверяет `forge-setup.py`; `chatgpt` - вход ChatGPT в `CODEX_HOME/auth.json` своего раннера, секрет не нужен; по умолчанию `api-key` |

### `review`

Круг ревью PR агентами в CI; требует `forge` и `ci-agent`. Модуль ставит круг у площадки - `code-review.yml` у
GitHub, `.gitlab/ci/review.yml` с дочерним конвейером `.gitlab/ci/review-round.yml` у GitLab, - его
шаги `review-round.py`, запуск сессии агента `agent-run.py`, план и публикатор вердикта, гейт метки
круга ([ci-cd](../delivery/ci-cd.md)). Выключен - ревью идет локальным кругом скилла `review`. Исполнителя
задает модуль `ci-agent`.

### `issue-agent`

Агент в CI: его будит обращение к агенту в начале комментария или ревью и конец круга ревью на PR
конвейера; требует `forge`, `tracker` и `ci-agent`. Модуль ставит воркфлоу агента у площадки - `issue-agent.yml`
у GitHub, `.gitlab/ci/agent.yml` у GitLab, - и промпт агента; событие GitLab разбирает задача
`harness-event` расширения `forge/gitlab`. Выключен - задачу и PR ведет агент разработчика скиллом
`issue-dev`. Токен записи агента: секрет `AGENT_GITHUB_TOKEN` у GitHub, переменная
`AGENT_GITLAB_TOKEN` у GitLab. Исполнителя задает модуль `ci-agent`.

### `stands`

Стенды проекта: чтение диагностики стенда агентами через MCP. Расширение ставит инструмент
`.harness/mcp-stand/` - экземпляр на стенд со своим конфигом, учетными данными и сетью
([README](../../../.harness/mcp-stand/README.md)) - и сервер MCP каждого стенда у агентов разработки:
`.mcp.json` у Claude Code, `.cursor/mcp.json` у Cursor, `.codex/config.toml` у Codex. Вход агента
разработки - OAuth экземпляра. Выключен - агенты стенды через MCP не читают.

| Расширение | Что ставит |
| --- | --- |
| `stands/mcp-stand` | сервер MCP только для чтения: исходники снимком, телеметрия, состояние хоста и нагрузок, журналы нагрузок |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `stands/mcp-stand.list` | `decision` | `records` | стенды: `name` - имя сервера MCP у агентов, `url` - адрес `<public_url>/mcp` экземпляра, `token` - имя секрета CI с токеном для агентов в CI; обязательны `name` и `url` |

### `mcp`

Внешние серверы MCP агентов разработки. Комплект пишет выбранные серверы в конфиг MCP каждого
включенного агента: `.mcp.json` и `enabledMcpjsonServers` в `.claude/settings.json` у Claude Code,
`.cursor/mcp.json` у Cursor, `.codex/config.toml` у Codex. Имя сервера - имя расширения. Туда же
идут сервер `lsp` стеков `dotnet` и `node` и серверы стендов `stands/mcp-stand`. Выключен - агенты
работают без внешних серверов.

| Расширение | Что ставит |
| --- | --- |
| `mcp/context7` | документация библиотек по версиям, `https://mcp.context7.com/mcp` |
| `mcp/parallel-search` | поиск в вебе, `https://search-mcp.parallel.ai/mcp` |
| `mcp/docker` | шлюз `docker mcp gateway run` к серверам каталога Docker; `configure` предлагает его, когда в дереве есть `Dockerfile` или файл compose. Плагин `docker mcp` там, где его нет, ставит [`ensure-docker-mcp.sh`](../../../.harness/provisioning/scripts/ensure-docker-mcp.sh); запуск шлюза ждет команду `docker` и ответ демона, а при пустом списке каталогов загружает каталог серверов `mcp/docker-mcp-catalog` из реестра Docker Hub, без доступа к реестру или после 45 секунд загрузки шлюз стартует без серверов |
| `mcp/refero` | образцы интерфейсов, `https://api.refero.design/mcp`; требует `frontend` |
| `mcp/mobbin` | образцы интерфейсов приложений, `https://api.mobbin.com/mcp`; требует `frontend` |
| `mcp/aspire` | сервер `aspire agent mcp` для AppHost Aspire; требует `stack/dotnet`, `configure` предлагает его по SDK `Aspire.AppHost.Sdk` в дереве. Aspire CLI без команды `aspire agent` сервер не запускает |

Сервер с командой, `mcp/docker` и `mcp/aspire`, запускает
[`mcp-launch.mjs`](../../../.harness/provisioning/scripts/mcp-launch.mjs): клиент MCP стартует серверы
раньше хука старта сессии и окружение сессии не наследует. На Linux и macOS запуск идет в окружении
[`session-env.sh`](../../../.harness/provisioning/scripts/session-env.sh): общий тулчейн и скрипт
[`agents.envScript`](#agents); на Windows команда берется из PATH пользователя.

### `alert-triage`

Разбор горящих алертов агентом в CI; требует `forge/github`, `tracker/github-issues` и `ci-agent`. Модуль ставит
конвейер `alert-triage.yml`, шаги и инструмент телеметрии `alert-triage.py` и промпт агента. Отбор
читает алерты через Grafana и ведет issue на ключ алерта; агент читает Prometheus, Loki и Tempo
через прокси источников той же Grafana по путям маршрута раннера разбора
([ci-cd](../delivery/ci-cd.md#разбор-алертов)). Выключен - алерты разбирает дежурный. Токен записи
агента - секрет `AGENT_GITHUB_TOKEN`. Исполнителя задает модуль `ci-agent`.

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `alert-triage.grafana` | `decision` | `text` | адрес Grafana; обязательно |
| `alert-triage.auth` | `decision` | `text` | секрет `<пользователь>:<пароль>` Basic маршрута раннера разбора; по умолчанию `GRAFANA_TRIAGE_AUTH`; пустой секрет - анонимный доступ |
| `alert-triage.alertmanager` | `decision` | `text` | имя источника Alertmanager в Grafana для ссылки issue на горящие алерты; пусто - алерты Grafana |
| `alert-triage.header` | `decision` | `text` | заголовок со значением `1` у запроса с учетными данными, по которому край узнает маршрут раннера; пусто - без заголовка |
| `alert-triage.pathsFile` | `fact` | `text` | Python-файл со словарем `GRAFANA_OPS_PATHS` путей маршрута раннера: ключи `prometheus`, `loki`, `tempo`, `alerts`; обязательно |
| `alert-triage.environment` | `decision` | `text` | окружение GitHub Actions заданий отбора и разбора; пусто - без окружения |
| `alert-triage.selectRunnerVar` | `decision` | `text` | переменная репозитория с метками `runs-on` отбора в JSON; пока она пуста, конвейер не запускается; пусто - отбор на `runners/github-actions.triage` |
| `alert-triage.severities` | `decision` | `texts` | значения метки `severity` в порядке приоритета; по умолчанию `["critical", "warning"]` |
| `alert-triage.component` | `decision` | `text` | метка компонента, вторая часть ключа issue; по умолчанию `component` |
| `alert-triage.group` | `decision` | `text` | метка группы ошибки, третья часть ключа и тред уведомления; пусто - ключ без группы |
| `alert-triage.label` | `decision` | `text` | метка issue алертов; по умолчанию `alert` |
| `alert-triage.cooldownHours` | `decision` | `text` | часы после завершенного разбора, в которые новое срабатывание открытого issue ждет в очереди; по умолчанию `24` |
| `alert-triage.limit` | `decision` | `text` | разборов на прогон отбора; по умолчанию `3`, остальные ждут в очереди |
| `alert-triage.notify` | `decision` | `text` | скрипт рассылки с контрактом `.harness/notify/notify.sh` для ссылки на issue; пусто - рассылка модуля `notifications`, без модуля ссылки нет |
| `alert-triage.notifyToken` | `decision` | `text` | секрет токена канала, который шаг `link` передает рассылке переменной с именем секрета; по умолчанию `SLACK_BOT_TOKEN` |
| `alert-triage.notifyChannel` | `decision` | `text` | канал ссылки, переменная `NOTIFY_SLACK_CHANNEL` рассылки; пусто - канал настроек рассылки |
| `alert-triage.notifyThread` | `decision` | `text` | регулярное выражение, по которому рассылка ищет сообщение алерта для треда ссылки, `{}` - имя алерта с группой ошибки; пусто - отдельное сообщение |

### `githooks`

Git-хуки harness. С включенным модулем `apply` ставит `core.hooksPath` = `.githooks`, с выключенным -
снимает его; на старте сессии то же по `lock.json` делает `ensure-repo-deps.sh`. Формат
коммитов ветки проверяет и гейт `commit-format` в Check.

| Расширение | Что ставит |
| --- | --- |
| `githooks/commit-msg` | `.githooks/commit-msg`: выделенная копия у сессии агента в ветке конвенции `git.branchConvention`, формат сообщения через `commit-format.sh` и трейлеры `Issue`, `PR`, `Agent`, `Task-Branch` |
| `githooks/pre-push` | `.githooks/pre-push`: у сессии агента refs и SHA, в ветке конвенции еще регистрация рабочей копии, у всех `git diff --check`; `.githooks/reference-transaction`: refs автора; `.githooks/post-merge`: версия комплекта из lock и очистка завершенных рабочих копий; `.githooks/post-checkout`: версия комплекта из lock после переключения ветки |

### `sdd`

Разработка от спецификации. Выключен - постановку несет задача трекера.

| Расширение | Что ставит |
| --- | --- |
| `sdd/speckit` | Spec Kit: `.specify/`, скиллы `speckit-*`, `specs/README.md`, гейт реестра требований ([delivery/sdd](../delivery/sdd.md)) |

### `prose`

Правила письма человеческого текста. Выключен - текст проверяет ревью.

| Расширение | Что ставит |
| --- | --- |
| `prose/vale` | `.vale.ini`, пакет стиля HumanizedText (канцелярит, клише, артефакты ИИ, стоп-слова, типографика, многословие и другие правила каталога `.vale/styles/HumanizedText/`) и стиль публикаций Publication в `.vale/styles/`, правило `Glossary.yml`, которое `apply` собирает из колонки "Проверка" глоссария проекта (пересборка - `vale-lint.py --glossary`), гейт `prose` (`vale-lint.py`), `ensure-vale.sh` |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `prose/vale.glossaryLevel` | `decision` | `choice` | уровень `HumanizedText.Glossary`: `suggestion` - подсказка без блокировки (по умолчанию), `warning` - блокирует комментарии кода и извлеченные значения JSON и `messages.env`, `error` - также документацию, сообщения коммитов и тексты для поставщика кода; после изменения пересоберите правило `vale-lint.py --glossary` |
| `prose/vale.localeDictionaries` | `decision` | `texts` | имена файлов словарей интерфейса, значения которых проверяет Vale наравне с `messages.env`; по умолчанию `ru.json` |
| `prose/vale.glossaryValueExceptions` | `decision` | `records` | исключения только `HumanizedText.Glossary` для значений реестров; обязательны `path` - путь от корня проекта, `key` - полный ключ, `value` - полное исходное значение и `reason` - причина; по умолчанию пустой список |

Значение по умолчанию позволяет установить комплект в проект с готовыми документами. После вычитки проект
выбирает `error`, чтобы термины стали обязательными в документации, комментариях и извлеченных
значениях JSON и `messages.env`. Тот же уровень действует в `vale-lint.py --stdin`, поэтому при
`error` термины блокируют и сообщения коммитов, и тексты для поставщика кода: это человеческий
текст наравне с документацией. Проверки C#-реестров сообщений `RegistryGlossary` и `UserGlossary`
сохраняют блокирующий уровень `warning`.

ADR правилом `HumanizedText.Glossary` проверяется как любой живой документ: принятое решение
правится на месте и пишется действующими терминами.
Дифф, задевший вход гейта - `vale-lint.py`, глоссарий или путь из поля `inputs`
записи `prose` реестра, - проверяет все файлы, а не только измененные.

Исключение значения срабатывает при точном совпадении пути, полного ключа и всей исходной строки
до нормализации пробелов. Остальные правила Vale продолжают проверять это значение. Изменение
пути, ключа или значения возвращает проверку глоссария; причина объясняет, почему термин допустим.

### `skills`

Вендорные скиллы стека и инструментов в `.agents/skills/`: файлы идут как у поставщика, а имена
скиллов включенных пакетов `configure` пишет в `docs.vendoredSkills`, поэтому гейты не требуют от них
правил проекта. Выключен - скилл вне пакетов проект подключает сам через `docs.vendoredSkills`.

| Расширение | Что ставит |
| --- | --- |
| `skills/aspire` | скиллы Aspire: роутер `aspire`, `aspire-init`, `aspireify`, `aspire-orchestration`, `aspire-deployment`, `aspire-monitoring`; требует `stack/dotnet` |
| `skills/grafana` | скиллы Grafana LGTM: `opentelemetry`, `prometheus`, `promql`, `loki`, `tempo`, `grafana-oss`, `dashboarding`, `alerting-irm` |
| `skills/dotnet-docs` | `microsoft-docs` и `dotnet-inspect`; требует `stack/dotnet` |
| `skills/drawio` | `drawio-skill`: скрипты Python, данные и стили диаграмм |

### `secrets`

Поиск секретов в дереве и истории ветки. Выключен - секреты в дереве ищут только ревью и защита push у
поставщика кода, если она включена.

| Расширение | Что ставит |
| --- | --- |
| `secrets/gitleaks` | заготовку `.gitleaks.toml` и гейт `secret-scan` |

### `shell`

Проверка shell-скриптов.

| Расширение | Что ставит |
| --- | --- |
| `shell/shellcheck` | `check-shell.sh`, `ensure-shellcheck.sh`, гейт `shell-scripts` |

### `release`

Релиз конвейером harness; требует `forge`. Выключен - версию и тег ставит процесс проекта.

| Расширение | Что ставит |
| --- | --- |
| `release/tag-flow` | `release.py` и конвейер релиза площадки - `release.yml` и `release-cut.yml` с заготовкой `project-deploy.yml` у GitHub, `.gitlab/ci/release.yml` с дочерним конвейером `release-pipeline.yml` и заготовкой `project-deploy.yml` у GitLab: версия из тега, срез `release/*`, хотфикс, обратное слияние ([ci-cd](../delivery/ci-cd.md#релиз)); [сессия релиза](../delivery/ci-cd.md#сессия-релиза) - вызов `/release` и skill `release`, без модуля `/release` отказывает |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `release/tag-flow.token` | `decision` | `text` | секрет токена записи, которым задания среза и обратного слияния создают ветку и PR, чтобы на PR сам стартовал Check; только ссылка на секрет, по умолчанию `RELEASE_TOKEN`; `static` - установка не спрашивает, меняет `--reopen release` |

### `notifications`

Уведомления конвейера: каналы - расширения модуля, рассылку ставит `.harness/notify/`
([ci-cd](../delivery/ci-cd.md#уведомления)). Выключен - итог релиза остается в сводке прогона CI.

| Расширение | Что ставит |
| --- | --- |
| `notifications/forge` | комментарий PR у поставщика кода или блок в описании PR; требует `forge` |
| `notifications/slack` | сообщение в канал Slack ботом |

| Настройка | Вид | Форма | Что задает |
| --- | --- | --- | --- |
| `notifications.events` | `decision` | `object` | каналы по видам уведомлений, например `{"release": ["slack"]}`; вид без записи идет во все включенные каналы |
| `notifications/forge.onFailure` | `decision` | `choice` | отказ доставки: `fatal` роняет шаг, `warn` печатает предупреждение |
| `notifications/slack.channel` | `decision` | `text` | канал Slack: литерал или ссылка на переменную окружения; переменная `NOTIFY_SLACK_CHANNEL` уведомления его заменяет |
| `notifications/slack.token` | `decision` | `text` | токен бота: только ссылка на секрет, по умолчанию `SLACK_BOT_TOKEN` |
| `notifications/slack.onFailure` | `decision` | `choice` | отказ доставки: `fatal` роняет шаг, `warn` печатает предупреждение |

## Внешние зависимости

Версии здесь не записываются: их держат машинные источники.
Сверку с машиной печатает `doctor` установщика комплекта; зависимость невключенного пути не проверяется.

| Зависимость | Путь | Источник версии |
| --- | --- | --- |
| git, bash, Python | `core` | минимум - `dependencies` в `kit.json` архива комплекта |
| Vale | `prose/vale` | `VALE_VERSION` в `ensure-vale.sh` |
| shellcheck | `shell/shellcheck` | `SHELLCHECK_VERSION` в `ensure-shellcheck.sh` |
| gitleaks или Docker | `secrets/gitleaks` | образ по digest в `check-all.py`: на Linux бинарь из образа, на macOS и Windows скан в контейнере; без Docker - `GITLEAKS_VERSION` в `ensure-gitleaks.sh` |
| Node.js, npm, ESLint, typescript-eslint | `stack/node` | `.node-version`, `devEngines` и `devDependencies` корневого `package.json` |
| .NET SDK | `stack/dotnet` | `global.json` решения |
| CSharpier | `stack/dotnet` | `.config/dotnet-tools.json`: запись `csharpier` ставит комплект, остальные инструменты манифеста - проектные |
| PowerShell | `stack/dotnet` | `PWSH_VERSION` в `ensure-pwsh.sh`; нужен только хуку очистки кода .NET перед коммитом |
| GitHub CLI `gh` | `forge/github` | не закреплена: адаптер GitHub берет `gh` с PATH |
| Spec Kit CLI | `sdd/speckit` | `.specify/init-options.json`; нужен только обновлению поставки `.specify/` |
| Claude Code CLI | `ci-agent/claude-code` | `agents.cli.claudeVersion` в `.harness/project/manifest.json`; ставит `ensure-agent-cli.sh` |
| Codex CLI | `ci-agent/codex` | `agents.cli.codexVersion` в `.harness/project/manifest.json`; ставит `ensure-agent-cli.sh` |
| Cursor Agent CLI | `agents/cursor`, `ci-agent/cursor` | `agents.cli.cursorVersion` в `.harness/project/manifest.json`, sha256 пакетов в `ensure-agent-cli.sh` |
| csharp-ls | `stack/dotnet` | `installation.csharp` в `.lsp-mcp.json` |
| typescript-language-server, TypeScript | `stack/node` | `installation.npm` в `.lsp-mcp.json` |
| lsp-mcp-server, Node.js | `stack/dotnet`, `stack/node` | `installation.npm` в `.lsp-mcp.json`; Node.js моста - с PATH |
| Docker с плагином `docker mcp`, Node.js | `mcp/docker` | `docker` и Node.js запуска `mcp-launch.mjs` - с PATH; плагин, который уже отвечает, остается, иначе ставится версия `DOCKER_MCP_VERSION` из `ensure-docker-mcp.sh` |
| Aspire CLI, Node.js | `mcp/aspire` | не закреплены: сервер берет `aspire` с PATH окружения сессии (`agents.envScript`), Node.js запуска `mcp-launch.mjs` - с PATH |
| пакет `mcp`, образ Python | `stands/mcp-stand` | `.harness/mcp-stand/requirements.txt`, digest образа в `.harness/mcp-stand/Dockerfile` |

## Конвейер issue-dev

Композиции объявлены в [`.harness/pipelines.json`](../../../.harness/pipelines.json): только ссылки
на процессы, зависимости и переходы. Определения процессов, промпты и политики лежат в
[`.harness/methodology.json`](../../../.harness/methodology.json); исполнители и версии CLI - в
`agents` манифеста проекта. Связи сверяет
[`check-pipeline.py`](../../../.harness/scripts/check/check-pipeline.py), исполнение и подтверждения стадий -
[`pipeline-run.py`](../../../.harness/scripts/pipeline/pipeline-run.py), инструкции агента -
[`issue-dev`](../../../.agents/skills/issue-dev/SKILL.md).
[`forge-setup.py`](../../../.harness/scripts/release/forge-setup.py) ставит недостающие метки, сверяет по
именам секреты включенных расширений, у GitLab заводит вебхук событий и печатает владельцу то, чего
сделать не может: защиту веток и свои раннеры.

| Настройка GitHub | Значение |
| --- | --- |
| метки | `processes.review.label`, `processes.review.failedLabel`, `processes.review.managedLabel`, `processes.review.fallback.label` и `processes.merge.steps[merge].handoff.label` методологии; с модулем `alert-triage` - `alert-triage.label`; с `tracker/github-issues` - метки задачи `policies.issues.types` и `policies.issues.priorities` с их описаниями |
| секрет `CLAUDE_CODE_OAUTH_TOKEN` | исполнитель `claude-code` ревью, агента задач и разбора алертов |
| секрет `CURSOR_API_KEY` | исполнитель `cursor` |
| секрет `CODEX_API_KEY` | исполнитель `codex` с `auth` = `api-key` |
| секрет `AGENT_GITHUB_TOKEN` | правки, комментарии, метки и PR агента задач и разбора алертов: права `contents`, `pull-requests` и `issues` на запись |
| секрет `release/tag-flow.token` | с модулем `release` - по умолчанию `RELEASE_TOKEN`: ветки и PR среза и обратного слияния от учетной записи владельца токена; fine-grained PAT с правами `contents`, `pull-requests` и `workflows` на запись ([ci-cd](../delivery/ci-cd.md#релиз)) |
| секреты разбора алертов | с модулем `alert-triage` - `alert-triage.auth` и `alert-triage.notifyToken`, в окружении `alert-triage.environment`, если оно задано; переменная `alert-triage.selectRunnerVar` - метки раннеров отбора в JSON |
| секрет `ISSUE_LINK_TOKEN` | привязка ветки к задаче и закрытие задач после мержа; без него воркфлоу берут `GITHUB_TOKEN`, и при нехватке его прав привязка отказывает в журнале прогона, а закрытие - предупреждением |
| защита ветки | ставит владелец по списку, который печатает `forge-setup.py` |

| Настройка GitLab | Значение |
| --- | --- |
| метки | те же, что у GitHub |
| переменная `GITLAB_TOKEN` | токен конвейера с правом `api`: круг ревью, события, релиз; protected и masked с окружением `harness`; с ролью Maintainer `forge-setup.py` заводит вебхук событий |
| переменная секрета релиза | с модулем `release` - секрет `release/tag-flow.token`, по умолчанию `RELEASE_TOKEN`: токен с правом `api` для ветки и MR среза и обратного слияния, protected и masked с окружением `harness`; значение `{"secret": "GITLAB_TOKEN"}` берет токен конвейера |
| переменная `AGENT_GITLAB_TOKEN` | с модулем `issue-agent` - токен учетной записи агента с правом `api` и ролью Developer: правки, комментарии и MR агента; protected и masked с окружением `harness*` |
| переменная `GITLAB_READ_TOKEN` | с модулем `review` - токен с правами `read_api` и `read_repository` и ролью Reporter, masked: задания проходов круга читают им запрос, его голову и результаты на ней; `harness-check` токена не берет и данных запроса не читает |
| переменная секрета трекера | с `tracker/jira` - секрет `tracker/jira.token`, по умолчанию `JIRA_TOKEN`, protected и masked, значения по окружениям: `harness` - токен конвейера, `harness-agent` - отдельный токен учетной записи агента, `review-read` - токен только для чтения задач для проходов круга. |
| переменные токенов стендов | с модулем `stands` - имена из `stands/mcp-stand.list[].token`: protected и masked с окружением `harness*` |
| переменные исполнителя | `CLAUDE_CODE_OAUTH_TOKEN`, `CURSOR_API_KEY` или `CODEX_API_KEY` (у `codex` с `auth` = `api-key`) по расширению модуля `ci-agent` |
| вебхук событий | события Merge request и Comments на адрес триггера конвейера ветки `git.baseBranch` ([ci-cd](../delivery/ci-cd.md#события-gitlab)); `forge-setup.py` переносит прежний вебхук комплекта с другой ветки |
| версия | GitLab 16.10 и новее: `workflow:rules:auto_cancel` |
| свои задания и `workflow` проекта | в файлах `include` с правилом `if: $HARNESS_PIPELINE == null`, не в `.gitlab-ci.yml` ([ci-cd](../delivery/ci-cd.md#события-gitlab)) |

Роль Maintainer видит значения переменных CI/CD: учетные записи `AGENT_GITLAB_TOKEN` и `GITLAB_READ_TOKEN`
ее не получают.
