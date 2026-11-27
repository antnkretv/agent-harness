Релиз коммита ${SHA} в ${ProductionBranch} не прошел: ${FailedChecks}.

1. Прочитай журналы упавших заданий командой ./.harness/hooks/python.cmd .harness/lib/forge.py job-log <задание> и найди причину.
2. Исправление идет хотфиксом в ${ProductionBranch} (.harness/CONTRIBUTING.md §11), правку ведешь ты сам, без конвейера задачи и кругов ревью: выдели копию командой ./.harness/hooks/python.cmd .harness/scripts/pipeline/task-worktree.py start --type fix --key <слаг> --base origin/${ProductionBranch}, внеси правку, закоммить, запушь и открой PR в ${ProductionBranch} командой ./.harness/hooks/python.cmd .harness/lib/forge.py open-change.
3. Из той же копии выполни ${GateCommand} --fix <номер PR хотфикса>: сессия дождется его мержа и прогона релиза, который мерж запускает.

Если по журналам выкладка и тег прошли, а упало только уведомление или отчет, хотфикс не нужен: оставь в release PR ${ChangeLink} итог релиза с этим сбоем: собери комментарий командой ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render comment/note --set NoteType=release --set-file AgentReply=<файл> --commit ${SHA} --out <комментарий> и опубликуй командой ./.harness/hooks/python.cmd .harness/lib/forge.py comment ${ChangeNumber} --body-file <комментарий>. Затем заверши сессию командой ${GateCommand} --done release-accept.
