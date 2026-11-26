# Claude Code

<!-- harness:begin core -->
Общие правила находятся в [`AGENTS.md`](AGENTS.md). Этот файл содержит только особенности Claude Code.

@AGENTS.md

- Зависимости сессии Claude Code восстанавливает [SessionStart hook](.claude/hooks/session-start.sh).
- Стадия `simplify` skill [`issue-dev`](.agents/skills/issue-dev/SKILL.md) запускается командой `/code-simplifier`.
- Тексты, которые Claude Code публикует инструментами поставщика кода, проверяет хук [`forge-text-lint.py`](.harness/hooks/forge-text-lint.py).
<!-- harness:end core -->

<!-- project:begin -->
<!-- project:end -->
