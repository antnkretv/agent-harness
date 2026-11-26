Релиз коммита ${SHA} прошел: ${ReleaseJobs}.

1. Прочитай журналы заданий выкладки и отчета командой ./.harness/hooks/python.cmd .harness/lib/forge.py job-log <задание>: выкладка прошла без ошибок, приложение запустилось, тег поставлен.
2. Оставь в release PR ${ChangeLink} итог релиза: версия, что выложено, PR обратного слияния и задачи, которые ждут приемки после выката, - по отчету релиза. Комментарий собери командой ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render comment/note --set NoteType=release --set-file AgentReply=<файл> --commit ${SHA} --out <комментарий> и опубликуй командой ./.harness/hooks/python.cmd .harness/lib/forge.py comment ${ChangeNumber} --body-file <комментарий>. PR обратного слияния вливает человек.
3. Выполни ${GateCommand} --done release-accept: на этом сессия релиза заканчивает наблюдение.
