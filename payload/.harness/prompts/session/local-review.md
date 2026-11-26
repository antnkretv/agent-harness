Нужен локальный круг: ${RoundReason}. Запусти его из ${WorkTree} параллельными агентами этой сессии.
Провайдер - ${SessionAgent}. Модель каждого прохода - ${ReviewModel}.

1. Подготовь пакет:
./.harness/hooks/python.cmd .harness/scripts/review/review-local.py --pr ${ChangeNumber} --reason ${LocalReasonKey} --provider ${SessionAgent} --context ${LocalContext} ${PassFlags}
2. По round.json запусти параллельно отдельного агента с новым контекстом на каждый проход. Передай только его промпт и пути пакета, без истории разработки. Агенты читают закрепленное дерево и ничего не правят.
3. Собери и опубликуй вердикты:
./.harness/hooks/python.cmd .harness/scripts/review/review-local.py --pr ${ChangeNumber} --collect --work <каталог круга из вывода подготовки> --publish
