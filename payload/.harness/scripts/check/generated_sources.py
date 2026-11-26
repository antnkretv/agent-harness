import fnmatch
from pathlib import Path
import re

from ci_common import GENERATED_PREFIXES, GENERATED_SEGMENTS, PAYLOAD_PREFIXES, ROOT, generated_path, get_list

VENDORED = (".specify/",) + tuple(f".agents/skills/{name}/" for name in get_list("docs.vendoredSkills", []))


def installed(path: str) -> str:
    return next((path[len(prefix):] for prefix in PAYLOAD_PREFIXES if path.startswith(prefix)), path)


def authored(path: str, owned: set[str], patterns: list[str] = ()) -> bool:
    return not (path in owned or generated_path(path)
                or installed(path).startswith(VENDORED)
                or any(fnmatch.fnmatch(path, pattern) for pattern in patterns))


def generated(path: str) -> bool:
    if path.startswith(GENERATED_PREFIXES) or any(
        segment in f"/{path}" for segment in GENERATED_SEGMENTS if segment.strip("/") != "Migrations"
    ):
        return True
    if "/Migrations/" not in f"/{path}" or not path.endswith(".cs"):
        return False
    file = Path(ROOT, path)
    if file.name.endswith("ModelSnapshot.cs"):
        source = file.read_text(encoding="utf-8")
        return bool(re.search(r"class\s+\w+\s*:\s*ModelSnapshot\b", source) and "[DbContext(typeof(" in source)
    if not re.fullmatch(r"\d{14}_.+(?:\.Designer)?\.cs", file.name):
        return False
    stem = file.name.removesuffix(".Designer.cs").removesuffix(".cs")
    designer = file.with_name(stem + ".Designer.cs")
    body = file.with_name(stem + ".cs")
    if not designer.is_file() or not body.is_file():
        return False
    metadata = designer.read_text(encoding="utf-8")
    return (f'[Migration("{stem}")]' in metadata and "[DbContext(typeof(" in metadata
            and bool(re.search(r"class\s+\w+\s*:\s*Migration\b", body.read_text(encoding="utf-8"))))
