---
name: harness-configure
description: Смена модулей и настроек установленного harness - вопросы модуля заново, apply комплектом установленной версии, приемка и PR. Использовать, когда проект включает, выключает или меняет модуль, расширение или настройку.
---

# Смена настроек harness

Скилл лежит в каталоге `skills/` архива комплекта, и агент выполняет его из архива установленной
версии, как `harness-init`: скилл никуда не копируется. Модули и их настройки - каталог `.harness/state/modules.json` проекта,
ответы и их происхождение - `.harness/project/answers.json`, манифест - `.harness/project/manifest.json`.
Манифест строится из ответов и меняется ответом, а не правкой руками.


На Windows `configure` с ответом, значение которого начинается с `/` (`--answer docs.generatedSegments=/node_modules/`),
идет из Git Bash с префиксом `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` в строке этой команды, а не в
окружении сессии (`AGENT.md`, начало).

## 1. Комплект

Вопросы и файлы модуля дает архив установленной версии: `version` и `digest` из `kit` в
`.harness/state/lock.json`. Путь к архиву и его sha256 - аргументы вызова, чего нет - спроси
владельца; после сверки sha256 распакуй архив вне репозитория в чистый каталог:

```bash
sha256sum <архив>                                   # не совпало с данным sha256 - стоп
KIT="$HOME/.cache/harness/$(basename <архив> .tar.gz)"
rm -rf "$KIT" && mkdir -p "$HOME/.cache/harness" && tar -xzf <архив> -C "$HOME/.cache/harness"
```

`version` или `digest` в `$KIT/kit.json` другие, чем в `lock.json`, - стоп: архив не тот.

## 2. Что меняется

Модуль каталога, который меняет владелец. Не назван - спроси одним вопросом.

Вне выделенной копии выдели ее до правок:
`"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" workspace --target <repo> --base origin/<база> --key harness-configure-<модуль>`;
продолжи в `path` из JSON результата.

## 3. Вопросы модуля

```bash
"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" configure --target . --reopen <модуль> --next
```

Вопрос модуля, его решения и факты печатаются заново, поштучно, прежний ответ - значение по умолчанию.
`--reopen agents` спрашивает и `agents.cli`, `agents.tools`, `--reopen release` - секрет токена
релиза. Один ответ спрашивает заново его id, например `--reopen apps.<приложение>.check`. Решения других модулей, записанные без владельца, приходят так же, как при установке
(раздел "Настройка" `docs/platform/agents/harness.md` проекта).
Поле `show` каждого вопроса выведи владельцу без правки и без своих слов вокруг, затем задай вопрос через AskUserQuestion, если он есть; ответ передай без правки: `--answer '<id>=<значение>' --next`.
Цикл идет до `{"done": true}`; `show` итога выведи так же без правки.

## 4. Установка

```bash
"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" apply --target . --archive <архив, которым идет apply>
```

Файлы и блоки комплекта `apply` пишет по новым настройкам, даже если проект их правил;
файлы и блоки снятого модуля уходят и с правкой проекта. Правку проекта, которую `apply` переписал,
в файлах оболочки показывает `git diff`, а поставленный файл `install` переписывает без следа в git, поэтому его правку проект переносит до обновления: нужную перенеси в место проекта, которое называет отказ гейта `harness-files`, а не вноси заново в файл комплекта.

Архив, собранный под решения, не несет модулей вне них, и `apply` откажет: нужен полный комплект
той же версии или архив под новые решения - `"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" pack --config
.harness/project/manifest.json --out <каталог>` из полного комплекта. Новый архив распакуй так же, как в шаге 1,
и дальше `KIT` - его каталог: `apply` и приемка ниже идут из него, потому что `verify` принимает только архив, записанный в
`lock.json`.

## 5. Приемка и PR

Проверь изменения и добавь в индекс только пути этой настройки до проверки: гейты реестров читают
отслеживаемые файлы. `KIT` - каталог архива, которым шел `apply`.

```bash
git status --short
git add -- <проверенные пути настройки>
"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" verify --target .
./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --base origin/<база>
```

Коммит явными путями из `git status`, PR черновиком в базовую ветку. В описании - что изменено в
модулях и какие правила включились или выключились; описание начинается с заготовки `./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py form ChangeBody --out <файл>`, а собирает его `./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render change/description --set-file ChangeBody=<файл> --out <файл>`.
