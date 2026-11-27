"""Загрузчик agent-harness: ставит в кеш архив версии из .harness/state/lock.json и передает команду утилите ah.

    python3 .harness/ah.py install [--archive <архив>]   # Windows: py -3 .harness/ah.py install
    python3 .harness/ah.py upgrade [--version <версия> | --commit <ветка или коммит>]  # ветка, lock и PR
    python3 .harness/ah.py change                         # смена настроек: вопросы с прежними ответами, затем apply
    python3 .harness/ah.py doctor                         # зависимости комплекта против машины

Кеш хранит версию под ее kit.tree. Версии нет в кеше - загрузчик собирает комплект из kit.commit репозитория
kit.repository, а без kit.commit скачивает архив выпуска; файлы любого источника сверяются с kit.tree.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("AGENT_HARNESS_CACHE") or Path.home() / ".cache" / "agent-harness")
MSG_unpinned='ah: в .harness/state/lock.json нет kit.tree: комплект ставится командой apply --archive'
MSG_shape='ah: архив %s - не комплект agent-harness: в нем нет одного каталога с ah.py'
MSG_no_archive='ah: после --archive нужен путь к архиву'
MSG_built='ah: файлы архива %s не сходятся с kit.tree %s из lock.json'
MSG_unbuilt='ah: kit.py build в исходнике %s не собрал архив'
MSG_uncached='ah: версии %s нет в кеше %s или ее файлы там изменены: передайте архив флагом --archive'
MSG_download='ah: архив выпуска %s не скачан: %s. Передайте архив флагом --archive'
MSG_fetch='ah: коммит %s комплекта не получен из %s: %s. Передайте архив флагом --archive'


def msg(key: str, *args: object) -> str:
    return globals()["MSG_" + key] % args


def tree_digest(files) -> str:
    return hashlib.sha256("".join(f"{name}\0{hashlib.sha256(data).hexdigest()}\n"
                                  for name, data in sorted(files)).encode("utf-8")).hexdigest()


def genuine(top: Path, tree: str) -> bool:
    return (top / "ah.py").is_file() and tree_digest(
        (path.relative_to(top).as_posix(), path.read_bytes()) for path in top.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts) == tree


def unpacked(archive: Path, home: Path, tree: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".unpack-", dir=CACHE))
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(work, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        tops = [path for path in work.iterdir() if (path / "ah.py").is_file()]
        if len(tops) != 1:
            sys.exit(msg("shape", archive))
        if not genuine(tops[0], tree):
            sys.exit(msg("built", archive, tree))
        try:
            os.replace(tops[0], home)
        except OSError:
            if not genuine(home, tree):
                try:
                    os.replace(home, work / "stale")
                except FileNotFoundError:
                    pass
                try:
                    os.replace(tops[0], home)
                except OSError:
                    if not genuine(home, tree):
                        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return home


def isolated() -> dict:
    local = set(subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True).stdout.split())
    local -= {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}
    return {key: value for key, value in os.environ.items() if key not in local}


def built(source: Path, commit: bool = False) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=".build-", dir=CACHE))
    if subprocess.run([sys.executable, str(source / "tools" / "kit.py"), "build", "--out", str(out), *(["--commit"] if commit else [])],
                      stdout=subprocess.DEVNULL, env=isolated()).returncode:
        shutil.rmtree(out, ignore_errors=True)
        sys.exit(msg("unbuilt", source))
    (found,) = out.glob("*.tar.gz")
    return found


def fetched(repository: str, ref: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    work, env = Path(tempfile.mkdtemp(prefix=".fetch-", dir=CACHE)), isolated()
    for command in (["init", "-q"], ["fetch", "-q", "--depth", "1", repository, ref], ["checkout", "-q", "--detach", "FETCH_HEAD"]):
        try:
            done = subprocess.run(["git", "-C", str(work), *command], capture_output=True, text=True, timeout=300, env=env)
        except subprocess.TimeoutExpired as error:
            shutil.rmtree(work, ignore_errors=True)
            raise OSError(str(error)) from None
        if done.returncode:
            shutil.rmtree(work, ignore_errors=True)
            raise OSError(done.stderr.strip())
    return work


def from_source(source: Path, tree: str) -> Path:
    archive = built(source, True)
    try:
        return unpacked(archive, CACHE / tree, tree)
    finally:
        shutil.rmtree(archive.parent, ignore_errors=True)


def release(kit: dict) -> str:
    return f"{kit['repository']}/releases/download/v{kit['version']}/{kit['name']}-{kit['version']}.tar.gz"


def downloaded(kit: dict) -> Path:
    import urllib.request

    url = release(kit)
    CACHE.mkdir(parents=True, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix=".download-", suffix=".tar.gz", dir=CACHE)
    try:
        with os.fdopen(handle, "wb") as out, urllib.request.urlopen(url, timeout=120) as response:
            shutil.copyfileobj(response, out)
    except OSError:
        Path(path).unlink(missing_ok=True)
        raise
    return Path(path)


def place(kit: dict, archive: "Path | None", home: Path, tree: str) -> None:
    if archive is not None:
        unpacked(archive, home, tree)
    elif kit.get("commit") and kit.get("repository"):
        try:
            source = fetched(kit["repository"], kit["commit"])
        except OSError as error:
            sys.exit(msg("fetch", kit["commit"][:12], kit["repository"], error))
        try:
            from_source(source, tree)
        finally:
            shutil.rmtree(source, ignore_errors=True)
    elif kit.get("repository"):
        try:
            downloaded_archive = downloaded(kit)
        except OSError as error:
            sys.exit(msg("download", release(kit), error))
        try:
            unpacked(downloaded_archive, home, tree)
        finally:
            downloaded_archive.unlink(missing_ok=True)
    else:
        sys.exit(msg("uncached", tree[:12], CACHE))


def main(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    kit = json.loads((ROOT / ".harness/state/lock.json").read_text(encoding="utf-8"))["kit"]
    tree = kit.get("tree")
    if not tree:
        sys.exit(msg("unpinned"))
    archive = None
    if "--archive" in args:
        at = args.index("--archive")
        if at + 1 == len(args):
            sys.exit(msg("no_archive"))
        archive = Path(args[at + 1])
        del args[at:at + 2]
    home = CACHE / tree
    if not genuine(home, tree):
        place(kit, archive, home, tree)
    link = ROOT / ".harness" / "kit"
    if os.path.realpath(link) != os.path.realpath(home) and os.name == "nt":
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif os.path.lexists(link):
            os.rmdir(link)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(home)], check=True, capture_output=True)
    elif os.path.realpath(link) != os.path.realpath(home):
        if os.path.lexists(link) and not link.is_symlink():
            os.rmdir(link) if link.is_dir() else link.unlink()
        fresh = link.with_name(f".kit.{os.getpid()}.harness-new")
        fresh.unlink(missing_ok=True)
        fresh.symlink_to(home, target_is_directory=True)
        os.replace(fresh, link)
    return subprocess.run([sys.executable, "-B", str(link / "ah.py"), *args, "--target", str(ROOT)],
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
