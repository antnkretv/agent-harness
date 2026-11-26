# Окружение агента: подготовка сессии и раннера

> **Тип (Diataxis):** Справочник

Документ описывает, как облачная сессия агента и раннер CI получают тулчейн проекта. Набор шагов
задает манифест [`.harness/project/manifest.json`](../project/manifest.json): решение .NET
`backend/dotnet.solution` и `.node-version` в корне, а установщики инструментов приходят с модулями,
которым инструмент нужен. Версии инструментов живут в машинных источниках, установщики -
официальные.

## Python

Выбор интерпретатора задает [python.sh](../lib/python.sh). В Bash вызывают
`harness_python` после подключения библиотеки, из CI, хуков и терминала -
[python.cmd](../hooks/python.cmd). Адаптер использует ту же библиотеку в Bash,
а в Windows CMD передает аргументы выбранному интерпретатору. Из PowerShell вызывают
[python.ps1](../hooks/python.ps1): он сохраняет аргументы при передаче тому же
launcher, включая кавычки, пустые строки и кириллицу, и возвращает код завершения Python.

`HARNESS_PYTHON` задает явный путь к интерпретатору, например Python виртуального окружения.
Он сохраняется в режимах `review`, `reply` и при переходе через `--read-only-fallback`; фильтрация учетных данных остается прежней.
Без него Windows использует `py -3`, а без Python Launcher - первый `python` из PATH вне WindowsApps;
Linux и macOS - `python3`. Заглушка WindowsApps берется, только когда другого `python` нет: она
сама сообщает, что Python не установлен. Версию выбранного интерпретатора проверяет `doctor` через ту же обертку. Дочерние процессы Python используют `sys.executable`, сохраняя выбранное окружение.
Стандартные потоки и кодировка файлов по умолчанию используют UTF-8, включая Windows.
Ненулевой код Python возвращается вызывающему; другой интерпретатор после ошибки не выбирается.

Библиотека входит в ядро архива и устанавливается в `.harness/lib`. Автономные поставки проекта
включают тот же файл при упаковке; зависимости от checkout исходников комплекта у них нет.

## Две фазы

