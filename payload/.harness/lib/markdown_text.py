from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from html.parser import HTMLParser

VENDOR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "vendor"))
with open(os.path.join(VENDOR, "libraries.json"), encoding="utf-8") as handle:
    LIBRARIES = json.load(handle)["libraries"]
for library in LIBRARIES:
    archive = os.path.join(VENDOR, library["filename"])
    with open(archive, "rb") as handle:
        if hashlib.sha256(handle.read()).hexdigest() != library["sha256"]:
            raise ImportError(archive)
    sys.path.insert(0, archive)

LOADED = {}
for library in LIBRARIES:
    module = importlib.import_module(library["module"])
    expected = os.path.normcase(os.path.join(VENDOR, library["filename"])) + os.sep
    if not os.path.normcase(os.path.normpath(module.__file__)).startswith(expected):
        raise ImportError(module.__file__)
    LOADED[library["module"]] = module
from platform_messages import msg
from markdown_it.parser_block import ParserBlock
from markdown_it.parser_inline import ParserInline


def check_nesting(state) -> None:
    if state.level >= state.md.options["maxNesting"]:
        raise RuntimeError(msg("runtime_markdown_nesting_limit_exceeded"))


class BoundedBlockParser(ParserBlock):
    def tokenize(self, state, startLine: int, endLine: int) -> None:
        check_nesting(state)
        super().tokenize(state, startLine, endLine)


class BoundedInlineParser(ParserInline):
    def tokenize(self, state) -> None:
        check_nesting(state)
        super().tokenize(state)

    def skipToken(self, state) -> None:
        check_nesting(state)
        super().skipToken(state)


MARKDOWN = LOADED["markdown_it"].MarkdownIt()
MARKDOWN.block = BoundedBlockParser()
MARKDOWN.inline = BoundedInlineParser()
MARKDOWN.configure("commonmark").enable("table")
BLOCKS = {"p", "div", "section", "article", "details", "summary", "li", "ul", "ol", "tr", "table",
          "thead", "tbody", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr"}
CODE = {"code", "pre", "script", "style"}


class TextWithoutCode(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.blocked = []
        self.quotes = 0
        self.line_start = True

    def emit(self, text: str) -> None:
        if self.blocked:
            return
        for part in text.splitlines(keepends=True):
            if self.line_start and part.strip():
                self.parts.append("> " * self.quotes)
                self.line_start = False
            self.parts.append(part)
            if part.endswith("\n"):
                self.line_start = True

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in CODE:
            self.emit(" ")
            self.blocked.append(tag)
        elif not self.blocked:
            if tag == "blockquote":
                self.emit("\n")
                self.quotes += 1
            elif tag in BLOCKS:
                self.emit("\n")
            elif tag in ("td", "th"):
                self.emit(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in CODE:
            if tag in self.blocked:
                index = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                del self.blocked[index]
            self.emit(" ")
        elif not self.blocked:
            if tag == "blockquote":
                self.emit("\n")
                self.quotes = max(0, self.quotes - 1)
            elif tag in BLOCKS:
                self.emit("\n")
            elif tag in ("td", "th"):
                self.emit(" | ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in CODE:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self.emit(data)

    def handle_comment(self, data: str) -> None:
        self.emit("<!--" + data + "-->")

    def handle_entityref(self, name: str) -> None:
        self.emit("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        self.emit("&#" + name + ";")


def without_code(text: str) -> str:
    parser = TextWithoutCode()
    parser.feed(MARKDOWN.render(text))
    parser.close()
    return "".join(parser.parts)
