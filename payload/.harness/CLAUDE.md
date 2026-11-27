# Claude Code: harness

Особенности Claude Code в конвейере harness. `CLAUDE.md` проекта подключает этот файл строкой
`@.harness/CLAUDE.md` вместе с `@AGENTS.md` и `@.harness/AGENTS.md`.

- Зависимости сессии Claude Code восстанавливает хук `SessionStart` в [`.claude/settings.json`](../.claude/settings.json): `ah.py hook session-start claude`.
- Стадия `simplify` skill [`issue-dev`](../.agents/skills/issue-dev/SKILL.md) запускается командой `/code-simplifier`.
- Тексты, которые Claude Code публикует инструментами поставщика кода, проверяет хук [`forge-text-lint.py`](hooks/forge-text-lint.py).