| Фаза | Скрипт | Когда идет | Что делает |
| --- | --- | --- | --- |
| снапшот окружения | [`cloud-setup.sh`](../provisioning/scripts/cloud-setup.sh) | один раз при сборке окружения облачного агента, после `install` комплекта по lock | Vale, shellcheck; .NET SDK и PowerShell при решении .NET; Node и npm при `.node-version`; языковые серверы стеков `dotnet` и `node`; затем шаги проекта `setup.snapshot` манифеста и плагин `docker mcp` модуля `mcp/docker`, когда `docker` стоит: шаг проекта ставит Docker раньше плагина |
| старт сессии | [`session-start.sh`](../hooks/session-start.sh) | каждый старт сессии: [ссылки агентов](#ссылки-агентов) и уборка задач в фоне; остальное - только удаленной | тулчейн по манифесту, окружение сессии с расширением `agents.envScript`, языковые серверы стеков, зависимости репозитория и `core.hooksPath`, затем шаги проекта `setup.session` манифеста |

`session-start.sh` пишет вывод шагов в журнал `.git/harness/session-start.log`, а в сессию отдает
одну строку итога; отказ шага подготовки или шага проекта он называет вместе с последними строками
вывода этого шага.

`cloud-setup.sh` называет неудачный шаг вслух. Отказ обязательного шага - Vale, shellcheck, .NET SDK,
Node, npm или профиля Bash - дает ненулевой код: окружение без инструмента гейтов не считается
подготовленным. PowerShell, языковые серверы и плагин `docker mcp` необязательны: их отказ называется, а подготовка
продолжается. Хук старта сессии синхронный: сборки и гейты не идут, пока подготовка не закончена.

Шаг, который не переживает снапшот окружения, например демон контейнеров, проект вписывает командой в
список `setup.session` [манифеста](harness-parameters.md#манифест-проекта). Хук старта удаленной сессии и старт машины
Cursor (`start` в `.cursor/environment.json` зовет `.cursor/start.sh --steps`) запускают команды из корня
репозитория по порядку списка после общего тулчейна; отказ шага называется и старт не останавливает. Без флага, например из
локальной настройки worktree, `.cursor/start.sh` шаги не запускает.

Снапшот хранит файлы, а экспорт переменных установки в новые терминалы не переносит.
`cloud-setup.sh` и удаленный хук старта подключают окружение сессии `session-env.sh` в `~/.bashrc` и первый существующий файл
`~/.bash_profile`, `~/.bash_login`, `~/.profile` (без них создает `~/.profile`). Это работает
для login-оболочек и интерактивных терминалов Bash, включая Cursor Cloud. Повторная установка
заменяет свой блок; PATH и переменные .NET вычисляются при старте оболочки. Установка от root
меняет его файлы старта, как и каталоги тулчейна; запускать установку нужно от пользователя сессии.
Проект добавляет свои переменные скриптом [`agents.envScript`](harness-parameters.md#agents):
скрипт вызывается с `--export`, печатает команды `export` и подключается после общего окружения;
без файла расширения остается общий тулчейн.

Команды агента удаленной сессии идут в неинтерактивной оболочке, которая файлы старта Bash не
читает. Поэтому хук старта дописывает то же окружение в файл окружения сессии `AGENT_ENV_FILE`:
тот же [`session-env.sh --export`](../provisioning/scripts/session-env.sh) печатает общий
тулчейн и расширение проекта. Отказ расширения хук называет, в сессии остается общий тулчейн.
Серверы MCP с командой получают это окружение при [запуске](harness-parameters.md#mcp).

### Ссылки агентов

Скиллы и правила агентов по путям лежат одним оригиналом в `.agents/skills/` и `.agents/rules/`.
Ссылки на них - `.claude/skills`, `.claude/rules/harness-<имя>.md` и `.cursor/rules/harness-<имя>.mdc` -
создает [`agent_links.py`](../lib/agent_links.py): ссылку, которая ведет не туда, он
заменяет, ссылку на исчезнувшее правило снимает. На Windows, если символическую ссылку создать
нельзя, он ставит junction для каталога и жесткую ссылку для файла. В git ссылки не попадают: в
свежем клоне они появляются на первом старте сессии, и если агент прочитал правила раньше хука, правила
по путям видны со следующей сессии. Скрипт зовут:

- `session-start.sh` первым шагом каждого старта сессии: Claude Code, Codex и `sessionStart` Cursor.
  Отказ останавливает старт с кодом 1 до подготовки удаленной сессии, и тулчейн с зависимостями тогда
  не ставятся. Каталог на месте ссылки, например копия скиллов в `.claude/skills/`, - такой отказ:
  перенесите свои скиллы в `.agents/skills/` и удалите каталог; копии скиллов установки `harness-*` не
  переносят, а удаляют: они живут только в архиве комплекта;
- хук `workspaceOpen` Cursor при открытии проекта и `.cursor/start.sh` на старте машины Cursor Cloud,
  где `sessionStart` не срабатывает; отказ в `start.sh` старт машины не останавливает;
- `agent_runtime` перед сессией агента в CI, кроме сессии ревью: она только читает дерево круга.
  Отказ завершает сессию с причиной.

## Подключение по агентам

Адаптер агента ставит расширение модуля `agents`.

| Агент | Снапшот | Старт сессии |
| --- | --- | --- |
| Claude Code (web), `agents/claude-code` | поле setup script окружения на claude.ai/code: [сниппет](#setup-script-облачного-окружения) | хук `SessionStart` в [`.claude/settings.json`](../../.claude/settings.json) зовет [`.claude/hooks/session-start.sh`](../../.claude/hooks/session-start.sh) |
| Cursor Cloud, `agents/cursor` | `install` в [`.cursor/environment.json`](../../.cursor/environment.json) | `start` - [`.cursor/start.sh`](../../.cursor/start.sh); хук `sessionStart` в [`.cursor/hooks.json`](../../.cursor/hooks.json) зовет тот же адаптер, что Codex; [ссылки агентов](#ссылки-агентов) создают также хук `workspaceOpen` и `start.sh` |
| Codex, `agents/codex` | поле setup script облачного окружения Codex: тот же [сниппет](#setup-script-облачного-окружения) | [`.codex/hooks/session-start.py`](../../.codex/hooks/session-start.py) |

Codex исполняет хуки проекта, в том числе [сессии конвейера](../README.md#сессия-конвейера),
только из доверенного слоя `.codex/` и после одобрения их определения командой `/hooks`.

Адаптер агента переводит его переменные в общие: `AGENT_PROJECT_DIR`, `AGENT_REMOTE` (удаленная
сессия), `AGENT_ENV_FILE` (файл, в который сессия дописывает окружение). В локальной сессии
хук старта только запускает в фоне уборку влитых задач; остальные шаги идут лишь в удаленной.

Установщик закрепленного Cursor CLI в комплекте поддерживает Linux/macOS. На native Windows Cursor CLI
ставит и обновляет его собственный установщик, а проходы ревью и `agent-run.py` запускают, как его шим,
последнюю версию из каталога `versions` рядом с `cursor-agent.cmd`.

Локальные проходы ревью Codex на Windows читают `windows.sandbox` и явное значение
`features.prefer_mxc` из `~/.codex/config.toml`. Без явного предпочтения используется
`features.prefer_mxc=false`, как при обычном запуске CLI. Проверка запуска использует `cmd.exe`,
сохраняет системные переменные Windows и работает с теми же настройками песочницы, что проход.
Режим остается `read-only`; варианты песочницы описаны в
[настройке Windows](https://learn.chatgpt.com/docs/windows/windows-sandbox).

### Setup script облачного окружения

Поле setup script хранится в настройках окружения у поставщика агента, а не в репозитории. Поле
находит checkout, ставит комплект по lock - `cloud-setup.sh` в git не лежит - и передает ему управление:

```bash
#!/bin/bash
for d in "$PWD" /home/user/* /workspace/* "$HOME"/*; do
  [ -f "$d/.harness/ah.py" ] || continue
  python3 "$d/.harness/ah.py" install && exec "$d/.harness/provisioning/scripts/cloud-setup.sh"
  exit 0
done
exit 0
```

В окружении облачного агента должен быть один репозиторий проекта: с несколькими репозиториями агент
стартует в общем каталоге над клонами, не видит хуков старта и навыков проекта, и комплект не ставится.

Не найденный checkout или неудачная установка не роняют окружение: комплект и тулчейн ставит хук
старта сессии. Шаг `install` Cursor Cloud в `.cursor/environment.json` тоже сначала ставит комплект, но его отказ
останавливает этот шаг: `cloud-setup.sh` без комплекта не запускается.

### Setup script и хук старта в Claude Code web

| | Setup script окружения | Хук `SessionStart` |
| --- | --- | --- |
| Где настроен | диалог окружения на claude.ai/code | [`.claude/settings.json`](../../.claude/settings.json) репозитория |
| Когда идет | до запуска Claude Code, только при промахе кеша окружения | после запуска Claude Code, на каждой сессии, включая resume |
| Где идет | только облачные сессии | облачные и локальные сессии |
| Ненулевой код | создание сессии отказывает | сессия стартует, ошибка видна в stderr |
| Бюджет | около 5 минут | `timeout` хука в секундах: 900 |

В setup script идет общий тулчейн, в хук - то, что зависит от ветки: зависимости репозитория и
`core.hooksPath`. Без setup script хук повторяет установку общего тулчейна и зависимостей.
Подготовку дополнительных инструментов и окружения проекта задает сам проект. Кеш окружения пересобирается
при правке setup script, при правке списка разрешенных хостов и примерно раз в семь дней.

Список разрешенных хостов окружения включает источники установщиков: `github.com` (Vale,
shellcheck, gitleaks, PowerShell, плагин `docker mcp`), `nodejs.org` (Node.js), `dot.net` и хосты загрузки
`dotnet-install.sh` (.NET SDK). Без хоста установщик отказывает, и снапшот собирается без
инструмента.

## Установщики

Каталог [`.harness/provisioning/scripts/`](../provisioning/scripts/cloud-setup.sh):

| Скрипт | Путь | Что ставит | Источник версии |
| --- | --- | --- | --- |
| `ensure-vale.sh` | `prose/vale` | Vale в `~/.local/bin` | `VALE_VERSION` скрипта |
| `ensure-shellcheck.sh` | `shell/shellcheck` | shellcheck в `~/.local/bin` | `SHELLCHECK_VERSION` скрипта |
| `ensure-gitleaks.sh` | `secrets/gitleaks` | gitleaks в `~/.local/bin` для скана секретов без Docker | `GITLEAKS_VERSION` скрипта |
| `ensure-dotnet-sdk.sh` | `stack/dotnet` | .NET SDK официальным `dotnet-install.sh` с проверкой подписи | `global.json` каталога решения |
| `ensure-pwsh.sh` | `stack/dotnet` | PowerShell для хука очистки кода .NET | `PWSH_VERSION` скрипта |
| `ensure-node.sh` | `stack/node`, `review`, `issue-agent`, `alert-triage` | Node.js через NVM образа или архив nodejs.org | `.node-version`, без файла - `agents.cli.nodeVersion` |
| `ensure-npm.sh` | `stack/node` | npm из `devEngines` корневого `package.json` | `devEngines` |
| `ensure-lsp.sh` | `stack/dotnet`, `stack/node` | мост MCP `lsp-mcp.mjs` и языковые серверы из `.lsp-mcp.json`: npm-пакеты и `csharp-ls` в `HARNESS_LSP_HOME`, по умолчанию `~/.local/share/harness-lsp` | `.lsp-mcp.json` |
| `ensure-agent-cli.sh` | `review`, `issue-agent`, `alert-triage` | CLI агента CI `claude-code`, `codex` или `cursor`: npm-пакет или архив выпуска со сверкой sha256; без npm сначала Node через `ensure-node.sh`, затем установка CLI в npm prefix окружения | `agents.cli` в `.harness/project/manifest.json` |
| `ensure-repo-deps.sh` | `core` | `npm ci` при корневом `package.json`, `dotnet restore` при решении, `core.hooksPath` по модулю `githooks` в `lock.json` | lock-файлы |
| `toolchain-env.sh` | `core` | PATH и `DOTNET_ROOT` для поставленного тулчейна; `VSTEST_ARTIFACTS_BASE_DIRECTORY` там, где нет `/run/user/<uid>` | - |
| `ensure-toolchain-profile.sh` | `core` | подключение окружения сессии `session-env.sh` в файлах старта Bash | - |
| `session-env.sh` | `core` | окружение сессии агента: общий тулчейн и расширение `agents.envScript`; `--export` печатает его, `--exec` запускает в нем команду | - |
| `ensure-docker-mcp.sh` | `mcp/docker` | плагин `docker mcp` в каталоге плагинов Docker CLI, когда он еще не отвечает; `--wait` ждет команду `docker` и ответ демона; каталог серверов, который не загрузился за `DOCKER_MCP_CATALOG_WAIT` секунд (по умолчанию 45), пропускается | `DOCKER_MCP_VERSION` скрипта |

Раннер GitHub Actions ставит тулчейн действиями `.github/actions/setup-node` и
`.github/actions/setup-dotnet` из тех же источников версий; Vale, shellcheck и CLI агента - теми же
скриптами с ключом `--ci`. Задачи GitLab CI `harness-check` и `harness-agent` ставят
тулчейн скриптами `ensure-*` и берут PATH из `toolchain-env.sh`. CLI агента ставит
`ensure-agent-cli.sh <исполнитель>` в каждой задаче, которая запускает сессию агента.
В той же оболочке GitLab вызывает `harness_ensure_agent_cli <исполнитель>` из `toolchain-env.sh`:
функция запускает этот установщик и добавляет каталог проверенного CLI первым в PATH.
Так используется фактический npm prefix, включая fallback, даже если прежний CLI остался
в недоступном для записи каталоге. На Windows fallback CLI находится в `~/.local/bin`,
отдельно от каталога Node `~/.local/node`. Python локального ревью преобразует PATH Git Bash
в формат Windows и также ставит проверенный каталог первым.

## Источники версий

Версия живет в одном машинном источнике своего стека. Машинные контракты и версии в документации
сверяет гейт `tool-pins` ([`check-tool-pins.py`](../scripts/check/check-tool-pins.py)).
Проверка версий в Markdown пропускает `docs.historicalPrefixes`: это граница охвата гейта, а не
разрешение копировать версии в датированные снимки. Соблюдение текстового правила в этих снимках
проверяет ревью. ADR гейт проверяет как живой документ.

Источники и правила:

- .NET SDK - `global.json` каталога решения.
- NuGet-пакеты - `<PackageVersion>` в ближайшем вверх от проекта `Directory.Packages.props`. В
  csproj остаются только `<PackageReference>` и их metadata; `Version` и `VersionOverride` в
  проектах запрещены.
- Центральный каталог перечисляет каждый используемый пакет и не держит неиспользуемых. Проект без
  `Directory.Packages.props` выше себя - отказ: каталог заводится тем же изменением, что и первый
  `PackageReference`.
- Node.js - `.node-version` корня, полный semver без префикса `v`. `engines.node` корневого
  `package.json` повторяет его мажор: `>=N.0.0 <N+1`, где N - мажор `.node-version`.
- npm-зависимости - корневой `package.json` и единственный lock-файл корня.
- Воркфлоу ставят Node только действием `.github/actions/setup-node`; прямой вызов
  `actions/setup-node` - отказ.
- Образы и скрипты читают версию из этих файлов и копий не держат.
- Документы в `docs/`, в каталогах приложений и корневой `README.md` не записывают версии пакетов,
  SDK, runtime и образов, в том числе в датированных снимках. Точную среду называет ссылка на
  коммит, артефакт выкладки или машинный вывод. Исключение - решения в `docs/platform/adr/`.
- Чужие действия и плагины агента в воркфлоу и действиях, которым отдан секрет подписки агента,
  закреплены на digest коммита.

## Разбор отказа

- Сервер MCP с командой не подключился: запуск `mcp-launch.mjs` идет через
  Node.js, а на Linux и macOS еще и через `bash`; без них на PATH процесса агента сервер не стартует.
  Причину отказа `ensure-docker-mcp.sh` и расширения окружения запуск пишет в stderr сервера.
- `vale-lint.py` без Vale печатает, как его поставить. В CI Vale ставит шаг `ensure-vale.sh --ci`
  до гейтов; без Vale гейт `prose` правила письма пропускает.
- `ensure-dotnet-sdk.sh` при расхождении подписи установщика останавливается; недоступность самой
  GPG-подписи на Linux/macOS он называет вслух и продолжает. На Windows перед запуском обязательна
  действительная Authenticode-подпись Microsoft Corporation; отсутствие или ошибка подписи останавливает установку.
- Установщик .NET из зеркала задает `DOTNET_INSTALL_URL`. Подпись и ключ скрипт ищет рядом с
  установщиком по его имени (`.sig`, `.asc`), другие адреса задают `DOTNET_INSTALL_SIG_URL` и
  `DOTNET_INSTALL_KEY_URL`, отпечаток ключа - `DOTNET_INSTALL_KEY_FPR`. С зеркалом без подписи
  на Linux/macOS SDK ставится без проверки: недоступная подпись отказом не считается. На Windows
  зеркало сохраняет Authenticode-подпись; явно заданные адреса GPG-подписи и ключа требуют также
  успешной GPG-проверки.
- `ensure-npm.sh` без `devEngines` принимает npm, который пришел с Node.
- `ensure-repo-deps.sh` без корневого `package.json` пропускает npm, без решения - `dotnet restore`.
- Без PowerShell хук очистки кода .NET перед коммитом не запускается; подготовка сессии называет
  отказ `ensure-pwsh.sh` вслух, а сессию не останавливает.
- `ensure-lsp.sh` без Node.js отказывает, а без .NET SDK отказывает после установки моста: сервер C#
  не встает. Подготовка сессии называет отказ вслух, агент работает без LSP или без C#. Сервер `lsp`
  агента доставляет недостающее сам при первом запуске.
