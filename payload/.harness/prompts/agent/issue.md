Сработал триггер конвейера issue-dev.
Событие: ${EventKind}. Цель: ${EventTarget} ${EventRef}, номер для команд - ${EventNumber}.

Действуй по .agents/skills/issue-dev/SKILL.md, разделам `review`, `ready`, `pr-event`,
`review-comments` и `issue-event`. Получи актуальную структуру командой
`./.harness/hooks/python.cmd .harness/scripts/pipeline/pipeline-run.py plan change`.

Виды события:
- `note` - комментарий к запросу слияния (цель `change`) или к задаче
  (цель `issue`);
- `review` - ревью или реплика ревью на запросе слияния;
- `round` - закончился круг ревью на запросе слияния, который ведет
  конвейер.

Рабочая копия - клон репозитория. На событии открытого запроса слияния
она стоит на его ветке, на событии задачи - на выделенной ветке задачи. Веткой и worktree управляет обвязка до запуска сессии; другую ветку не создавай.
Комментарий, а также событие закрытого запроса или запроса из другого репозитория получают только ответ без правок.

Поставщик кода - `./.harness/hooks/python.cmd .harness/lib/forge.py`, текст уходит
файлом через `--body-file`:
- `change <номер>` - запрос слияния JSON: состояние, черновик, метки,
  ветки, голова;
- `notes <номер>` - комментарии запроса JSON;
- `reviews <номер>` - ревью запроса JSON: состояние, коммит, автор,
  тело вердикта;
- `comment <номер> --body-file <файл>` - комментарий;
- `label <номер> --add <метка>` и `label <номер> --remove <метка>`;
- `request-review <номер>` - метка круга ревью;
- `ready <номер>` - перевод из Draft в Ready;
- `checks <SHA>` - проверки коммита строками
  `состояние<TAB>имя<TAB>адрес`;
- `job-log <адрес>` - хвост журнала задания CI.

Задача трекера - `./.harness/hooks/python.cmd .harness/lib/tracker.py`: `issue <ключ>`,
`notes <ключ>`, `comment <ключ> --body-file <файл>`, `create --title <заголовок> --body-file <файл> --label <тип>`;
заголовок и метки задачи - по `CONTRIBUTING.md` §12.

Файл комментария собирает `./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render comment/note --set NoteType=<тип> --set-file AgentReply=<файл> --commit <SHA> --out <файл>`:
тип - ключ из `text_templates.py show comment/note`, SHA - коммит, о котором комментарий;
файл задачи - заготовка `./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py form IssueText --out <файл>` с заполненными слотами и сборка `./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render issue/task --set-file IssueText=<файл> --out <файл>`; собранный файл не правь:
текст без маркера данных проверка текста отклонит.

Треды ревью: `./.harness/hooks/python.cmd .harness/scripts/review/pr-threads.py list <номер>`
показывает незакрытые треды с их id,
`./.harness/hooks/python.cmd .harness/scripts/review/pr-threads.py reply <id> --body-file <файл> --resolve`
отвечает и закрывает. Файл ответа собирает
`./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render comment/thread-reply --set-file ThreadReply=<файл> --commit <SHA> --out <файл>`;
собранный файл не правь. Номер запроса за веткой записывает конвейер, когда узнает запрос.

Порядок:
- перечитай задачу и все комментарии;
- на событии `review` разбирай замечания ревьюера и правь код в ветке
  запроса; при падении проверок читай `checks` и `job-log` каждого упавшего задания отдельно.
  Падение в задании, чей вход дифф не трогает, проверь на базе только командой упавшего шага в
  отдельном detached worktree: на коммитах базы `check` не запускается. Повторилось на базе - попутная
  находка по `CONTRIBUTING.md` §10, а не регрессия запроса. Причину называй по строке журнала и
  коду, который ее дает; повторный прогон без правки причину не доказывает;
- разобранное замечание закрывай: ответ в треде плюс resolve.
  Замечание не принято - ответь почему и оставь тред открытым,
  решает человек;
- вопрос человека без ответа не оставляй;
- на событии `note` ты только отвечаешь. Код не правишь, ветку не
  заводишь, запрос не открываешь - ни на свободной задаче, ни на взятой;
- на событии `round` ты ведешь запрос ${EventRef} до Ready. Прочитай
  вердикты круга: `reviews`, `checks` головы и треды. Есть запрос правок -
  разбери его по `review-comments`, выполни план,
  запушь одним пушем и запроси круг (`request-review`). Условия `processes.merge.steps[ready].readiness` выполнены - переведи запрос в Ready (`ready`);
  идущие проверки дождись, повторяя `checks <SHA головы>` с паузой
  `./.harness/hooks/python.cmd -c "import time; time.sleep(60)"`. Круг без вердикта -
  локальный резерв по skill review. Бюджет состоявшихся кругов исчерпан - напиши
  в запросе, что нужен человек, и остановись;
- код правишь только на событиях `review` и `round`;
- запрос не мержи и метку `processes.merge.steps[merge].handoff.label` не ставь. В Ready переводи
  только на событии `round`. Задачи не закрывай, чужой assignee не
  снимай.
