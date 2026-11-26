#!/usr/bin/env python3
"""Версия релиза из git-тегов и текст о релизе."""

from __future__ import annotations

import argparse
import os
import re
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import text_templates  # noqa: E402
from areas import deploy_areas, load_map, resolve  # noqa: E402
from ci_common import (BACK_PREFIX, BASE_BRANCH, CUT_PREFIX, annotation, forge, msg, opened_from,  # noqa: E402
                       opened_into, release_branches, release_flow, run_strict, secrets, setting, tracker)

PREFIX = "v"
FEAT = re.compile(r"^feat(\([^)]*\))?!?:\s")
LISTED_ISSUES = 20
DEPLOY_JOB, TAG_JOB = "harness-release-deploy", "harness-release-tag"
EXTENSION = "release/tag-flow"


def git(*args: str) -> str:
    return run_strict("git", *args, unread=msg("release_git_failed", " ".join(args))).strip()


def fail(key: str, *args: object) -> None:
    print(annotation("error", key, *args), file=sys.stderr)
    sys.exit(1)


def versions(prefix: str, *extra: str) -> list[tuple[tuple[int, int, int], str]]:
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)\.(\d+)\.(\d+)$")
    found = []
    for tag in git("tag", "--list", f"{prefix}*", *extra).splitlines():
        match = pattern.match(tag.strip())
        if match:
            found.append(((int(match[1]), int(match[2]), int(match[3])), tag.strip()))
    return sorted(found)


def plan(ref: str, major: bool, version: str = "", *,
         prefix: str = PREFIX, initial: str = "") -> dict[str, str]:
    known = versions(prefix)
    if version:
        tag = prefix + version.removeprefix("v")
        if tag not in {name for _, name in known}:
            fail("release_no_such_tag", tag)
        return {"mode": "rollback", "tag": tag, "previous": "", "sha": git("rev-parse", f"{tag}^{{commit}}")}

    sha = git("rev-parse", f"{ref}^{{commit}}")
    own = versions(prefix, "--points-at", ref)
    if own and not major:
        return {"mode": "released", "tag": own[-1][1], "previous": "", "sha": sha}
    if not known:
        if initial:
            return {"mode": "release", "tag": prefix + initial, "previous": "", "sha": sha}
        fail("release_no_tag", prefix, prefix)

    (base_major, base_minor, base_patch), previous = known[-1]
    changes = git("log", "--no-merges", "--format=%s", f"{previous}..{ref}").splitlines()
    if not changes and not major:
        return {"mode": "released", "tag": previous, "previous": "", "sha": sha}
    if major:
        number = (base_major + 1, 0, 0)
    elif any(FEAT.match(subject) for subject in changes):
        number = (base_major, base_minor + 1, 0)
    else:
        number = (base_major, base_minor, base_patch + 1)
    return {"mode": "release", "tag": prefix + ".".join(map(str, number)), "previous": previous, "sha": sha}


def linked_issues(prs: set[int], *, closing: bool) -> set[str]:
    if not prs or not tracker.name:
        return set()
    try:
        return tracker.closing_issues(prs) if closing else tracker.linked_issues(prs, closing=False)
    except (forge.ForgeError, tracker.TrackerError) as error:
        print(annotation("warning", "release_issues_unread", error), file=sys.stderr)
        return set()


def merged_changes(previous: str, ref: str) -> dict[int, bool]:
    found: dict[int, bool] = {}
    for record in git("log", "--format=%P%x1f%B%x00", f"{previous}..{ref}").split("\0"):
        parents, _, message = record.lstrip("\n").partition("\x1f")
        number = forge.change_in_message(message)
        if number is not None:
            found[number] = found.get(number, False) or len(parents.split()) > 1
    return found


def flow(number: int) -> bool:
    change = forge.change(number)
    return change is not None and release_flow(change.head)


def changes(previous: str, ref: str) -> set[int]:
    found = merged_changes(previous, ref)
    try:
        return {number for number, merge in found.items() if not (merge and flow(number))}
    except forge.ForgeError as error:
        print(annotation("warning", "release_changes_unread", error), file=sys.stderr)
        return set(found)


def contents(previous: str, ref: str) -> tuple[list[int], list[str], list[str]]:
    prs = changes(previous, ref)
    closing = linked_issues(prs, closing=True)
    pending = linked_issues(prs, closing=False) - closing
    return sorted(prs), sorted(closing, key=natural), sorted(pending, key=natural)


