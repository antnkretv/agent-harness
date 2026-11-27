# Этап 3: код под гейты и тесты

Порядок этапов, обозначения, ошибки, критерии готовности и шаблон отчета - [AGENT.md](../../AGENT.md).

Копия:

```bash
./.harness/hooks/python.cmd .harness/scripts/pipeline/task-worktree.py start --type chore --key harness-code --base origin/chore/harness-context
cd <path из JSON результата>
```

## Подключение к коду

На этом этапе идут все гейты `check-all.py` по всему репозиторию: существующий код проекта обязан их
пройти. `verify` с гейтами проверяет и отрицательные пробы: каждый гейт обязан отказать на заведомом
нарушении.

Часть подключения `apply` делает сам по фактам манифеста, остальное называет под строкой о правке
агента его план без записи:

```bash
"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" apply --target . --dry-run
```

Подробности - `docs/platform/architecture/gates.md` в проекте.
{{#if stack:dotnet}}
- **dotnet.** `apply` вписывает импорт журнала диагностик `.harness/dotnet/Harness.Build.targets` в
  ближайший к решению `Directory.Build.targets`; каталог, заведенный после `apply`, получает импорт
  повторным `apply`. Анализаторы, арх-тесты и политику сборки проект держит сам: агент называет их
  точками входа `analyze` и `architecture` приложения, а файлы политики - в `configs`. Остальное -
  раздел "Подключение к коду .NET".
{{/if}}{{#if stack:node}}
- **node.** Адаптер требует исходники TypeScript и npm; для чистого JavaScript или другого
  менеджера пакетов нужен отдельный адаптер либо миграция. Корневой `package.json` с `workspaces`, единственный lock-файл корня, `.node-version`
  и `engines.node` по его мажору; в `devDependencies` корня - `eslint`, `typescript-eslint`,
  `@typescript-eslint/parser`. `eslint.config` каждого приложения подключает `layerBoundaries` из `.config/` и объявляет слои
  приложения; правила, которые нельзя ослабить, проект экспортирует как `required` из модулей настройки
  `<сторона>/node.rules`, и конфиг каждого приложения стороны их импортирует.
  `apply` такой конфиг не переписывает, а называет приложение, чей конфиг адаптер не берет: его
  подключает агент. Проверка - гейт `node-adapter`.
{{/if}}
- **Стек без адаптера.** Гейт границ - точка входа `entries.architecture` приложения. Гейта нет -
  `architectureWaiver` с причиной и задача на замену.
{{#if stack:dotnet}}
## Подключение к коду .NET

1. Сборка решения с анализаторами проекта - точка входа `analyze` приложения .NET, архитектурные
   тесты - точка входа `architecture`, `Directory.Build.*` политики и конфиги анализаторов - `configs`:
   правка `apps.<id>.entries` и `apps.<id>.configs` в манифесте и `python3 .harness/ah.py change`. Нет своих анализаторов или арх-тестов - роль остается
   пустой, и прогон показывает ее строкой "не настроено".
2. `NoWarn` и список рукописных входов компилятора живут только в файлах `configs`: в другом csproj,
   `.props` или `.targets` их отклоняет гейт `diagnostic-integrity`.
3. C# переформатируется CSharpier отдельным коммитом: `dotnet tool restore` и
   `dotnet tool run csharpier format .`. Манифест инструмента, `.csharpierignore` и настройки `.vscode`
   ставит `apply`; файл, который после этого не проходит `density` гейта `comment-layer`, сокращает
   поясняющие комментарии: документирующие в долю не входят.
4. Голова с workload (MAUI, `net*-android`, `net*-ios`) собирается только там, где стоят workload
   (`dotnet workload install`) и SDK платформы; образ раннера CI их получает от владельца. Конфигурацию,
   которую эта среда собирает, задает решение `configuration` расширения `dotnet` (на Linux у Android
   нет Release с AOT). Голову, которую среда не собирает, и непроверенный платформенный код называют в отчете.
{{/if}}
| Гейт | Что чинить |
| --- | --- |
| `comment-layer` | комментарий-пересказ, история, объем - по `docs/platform/standards/code-comments.md` |
| `text-registry` | текст для человека в коде скрипта - ключ в `messages.env` своего корня; реестры `.harness/` - файлы комплекта, workflow проекта держит ключ `MSG_<ключ>: '<текст>'` в `env:` своего файла; текст интерфейса в коде - ключ словаря `locales/` приложения |
| `adr-reference` | голый номер ADR в коде - ссылка на файл решения |
| `tool-pins` | версия инструмента числом в документе - машинный источник версии и ссылка на него{{#if stack:dotnet}}; версия пакета в `Directory.Build.props` или `.targets` - запись в `Directory.Packages.props` решения: Central Package Management{{/if}} |
| `workflow-shell`, `shell-scripts` | скрипт CI или shell, который роняет поиск без совпадения или не проходит ShellCheck |
{{#if module:secrets/gitleaks}}| `secret-scan` | секрет в дереве: удалить, отозвать |
{{/if}}{{#if stack:dotnet}}| `build-diagnostics` | диагностика или подавление в коде .NET - исправить код; подавление - только подавителем с контрактом. Сообщения info консоль `dotnet build` не печатает: их список дает сам гейт после сборки |
| `dotnet-format` | файл C# расходится с CSharpier - `dotnet tool run csharpier format .` и коммит результата |
| роли `analyze` и `architecture` | правила анализаторов и арх-тестов проекта - по их описанию в проекте |
{{/if}}{{#if stack:node}}| `node-adapter` | `eslint.config` приложения не берет адаптер или правило не на `error` - подключить по разделу "Подключение к коду" |
{{/if}}{{#if module:prose/vale}}| глоссарий в словаре интерфейса | значение словаря - имя типа, а не общий термин: исключение `glossaryValueExceptions` модуля `prose` - решение владельца: владелец отвечает в `python3 .harness/ah.py change` |
{{/if}}| `check` приложения | сборка и тесты приложения из манифеста |

Рецепт правила - его описание, которое `build-diagnostics` печатает под нарушением: что правило отклоняет и
какие настройки его меняют. Нарушение,
которое правкой кода по рецепту не снять - правка меняет поведение, требует выбора по смыслу или
факт настройки расходится с кодом, - решение владельца: этап останавливается по случаю 2 раздела "Когда этап останавливается" `AGENT.md`, отчет называет
диагностику, число мест и варианты, ветка этапа не сливается. Неверный факт исправляют
правкой манифеста и `python3 .harness/ah.py change`.

Первый полный прогон называет нарушения. Пока чинишь их, гоняй проверку той области, которую правишь:
команду приложения из `AGENTS.md` или гейты дерева без сборки (`check-all.py --tree`). Повторный полный
прогон с `verify` идет один раз, после апрува всех проходов ревью, перед Ready:

```bash
./.harness/hooks/python.cmd .harness/scripts/check/check-all.py --base origin/chore/harness-context
"$KIT/payload/.harness/hooks/python.cmd" "$KIT/ah.py" verify --target .
```

PR этапа 3 открывается в `chore/harness-context`; Check на его голове должен быть зеленым.
{{#if module:review}}Ревью на голове PR запрашивает `./.harness/hooks/python.cmd .harness/lib/forge.py request-review <номер PR>`, дальше скилл
`.agents/skills/issue-dev/SKILL.md`, разделы `review` и `ready`. Условия Ready, исполнителя и способ
мержа печатает `./.harness/hooks/python.cmd .harness/scripts/pipeline/pipeline-run.py plan change`.
{{/if}}Отключенный или неприменимый модуль не заменяет обязательное подтверждение процесса `change`.
Конфигурация без полного процесса не обещает автоматический Ready и не разрешает обход его гейтов.
