Check release PR ${ChangeLink} упал: ${FailedChecks}.

1. Прочитай журналы упавших заданий командой ./.harness/hooks/python.cmd .harness/lib/forge.py job-log <задание> и найди причину.
2. Баг релиза чинится в ${BaseBranch} (CONTRIBUTING.md §11), правку ведешь ты сам, без конвейера задачи: выдели копию командой ./.harness/hooks/python.cmd .harness/scripts/pipeline/task-worktree.py start --type fix --key <слаг> --base origin/${BaseBranch}, внеси правку, закоммить, запушь и открой PR в ${BaseBranch} командой ./.harness/hooks/python.cmd .harness/lib/forge.py open-change.
3. Из той же копии выполни ${GateCommand} --fix <номер PR правки>. Дальше сессия ведет правку через круг ревью и ждет ее мержа; release PR остается открытым до мержа правки.
