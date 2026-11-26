#!/usr/bin/env python3
"""Запрос GitLab глазами конвейера: номер в коммите мержа и база влитого запроса.

Номер запроса в сообщении коммита - единственный вход закрытия задач после мержа,
а база влитого запроса без целевой ветки роняла уборку рабочих копий. Таблица держит
разбор коммита слияния и squash GitLab и пустую базу влитого запроса без целевой ветки.

    python3 .harness/tests/gitlab-change.py
"""

from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_root = Path(__file__).resolve().parents[2]
if not (_root / ".harness").is_dir():
    _root = _root.parent
LIB = _root / ".harness" / "lib"
if not (LIB / "gitlab_adapter.py").is_file():
    print("gitlab-change: SKIP - адаптер GitLab не установлен")
    sys.exit(0)
sys.path.insert(0, str(LIB))
import forge  # noqa: E402
import gitlab_adapter  # noqa: E402

failed = 0
checked = 0


def report(name: str, want, got) -> None:
    global failed, checked
    checked += 1
    if want == got:
        print(f"  ok    {name}")
        return
    failed += 1
    print(f"  FAIL  {name}\n        ждали: {want}\n        было:  {got}", file=sys.stderr)


print("gitlab-change.py - запрос GitLab в коммите и его база:")
adapter = gitlab_adapter.GitLabForge({"project": "group/project", "url": "https://gitlab.example.com"})
MERGE = "Merge branch 'feature/FLX-1-login' into 'develop'\n\nВход по паролю\n\nCloses FLX-1\n\nSee merge request group/project!12\n"
report("коммит слияния называет запрос строкой See merge request", 12, adapter.change_in_message(MERGE))
report("squash называет запрос в конце заголовка", 13, adapter.change_in_message("feat: вход по паролю (!13)\n"))
report("запрос чужого проекта не берется", None,
       adapter.change_in_message(MERGE.replace("group/project!12", "group/other!12")))
report("squash по шаблону %{title} номера не несет", None, adapter.change_in_message("feat: вход по паролю\n"))


def request(method, path, payload=None, ok=(200,), raw=False):
    if path.startswith("repository/branches/"):
        raise forge.ForgeError("HTTP 404: Branch Not Found", 404)
    return {}


adapter._request = request
item = {"iid": 5, "state": "merged", "target_branch": "stage/1", "source_branch": "feature/FLX-2"}
report("влитый запрос читается с пустой базой и без чтения целевой ветки", "", adapter._as_change(item).base_sha)
try:
    adapter._as_change({**item, "state": "opened"})
    refused = False
except forge.ForgeError:
    refused = True
report("открытый запрос без целевой ветки - отказ", True, refused)

print()
if failed:
    print(f"gitlab-change: провалено {failed} из {checked}", file=sys.stderr)
    sys.exit(1)
print(f"gitlab-change: {checked} проверок пройдено")
