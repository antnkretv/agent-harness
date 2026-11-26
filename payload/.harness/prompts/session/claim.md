Конвейер разработки: выдели ветку задачи командой
./.harness/hooks/python.cmd .harness/scripts/pipeline/task-worktree.py start --type <тип заголовка PR> --key <слаг по смыслу задачи> --session ${SessionId}
Тип - тип Conventional Commits будущего заголовка PR. Постановке с задачей трекера, в том числе со ссылкой на ее карточку, добавь --issue <ключ задачи>; продолжению PR - --change <номер> без --type и --key. Дальше работай только в каталоге path из вывода команды. Порядок - ${StepDoc}.
