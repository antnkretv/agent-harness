# agent-harness

Комплект ставит в репозиторий проекта правила, гейты и конвейер задачи `issue-dev` для работы ИИ-агентов.
В git проекта лежат только его файлы, настройки и тонкая оболочка: `.harness/project/`, `.harness/state/lock.json`,
загрузчик `.harness/ah.py`, workflow CI и настройки агентов. Остальное, в том числе правила агентов
`.harness/AGENTS.md` и документы комплекта `.harness/docs/`, `ah install` ставит по закрепленной в lock версии
в неотслеживаемые пути; `AGENTS.md` проекта подключает их одной строкой.

## Быстрый старт

Подключить проект, один раз: в корне клона проекта выполнить команду установки последнего выпуска.

```bash
curl -fsSL https://github.com/antnkretv/agent-harness/releases/latest/download/install.sh | bash
```

На Windows - в PowerShell:

```powershell
irm https://github.com/antnkretv/agent-harness/releases/latest/download/install.ps1 | iex
```

Скрипт скачивает архив выпуска, сверяет его sha256 с опубликованным и запускает `ah init`: вопросы о
модулях и настройках идут подряд в терминале, найденное в дереве установщик отвечает сам, а после последнего
ответа ставит комплект. Ответы остаются в `.harness/project/answers.json`: повторный запуск берет их и
спрашивает только новое. Нужны git, Python 3.11+ и pip.

Установка лежит в клоне незакоммиченной. Ветку установки, поставщика кода и PR накатки дальше ведет агент:
в сессии агента в том же клоне попросить выполнить `.harness/kit/skills/harness-init/SKILL.md` - скилл
поставленной версии в кеше. Скилл переносит установку в ветку `chore/harness-install` и ведет этап 1 по
[AGENT.md](AGENT.md); без чата те же шаги идут командами [STAGE-1.md](installer/stages/STAGE-1.md).

Архив из `develop` вместо выпуска собирает `tools/kit.py`:

```bash
git clone -b develop https://github.com/antnkretv/agent-harness "$HOME/src/agent-harness"
python3 "$HOME/src/agent-harness/tools/kit.py" build --commit --out "$HOME/.cache/harness"   # архив и sha256
python3 "$HOME/.cache/harness/agent-harness-<версия>/ah.py" init --target . --archive "$HOME/.cache/harness/agent-harness-<версия>.tar.gz"
```

Поставить у разработчика, в CI и в облачной сессии; хуки `post-checkout`, `post-merge` и каждый хук агента
(`.harness/ah.py hook`) зовут ее сами:

```bash
python3 .harness/ah.py install   # Windows: py -3 .harness/ah.py install
```

## Обновление и смена настроек

```bash
python3 .harness/ah.py upgrade --commit develop   # или --version <версия>: ветка, новая версия, lock и черновой PR
```

Смена модулей и настроек - `python3 .harness/ah.py change` в терминале: все вопросы с прежними ответами,
затем установка. Обновление из архива - скилл `harness-sync` архива
([AGENT.md](AGENT.md#обновление-и-смена-настроек)).

## Выпуск

```bash
git switch master && git merge --ff-only origin/develop   # version в kit.json поднята в develop
git push origin master                                    # release.yml ставит тег v<версия>, публикует архив и sha256
```

## Подробнее

- Установленный слой, установка по lock и обновление - [harness.md](payload/.harness/docs/harness.md).
- Модули, архив под решения, манифест и коды установщика - [PARAMETERS.md](PARAMETERS.md).
- Сборка, проверка, выпуск и устройство комплекта - [MAINTAINING.md](MAINTAINING.md).