def natural(key: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", key)]


def changed_areas(previous: str, ref: str) -> list[str]:
    carte = load_map()
    raised, _ = resolve(git("diff", "--name-only", "-z", previous, ref).split("\0"), carte)
    return [name for name in deploy_areas(carte) if name in raised]


def issue_links(issues: list[int]) -> str:
    shown = [f"[{tracker.issue_ref(i)}]({tracker.issue_url(i)})" for i in issues[:LISTED_ISSUES]]
    text = ", ".join(shown) if shown else msg("value_absent")
    if len(issues) > LISTED_ISSUES:
        text += " " + msg("release_more", len(issues) - LISTED_ISSUES)
    return text


def title(rollback: bool, success: bool, tag: str) -> str:
    if rollback and success:
        return msg("release_notify_title_rollback", tag)
    if rollback:
        return msg("release_notify_title_rollback_failure", tag)
    if success:
        return msg("release_notify_title_success", tag)
    return msg("release_notify_title_failure", tag)


def output_plan(result: dict[str, str], *, prefix: str = PREFIX, artifact_suffix: str = "") -> None:
    result["version"] = result["tag"].removeprefix(prefix)
    result["artifact"] = result["sha"] + artifact_suffix
    if result["mode"] == "released":
        print(annotation("notice", "release_already", result["tag"]), file=sys.stderr)
    for key, value in result.items():
        name = f"RELEASE_{key.upper()}"
        print(f"{name}={value}")
        if forge.in_ci():
            forge.set_output(name, value)


def command_next(args: argparse.Namespace) -> None:
    output_plan(plan(args.ref, args.major, args.version))


def results(args: argparse.Namespace) -> tuple[str, str]:
    if args.deploy is not None or args.tagged is not None:
        return args.deploy or "", args.tagged or ""
    try:
        jobs = forge.run_jobs()
    except forge.ForgeError as error:
        print(annotation("warning", "release_jobs_unread", error), file=sys.stderr)
        return "", ""
    return jobs.get(DEPLOY_JOB, ""), jobs.get(TAG_JOB, "")


def failed_stage(rollback: bool, deploy: str, tagged: str, build: str | None = None) -> str:
    if tagged == "failure":
        return msg("release_stage_tag")
    if deploy == "failure":
        return msg("release_stage_deploy")
    if build == "failure":
        return msg("release_stage_build")
    if not deploy and not tagged:
        return msg("release_stage_unknown")
    return "" if (deploy if rollback else tagged) == "success" else msg("release_stage_cancelled")


def link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def report_text(args: argparse.Namespace, rollback: bool, failed: str, *, back_merge: bool = True) -> str:
    values = {"CompareLink": "", "ChangeCount": "", "Areas": "", "Issues": "", "PendingIssues": "",
              "FailedStage": failed, "ReleaseBranch": "", "BaseBranch": "", "CommitCount": "", "BackMergeChange": "",
              "RunLink": link(msg("release_notify_run"), forge.run_url()) if forge.run_url() else ""}
    if args.previous:
        prs, issues, pending = contents(args.previous, args.ref)
        values.update(CompareLink=link(f"{args.previous}...{args.tag}", forge.compare_url(args.previous, args.ref)),
                      ChangeCount=len(prs), Issues=issue_links(issues), PendingIssues=issue_links(pending) if pending else "",
                      Areas=", ".join(changed_areas(args.previous, args.ref)) or msg("value_absent"))
    if not failed and not rollback and back_merge:
        behind = outside(args.ref)
        if behind != "0":
            release, base = release_branches()
            opened = back_merge_change(args.tag)
            values.update(ReleaseBranch=release, BaseBranch=base, CommitCount=behind,
                          BackMergeChange=link(forge.change_ref(opened.number), opened.url) if opened
                          else msg("release_notify_back_merge_missing"))
    return text_templates.render("notify/release", values)


def command_report(args: argparse.Namespace, *, preamble: tuple[str, ...] = (), back_merge: bool = True) -> None:
    rollback = args.mode == "rollback"
    failed = failed_stage(rollback, *results(args), build=args.build)
    head = title(rollback, not failed, args.tag)
    body = "\n".join([*preamble, report_text(args, rollback, failed, back_merge=back_merge)])
    if args.body_file:
        with open(args.body_file, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
    forge.step_summary(f"### {head}\n\n{body}\n")
    if forge.in_ci():
        forge.set_output("title", head)
        forge.set_output("status", ("rollback" if rollback else "release") + ("-failure" if failed else ""))
        forge.set_output("failed", failed)
    print(head)
    print(body)


def command_no_deploy(args: argparse.Namespace) -> None:
    fail("release_deploy_unconnected", args.version)


def outside(ref: str) -> str:
    return git("rev-list", "--count", f"{BASE_BRANCH}..{ref}")


def back_merge_change(tag: str) -> forge.Change | None:
    try:
        return opened_from(BACK_PREFIX + tag)
    except forge.ForgeError as error:
        print(annotation("warning", "release_back_merge_unread", error), file=sys.stderr)
        return None


def release_token() -> None:
    token = setting(EXTENSION, "token", "")
    if not token:
        fail("release_token_missing", ", ".join(secrets(EXTENSION)))
    for name in forge.token_names:
        os.environ[name] = token


def publish(branch: str, sha: str, base: str, title: str, body: str, *, draft: bool, own: bool,
            single: str = "") -> forge.Change:
    if own:
        release_token()
    try:
        for change in opened_into(base, single) if single else ():
            if (change.head, change.head_sha) != (branch, sha):
                fail("release_cut_open", forge.change_ref(change.number), change.head)
        opened = opened_from(branch)
        if opened is not None:
            return opened
        try:
            current = forge.branch_head(branch)
        except forge.ForgeError as error:
            if error.status != 404:
                raise
            current = ""
        if not current:
            forge.create_branch(branch, sha)
            current = forge.branch_head(branch)
        if current != sha:
            fail("release_branch_taken", branch, current[:12], sha[:12])
        return forge.open_change(branch, base, title, body, draft)
    except forge.ForgeError as error:
        fail("release_write_refused", branch, base, error)


def command_cut(args: argparse.Namespace) -> None:
    release, base = release_branches()
    git("fetch", "--quiet", "--tags", "origin", base)
    ref = git("rev-parse", f"{BASE_BRANCH}^{{commit}}")
    result = plan(ref, major=False)
    if result["mode"] != "release":
        print(annotation("notice", "release_nothing_to_cut", base, result["tag"]), file=sys.stderr)
        return
    branch = CUT_PREFIX + result["tag"]
    prs, issues, pending = contents(result["previous"], ref)
    title = msg("release_cut_title", result["tag"])
    body = text_templates.render("change/release-cut", {
        "Tag": result["tag"], "PreviousTag": result["previous"], "SHA": ref, "ChangeCount": len(prs),
        "Changes": ", ".join(map(forge.change_ref, prs)) or msg("value_absent"),
        "Areas": ", ".join(changed_areas(result["previous"], ref)) or msg("value_absent"),
        "Issues": ", ".join(map(tracker.issue_ref, issues)) or msg("value_absent"),
        "PendingIssues": ", ".join(map(tracker.issue_ref, pending)),
    })
    print(msg("release_cut_branch", branch, ref))
    print(title)
    print()
    print(body)
    if args.push:
        opened = publish(branch, ref, release, title, body, draft=True, own=args.release_token, single=CUT_PREFIX)
        print(msg("release_opened", forge.change_ref(opened.number), opened.url))


def command_back_merge(args: argparse.Namespace) -> None:
    release, base = release_branches()
    behind = outside(args.ref)
    if behind == "0":
        print(annotation("notice", "release_back_merge_none", release, base), file=sys.stderr)
        return
    title = msg("release_back_merge_title", args.tag, base)
    body = text_templates.render("change/back-merge", {
        "Tag": args.tag, "SHA": args.ref, "CommitCount": behind, "BaseBranch": base})
    opened = publish(BACK_PREFIX + args.tag, args.ref, base, title, body, draft=False, own=args.release_token)
    print(msg("release_opened", forge.change_ref(opened.number), opened.url))


def next_arguments(step: argparse.ArgumentParser) -> None:
    step.add_argument("--ref", default="HEAD")
    step.add_argument("--major", action="store_true")
    step.add_argument("--version", default="", help="откат: выложить уже выпущенную версию")


def report_arguments(report: argparse.ArgumentParser) -> None:
    report.add_argument("--mode", required=True, choices=("release", "rollback"))
    report.add_argument("--tag", required=True)
    report.add_argument("--previous", default="")
    report.add_argument("--ref", default="HEAD")
    outcomes = ("success", "failure", "cancelled", "skipped")
    report.add_argument("--build", choices=outcomes, help="итог сборки")
    report.add_argument("--deploy", choices=outcomes, help="итог выкладки")
    report.add_argument("--tagged", choices=outcomes, help="итог тега")
    report.add_argument("--body-file", default="", help="файл текста отчета без заголовка")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    step = commands.add_parser("next")
    next_arguments(step)
    step.set_defaults(handler=command_next)

    report = commands.add_parser("report")
    report_arguments(report)
    report.set_defaults(handler=command_report)

    unconnected = commands.add_parser("no-deploy")
    unconnected.add_argument("--version", default="")
    unconnected.set_defaults(handler=command_no_deploy)

    cut = commands.add_parser("cut")
    cut.add_argument("--push", action="store_true", help="создать ветку и черновой PR в ветку релиза")
    cut.set_defaults(handler=command_cut)

    back = commands.add_parser("back-merge", help="PR обратного слияния выпущенного коммита в базовую ветку")
    back.add_argument("--tag", required=True)
    back.add_argument("--ref", required=True, help="точный SHA выпущенного коммита")
    back.set_defaults(handler=command_back_merge)

    for command in (cut, back):
        command.add_argument("--release-token", action="store_true",
                             help="ветку и PR создает токен из настройки release/tag-flow.token, а не учетные данные вызывающего")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
