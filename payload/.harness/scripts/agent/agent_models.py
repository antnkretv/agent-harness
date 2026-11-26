from __future__ import annotations
import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class SessionError(RuntimeError):
    pass


class Stopped(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class Server:
    url: str
    token: str


@dataclass
class Session:
    args: argparse.Namespace
    prompt: str
    env: dict[str, str]
    workdir: Path
    servers: dict[str, Server]
    temp: Path
    undo: list[Callable[[], None]] = field(default_factory=list)

    @property
    def review(self) -> bool:
        return self.args.mode == "review"

    @property
    def readonly(self) -> bool:
        return self.args.mode != "agent"

    @property
    def denials(self) -> Path:
        return self.temp / "denials.jsonl"


@dataclass
class Outcome:
    ok: bool = False
    text: str = ""
    structured: object = None
    usage: dict = field(default_factory=dict)
