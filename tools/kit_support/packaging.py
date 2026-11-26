from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from installer.archive import agent_guide, archive, pack_kit
from installer.documents import json_text
from installer.resources import GUIDE_TEMPLATES, GUIDES, Refusal, sha256

from .catalog_checks import every_module
from .composition import composition
from .context import KIT, SHIPPED, catalog, manifest, msg
from .source import tree_files


def shipped_manifest(kit: dict, folder: Path) -> dict:
    """Состав собранного комплекта: digest - по файлам, какими они легли в архив."""
    tree = {path.relative_to(folder).as_posix(): sha256(path.read_bytes())
            for path in sorted(folder.rglob("*")) if path.is_file() and path.name != "kit.json"}
    shipped = {key: value for key, value in kit.items() if key not in ("_", "files", "leaks", "provenance")}
    shipped["files"] = {
        path: {key: value for key, value in meta.items() if key not in ("origin", "listed")}
        for path, meta in sorted(kit["files"].items())}
    shipped["digest"] = sha256(json_text({"manifest": shipped, "tree": tree}).encode("utf-8"))
    return shipped


def stage(kit: dict, out: Path) -> Path:
    """Каталог полного комплекта. Инструкции установки в нем - под все модули, а их шаблоны лежат рядом: из
    них pack рендерит инструкции архива под решения."""
    name = f"{kit['name']}-{kit['version']}"
    folder = out / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for item in SHIPPED:
        ship(item, folder)
    for guide_path in GUIDES:
        template, target = folder / GUIDE_TEMPLATES / guide_path, folder / guide_path
        template.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KIT / guide_path, template)
        guide = agent_guide(template.read_text(encoding="utf-8"), every_module(catalog()), kit, name + ".tar.gz", guide_path)
        target.write_text(guide, encoding="utf-8", newline="\n")
    for path in tree_files():
        ship(path, folder)
    (folder / "kit.json").write_text(json_text(shipped_manifest(kit, folder)), encoding="utf-8", newline="\n")
    return folder


def build(args: argparse.Namespace) -> int:
    kit = manifest()
    problems = composition(kit)
    if problems:
        print("\n".join(problems))
        return 1
    out = Path(args.out).resolve()
    if args.config:
        # Архив под решения собирается тем же pack, что и у владельца: из каталога полного комплекта.
        with tempfile.TemporaryDirectory() as folder:
            try:
                packed = pack_kit(stage(kit, Path(folder)), Path(args.config).resolve(), out)
            except Refusal as refusal:
                print(refusal)
                return 2
        print(msg("kit_packed", packed, sha256(packed.read_bytes())))
        return 0
    folder = stage(kit, out)
    packed = archive(folder)
    print(msg("kit_built", folder, packed, sha256(packed.read_bytes())))
    return 0


def ship(path: str, folder: Path) -> None:
    target = folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(KIT / path, target)


