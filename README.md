# agent-harness

Комплект ставит в репозиторий проекта правила, гейты и конвейер задачи `issue-dev` для работы ИИ-агентов.
В git проекта лежат только его настройки и тонкая оболочка: `.harness/project/`, `.harness/state/lock.json`,
загрузчик `.harness/ah.py`, workflow CI, блоки комплекта в `AGENTS.md` и настройки агентов. Остальное
`ah install` ставит по закрепленной в lock версии в неотслеживаемые пути.

## Быстрый старт

Скачать выпуск и сверить sha256, распаковать вне репозитория проекта:

```bash
V=<версия>; URL=https://github.com/antnkretv/agent-harness/releases/download/v$V
curl -fLO "$URL/agent-harness-$V.tar.gz" -fLO "$URL/agent-harness-$V.tar.gz.sha256"
sha256sum -c "agent-harness-$V.tar.gz.sha256"
mkdir -p "$HOME/.cache/harness" && tar -xzf "agent-harness-$V.tar.gz" -C "$HOME/.cache/harness"
```

Подключить проект, один раз: в сессии агента в клоне проекта попросить выполнить
`$HOME/.cache/harness/agent-harness-<версия>/skills/harness-init/SKILL.md` с путем к архиву и его sha256.
Скилл ведет этап 1 по [AGENT.md](AGENT.md): рабочая копия ветки установки, `doctor`, вопросы владельцу,
установка и PR. Без чата те же шаги идут командами [STAGE-1.md](installer/stages/STAGE-1.md).

Поставить у разработчика, в CI и в облачной сессии; хуки `post-checkout`, `post-merge` и старт сессии агента
зовут ее сами:

```bash
python3 .harness/ah.py install   # Windows: py -3 .harness/ah.py install
```

## Обновление и смена настроек

```bash
python3 .harness/ah.py upgrade [--version <версия>]   # ветка, новая версия, lock и черновой PR
```

Обновление из архива и смена модулей - скиллы `harness-sync` и `harness-configure` архива
([AGENT.md](AGENT.md#обновление-и-смена-настроек)).

## Выпуск

```bash
git switch master && git merge --no-ff origin/develop    # version в kit.json поднята в develop
git tag v<версия> && git push origin master v<версия>    # workflow release.yml публикует архив и sha256
```

## Подробнее

- Установленный слой, установка по lock и обновление - [harness.md](payload/docs/platform/agents/harness.md).
- Модули, архив под решения, манифест и коды установщика - [PARAMETERS.md](PARAMETERS.md).
- Сборка, проверка, выпуск и устройство комплекта - [MAINTAINING.md](MAINTAINING.md).
