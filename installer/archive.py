from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import tarfile
from pathlib import Path

from .catalog import enabled, shipped_bytes, wanted
from .configure import answers_from, configured, known_id
from .detect import Tree
from .documents import json_text
from .questions import Pending, Session
from .render import absent_entries, computed, fragment_folder, registry_pruner, render
from .resources import (
    AGENT,
    CATALOG,
    CONFIG,
    GUIDE_TEMPLATES,
    GUIDES,
    KIT_DOCUMENTS,
    Refusal,
    kit_root,
    load_json,
    msg,
    say,
    sha256,
    runtime_files,
)

RETIRED = re.compile(r"modules\.[^.]+\.defaults")


def config_decisions(source: Path, catalog: dict) -> tuple[dict, dict]:
    data = load_json(source, msg("no_config", source))
    if isinstance(data.get("decisions"), dict):
        data["decisions"] = {key: value for key, value in data["decisions"].items() if not RETIRED.fullmatch(key)}
        for key in data["decisions"]:
            known_id(key, catalog)
        given = data["decisions"]
    elif isinstance(data.get("modules"), dict):
        given = answers_from(data, catalog)
    else:
        raise Refusal(msg("pack_config_shape", source))
    session = Session(catalog, Tree(None), {key: {"value": value, "by": "owner"} for key, value in given.items()},
                      given, defaults=True, decisions=True)
    try:
        modules = configured(session)["modules"]
    except Pending as stop:
        raise Refusal(msg("pack_undecided", stop.question.id, stop.question.text)) from None
    facts = sorted(set(data.get("decisions", {})) - session.seen)
    if facts:
        raise Refusal(msg("pack_not_decision", ", ".join(facts)))
    return dict(sorted(session.decided.items())), modules


def put(path: Path, data: bytes, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)


def archive(folder: Path) -> Path:
    kit = load_json(folder / "kit.json", msg("no_kit", folder / "kit.json"))
    executables = {"ah.py"} | {"payload/" + path for path, meta in kit["files"].items() if meta.get("executable")}
    target = folder.with_name(folder.name + ".tar.gz")
    with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped, \
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted([folder, *folder.rglob("*")], key=lambda p: p.relative_to(folder.parent).as_posix()):
            info = tar.gettarinfo(str(path), arcname=path.relative_to(folder.parent).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if path.is_dir() or path.relative_to(folder).as_posix() in executables else 0o644
            if path.is_file():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
    return target


def agent_guide(template: str, project: dict, kit: dict, archive_name: str, name: str = AGENT) -> str:
    return render(template, project, {**computed(kit, project), "harness.archive": archive_name}, name)


def pack_kit(kit_dir: Path, source: Path, out: Path) -> Path:
    kit = load_json(kit_dir / "kit.json", msg("no_kit", kit_dir))
    templates = kit_dir / GUIDE_TEMPLATES
    if kit.get("packed") or not all((templates / name).is_file() for name in GUIDES):
        raise Refusal(msg("pack_full_kit", kit_dir))
    catalog = load_json(kit_dir / CATALOG, msg("no_catalog", kit_dir / CATALOG))
    decisions, modules = config_decisions(source, catalog)
    project = {"modules": modules}
    paths = enabled(project)
    config = json.dumps({"decisions": decisions}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = sha256(config.encode("utf-8"))
    name = f"{kit['name']}-{kit['version']}-{digest[:8]}"
    folder = out / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    prune = registry_pruner(kit_dir, kit, paths)
    files = {}
    for path, meta in sorted(kit["files"].items()):
        if not wanted(meta, paths):
            continue
        files[path] = dict(meta)
        if not meta.get("compiled"):
            put(folder / "payload" / path, prune(path, shipped_bytes(kit_dir, path, meta)), bool(meta.get("executable")))
    extras = [*KIT_DOCUMENTS, *runtime_files(kit_dir)]
    extras += sorted(path.relative_to(kit_dir).as_posix() for path in (kit_dir / "skills").rglob("*") if path.is_file())
    for module_path in sorted(paths):
        fragments = kit_dir / "fragments" / fragment_folder(module_path)
        extras += sorted(path.relative_to(kit_dir).as_posix() for path in fragments.rglob("*") if path.is_file())
    for path in extras:
        put(folder / path, (kit_dir / path).read_bytes(), path == "ah.py")
    for guide in GUIDES:
        text = agent_guide((templates / guide).read_text(encoding="utf-8"), project, kit, name + ".tar.gz", guide)
        put(folder / guide, text.encode("utf-8"), False)
    put(folder / CONFIG, config.encode("utf-8"), False)
    shipped = {key: value for key, value in kit.items() if key not in ("files", "dependencies")}
    shipped["files"] = files
    shipped["dependencies"] = [item for item in kit.get("dependencies", []) if wanted(item, paths)]
    shipped["absent"] = {path: {key: meta[key] for key in ("module", "also") if key in meta}
                         for path, meta in sorted(absent_entries(kit, paths, catalog).items())}
    shipped["packed"] = {"config": digest, "paths": sorted(paths)}
    put(folder / "kit.json", json_text(shipped).encode("utf-8"), False)
    return archive(folder)


def pack(args: argparse.Namespace) -> int:
    packed = pack_kit(kit_root(), Path(args.config).resolve(), Path(args.out).resolve())
    say("pack_done", packed, sha256(packed.read_bytes()))
    return 0
