from __future__ import annotations

from .context import catalog

SMOKE_ANSWERS = ("modules.agents=claude-code,codex,cursor", "modules.ci-agent=claude-code", "modules.review=on",
                 "modules.issue-agent=on", "modules.release=tag-flow", "modules.notifications=forge,slack",
                 "modules.alert-triage=on", "modules.alert-triage.settings.grafana=https://telemetry.example.com",
                 "modules.alert-triage.settings.pathsFile=deploy/edge.py")
SKILL_PACKS = ("grafana", "drawio")
DOTNET_PACKS = ("aspire", "dotnet-docs")
SMOKE_APP_PATHS = {"node": "apps/web", "dotnet": "src"}
GITLAB_CI = ".gitlab/ci/harness.yml"
SMOKE_STAND_URL = "https://mcp.example.com/mcp"
GITLAB_ANSWERS = ("modules.ci-agent=cursor", "modules.review=on", "modules.issue-agent=on", "modules.release=tag-flow",
                  "modules.runners=gitlab-runner", "modules.runners.extensions.gitlab-runner.review=docker",
                  "modules.stands=mcp-stand",
                  'modules.stands.extensions.mcp-stand.list=[{"name": "production", "url": "%s", '
                  '"token": "STAND_PRODUCTION_TOKEN"}]' % SMOKE_STAND_URL, "modules.mcp=context7")
SMOKE_MCP_URL = catalog()["modules"]["mcp"]["extensions"]["context7"]["server"]["url"]
LSP_CONFIG = ".lsp-mcp.json"
SIDE_LSP = {"backend": ["csharp"], "frontend": ["typescript"]}
GITLAB_PIPELINES = ("harness", "events", "review", "agent", "tracker", "release")
NODE_FILES = (".config/eslint-boundaries.js", ".harness/scripts/check/check-node-adapter.py")
DOTNET_FILES = (".harness/dotnet/Harness.Build.targets", ".harness/scripts/check/check-build-diagnostics.py")
BACKEND_DECISIONS = {"shape": "backend", "modules.backend": ["dotnet"], "modules.agents": ["claude-code"],
                     "modules.forge": "github", "modules.tracker": "github-issues",
                     "modules.skills": ["dotnet-docs"]}
IDENTITY = {"GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
            "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@example.invalid"}
EDITED = ".harness/docs/delegated-task.md"
HOOKED = ".harness/hooks/agent-gate.py"
HOOK_CALL = {"tool_name": "Read", "tool_input": {"file_path": "README.md", "path": "README.md"}}


