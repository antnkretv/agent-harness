#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import base64
import dataclasses
import datetime as dt
import functools
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import script_paths  # noqa: E402,F401
import agent_provider  # noqa: E402
import rest  # noqa: E402
import session_cost  # noqa: E402
import text_templates  # noqa: E402
from ci_common import ROOT, forge, git, module_paths, msg, release_branches, setting, tracker  # noqa: E402

MODULE = "alert-triage"
NOTIFY = os.path.join(".harness", "notify", "notify.sh")
PATHS = "GRAFANA_OPS_PATHS"

MIN_STEP = 60
POINTS = 60
SHOWN_POINTS = 2 * POINTS
LINE_CHARS = 1000
NAMES = 300
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TRACE_ID = re.compile(r"[0-9a-fA-F]{1,32}")
SPANS = 200
RELEASES = "release/tag-flow"
RELEASE_LINES = 40
ROLLOUT = 3600

RETRY_DELAY = dt.timedelta(minutes=10)
PAGES = 3
MARK = "<!-- alert-{}: {} -->"
SEEN, ASKED, ANALYSIS = "seen", "triage", "analysis"
QUEUED, COMPLETED, FAILED, RUN = "queued", "completed", "failed", "run"
NOTIFY_TIMEOUT = 120
ANALYSIS_CHARS = 60000


class Refused(Exception):
    pass


class GrafanaError(RuntimeError):
    def __init__(self, why: str, status: int | None = None):
        super().__init__(why)
        self.status = status


@functools.cache
def config(key: str):
    return setting(MODULE, key)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def moment(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00"))


def iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def grafana_url() -> str:
    return str(config("grafana") or "").rstrip("/")


def grafana(path: str, query: dict | None = None):
    base = grafana_url()
    if not base:
        raise Refused(msg("triage_no_grafana_url"))
    auth = str(config("auth") or "")
    if auth and urllib.parse.urlsplit(base).scheme != "https":
        raise Refused(msg("triage_grafana_https"))
    private = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode("ascii")} if auth else {}
    headers = {"Accept": "application/json", **({config("header"): "1"} if auth and config("header") else {})}
    url = base + path + ("?" + urllib.parse.urlencode(query) if query else "")
    for attempt in range(7):
        try:
            return rest.request("GET", url, headers, GrafanaError, private=private)
        except GrafanaError as error:
            if auth and error.status == 429 and attempt < 6:
                time.sleep(min(2 ** attempt, 30))
                continue
            if error.status == 403:
                raise Refused(msg("triage_grafana_forbidden", path) if auth else msg("triage_no_grafana_auth")) from None
            if error.status is None:
                raise Refused(msg("triage_grafana_unreachable", base, error)) from None
            raise Refused(msg("triage_grafana_http", path, error)) from None


@functools.cache
def route_paths() -> dict[str, str]:
    route = os.path.join(ROOT, str(config("pathsFile") or ""))
    try:
        with open(route, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except OSError:
        raise Refused(msg("triage_no_route", PATHS, route)) from None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == PATHS for t in node.targets):
            return ast.literal_eval(node.value)
    raise Refused(msg("triage_no_route", PATHS, route))


def ns(seconds: float) -> int:
    return int(seconds * 1e9)


def duration(text: str) -> int:
    parts = re.findall(r"(\d+)([smhdw])", text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        raise argparse.ArgumentTypeError(msg("triage_bad_duration", text))
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return sum(int(number) * scale[unit] for number, unit in parts)


def point(text: str) -> float:
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        return moment(text).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(msg("triage_bad_time", text)) from None


def trace_id(text: str) -> str:
    if not TRACE_ID.fullmatch(text):
        raise argparse.ArgumentTypeError(msg("triage_bad_trace_id", text))
    return text


def clean(text: str) -> str:
    return CONTROL.sub("?", text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + "...")


def series(metric: dict) -> str:
    pairs = ", ".join(f'{key}="{value}"' for key, value in sorted(metric.items()) if key != "__name__")
    return f"{metric.get('__name__', '')}{{{pairs}}}"


def result(answer: dict) -> dict:
    if answer.get("status") != "success":
        raise Refused(msg("triage_query_error", answer.get("error") or answer))
    return answer.get("data", {})


def samples(data: dict, limit: int) -> None:
    kind, found = data.get("resultType"), data.get("result")
    if kind in ("scalar", "string"):
        print(f"{stamp(float(found[0]))} {found[1]}")
        return
    if not found:
        print(msg("value_empty"))
    for item in found[:limit]:
        if kind == "vector":
            print(f"{series(item.get('metric', {}))} {item['value'][1]}")
            continue
        points = item.get("values", [])
        print(series(item.get("metric", {})))
        if len(points) > SHOWN_POINTS:
            print("  " + msg("triage_more_points", len(points) - SHOWN_POINTS))
        for at, value in points[-SHOWN_POINTS:]:
            print(f"  {stamp(float(at))} {value}")
    if len(found) > limit:
        print(msg("triage_more_series", len(found) - limit))


def prom(args: argparse.Namespace) -> None:
    if args.range is None:
        answer = grafana(route_paths()["prometheus"] + "query", {"query": args.expr, "time": args.end})
        samples(result(answer), args.limit)
        return
    step = args.step or max(MIN_STEP, math.ceil(args.range / POINTS))
    query = {"query": args.expr, "start": args.end - args.range, "end": args.end, "step": step}
    samples(result(grafana(route_paths()["prometheus"] + "query_range", query)), args.limit)


def loki(args: argparse.Namespace) -> None:
    query = {
        "query": args.expr,
        "start": ns(args.end - args.since),
        "end": ns(args.end),
        "limit": args.limit,
        "direction": "backward",
    }
    data = result(grafana(route_paths()["loki"] + "query_range", query))
    if data.get("resultType") != "streams":
        samples(data, args.limit)
        return
    streams = sorted(data.get("result", []), reverse=True,
                     key=lambda stream: max((int(value[0]) for value in stream["values"]), default=0))
    if not streams:
        print(msg("value_empty"))
    for stream in streams:
        print(series(stream.get("stream", {})))
        for at, line, *_ in stream["values"]:
            print(f"  {stamp(int(at) / 1e9)} {clean(line)}")


def started(item: dict) -> int:
    return int(item.get("startTimeUnixNano") or 0)


def attributes(items: list[dict]) -> dict[str, str]:
    found = {}
    for item in items:
        value = item.get("value", {})
        kind = next((key for key in ("stringValue", "intValue", "doubleValue", "boolValue") if key in value), None)
        found[item.get("key", "")] = str(value[kind]) if kind else json.dumps(value, ensure_ascii=False)
    return found


def traces(args: argparse.Namespace) -> None:
    query = {"q": args.expr, "start": int(args.end - args.since), "end": int(args.end), "limit": args.limit}
    found = grafana(route_paths()["tempo"] + "search", query).get("traces") or []
    names([clean(f"{stamp(started(item) / 1e9)} {item.get('traceID', '')} {item.get('durationMs', 0)} ms "
                 f"{item.get('rootServiceName', '')} {item.get('rootTraceName', '')}") for item in found],
          args.limit)


def span_line(span: dict, service: str, origin: int, depth: int) -> str:
    status = span.get("status") or {}
    failed = f" ERROR {status.get('message', '')}" if status.get("code") == "STATUS_CODE_ERROR" else ""
    tags = " ".join(f"{key}={value}" for key, value in attributes(span.get("attributes", [])).items())
    offset, length = (started(span) - origin) / 1e6, (int(span.get("endTimeUnixNano") or 0) - started(span)) / 1e6
    return '  ' * depth + clean(
        f"+{offset:.0f} ms {length:.0f} ms {service} {span.get('name', '')}{failed} {tags}")


def trace(args: argparse.Namespace) -> None:
    answer = grafana(route_paths()["tempo"] + "v2/traces/" + args.trace_id) or {}
    spans = sorted(((attributes(block.get("resource", {}).get("attributes", [])).get("service.name", ""), span)
                    for block in answer.get("trace", {}).get("resourceSpans", [])
                    for scope in block.get("scopeSpans", []) for span in scope.get("spans", [])),
                   key=lambda pair: started(pair[1]))
    known = {span.get("spanId") for _, span in spans}
    children: dict[str | None, list[tuple[str, dict]]] = {}
    for service, span in spans:
        parent = span.get("parentSpanId")
        children.setdefault(parent if parent in known else None, []).append((service, span))
    origin = started(spans[0][1]) if spans else 0

    def walk(parent: str | None, depth: int):
        for service, span in children.get(parent, []):
            yield span_line(span, service, origin, depth)
            if span.get("spanId"):
                yield from walk(span["spanId"], depth + 1)

    names(list(itertools.islice(walk(None, 0), args.limit)), args.limit)
    if len(spans) > args.limit:
        print(msg("triage_more_spans", len(spans) - args.limit))


def names(found: list[str], limit: int = NAMES) -> None:
    print("\n".join(found[:limit]) if found else msg("value_empty"))
    if len(found) > limit:
        print(msg("triage_more_names", len(found) - limit))


def metrics(args: argparse.Namespace) -> None:
    try:
        pattern = re.compile(args.pattern, re.IGNORECASE)
    except re.error as error:
        raise Refused(msg("triage_bad_pattern", args.pattern, error)) from None
    found = result(grafana(route_paths()["prometheus"] + "label/__name__/values"))
    names([name for name in found if pattern.search(name)])


def loki_labels(args: argparse.Namespace) -> None:
    end = now().timestamp()
    path = f"label/{urllib.parse.quote(args.name, safe='')}/values" if args.name else "labels"
    names(result(grafana(route_paths()["loki"] + path, {"start": ns(end - args.since), "end": ns(end)})))


def alerts(_: argparse.Namespace) -> None:
    found = sorted(grafana(route_paths()["alerts"]), key=lambda alert: alert.get("startsAt", ""))
    if not found:
        print(msg("value_empty"))
    for alert in found:
        tags = dict(alert.get("labels", {}))
        name, severity = tags.pop("alertname", ""), tags.pop("severity", "")
        state = alert.get("status", {}).get("state", "")
        summary = alert.get("annotations", {}).get("summary", "")
        print(f"{state} {severity} {name} {series(tags)} {alert.get('startsAt', '')} {summary}")


def tag_dates() -> dict[str, dt.datetime]:
    found = git("for-each-ref", "--format=%(creatordate:iso-strict) %(refname:strip=2)", "refs/tags").splitlines()
    return {name: moment(created).astimezone(dt.timezone.utc)
            for created, name in (line.split(" ", 1) for line in found)}


def changes(span: str) -> None:
    print(msg("triage_release_changes", span, git("rev-list", "--count", "--no-merges", span), RELEASE_LINES))
    print(git("log", "--no-merges", "--format=%h %s", "-n", str(RELEASE_LINES), span))
    print(git("diff", "--stat", f"--stat-count={RELEASE_LINES}", span))


def release(args: argparse.Namespace) -> None:
    if RELEASES not in module_paths():
        print(msg("triage_release_off", RELEASES))
        return
    import release as releases
    at = dt.datetime.fromtimestamp(args.at, dt.timezone.utc)
    dates = tag_dates()
    known = sorted((dates[name], name) for _, name in releases.versions(releases.PREFIX))
    earlier = [item for item in known if item[0] <= at]
    if not earlier:
        print(msg("triage_release_none", iso(at), releases.PREFIX))
        return
    date, live = earlier[-1]
    age = (at - date).total_seconds()
    print(msg("triage_release_live", iso(at), live, iso(date), session_cost.duration(age)))
    branch = "origin/" + release_branches()[0]
    untagged = git("log", "--first-parent", "--format=%h", "--until=" + iso(at), f"{live}..{branch}").splitlines()
    if not git("rev-parse", "--verify", "--quiet", branch):
        print(msg("triage_release_no_branch", branch))
    elif untagged:
        print(msg("triage_release_untagged", branch, live, len(untagged)))
        changes(live + ".." + untagged[0])
    later = [name for _, name in known[len(earlier):]]
    if later:
        print(msg("triage_release_later", ", ".join(later)))
    if len(earlier) < 2:
        print(msg("triage_release_first", live))
        return
    previous = earlier[-2][1]
    if age < ROLLOUT:
        print(msg("triage_release_fresh", previous))
    changes(previous + ".." + live)


def episodes_file() -> str:
    return os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "alert-episodes.json")


def plain(text: str) -> str:
    return text.replace("<", "&lt;")


@functools.cache
def marker(name: str) -> re.Pattern:
    return re.compile(MARK.format(name, r"(\S+)"))


def field(body: str | None, name: str) -> str | None:
    found = marker(name).search(body or "")
    return found.group(1) if found else None


def marked(body: str, name: str, value: str) -> str:
    line = MARK.format(name, value)
    body, replaced = marker(name).subn(lambda _: line, body, count=1)
    return body if replaced else marker("key").sub(lambda found: found.group(0) + "\n" + line, body, count=1)


def alert_key(tags: dict) -> str:
    key = f"{tags.get('alertname', '')}/{tags.get(config('component'), '-')}"
    group = tags.get(config("group"))
    return f"{key}/{group}" if group else key


def headline(tags: dict) -> str:
    return " ".join(part for part in (tags.get("alertname", ""), tags.get(config("group"))) if part)


def firing() -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for alert in grafana(route_paths()["alerts"], {"active": "true", "silenced": "false", "inhibited": "false"}):
        tags = alert.get("labels", {})
        if tags.get("severity") in config("severities") and alert.get("status", {}).get("state") == "active":
            found.setdefault(alert_key(tags), []).append(alert)
    return {key: sorted(items, key=lambda alert: moment(alert["startsAt"])) for key, items in found.items()}


@functools.cache
def author() -> str:
    return tracker.login()


def priority(issue: tracker.Issue) -> tuple[bool, int]:
    return not issue.open, int(issue.key)


def alert_issues() -> dict[str, tracker.Issue]:
    found: dict[str, tracker.Issue] = {}
    for issue in tracker.find_issues(config("label"), open_only=False, closed_pages=PAGES):
        key = field(issue.body, "key")
        if not key or issue.author != author():
            continue
        known = found.get(key)
        if known is None or priority(issue) < priority(known):
            found[key] = issue
    return found


def find_alert_issue(key: str) -> tracker.Issue | None:
    found, complete = tracker.search_issues(config("label"), key)
    if not complete:
        raise Refused(msg("triage_issue_lookup_incomplete", key))
    matches = [issue for issue in found if issue.author == author() and field(issue.body, "key") == key]
    return min(matches, key=priority, default=None)


def load_result(issue: tracker.Issue) -> tracker.Issue:
    body = issue.body
    asked, run = field(body, ASKED), field(body, RUN)
    completed = field(body, COMPLETED)
    if not asked or not run or (completed and moment(completed) >= moment(asked)):
        return issue
    for note in tracker.issue_notes(issue.key, since=asked):
        if note.author != author() or field(note.body, ASKED) != asked or field(note.body, RUN) != run:
            continue
        for name in (COMPLETED, FAILED):
            value = field(note.body, name)
            if value and (name != COMPLETED or value == asked):
                previous = field(body, name)
                if not previous or moment(value) > moment(previous):
                    body = marked(body, name, value)
    if body == issue.body:
        return issue
    tracker.update_issue(issue.key, body)
    return dataclasses.replace(issue, body=body)


def primary_issue(issue: tracker.Issue | None, known: dict[str, tracker.Issue]) -> tracker.Issue | None:
    duplicate = field(issue.body if issue else None, "duplicate")
    if duplicate is None:
        return issue
    if not duplicate.isascii() or not duplicate.isdigit():
        raise Refused(msg("triage_bad_duplicate", issue.key, duplicate, config("label")))
    primary = known.get(duplicate) or tracker.issue(duplicate)
    if (primary is None or primary.author != author() or config("label") not in primary.labels
            or not field(primary.body, "key") or field(primary.body, "duplicate") is not None):
        raise Refused(msg("triage_bad_duplicate", issue.key, duplicate, config("label")))
    known[duplicate] = primary
    return primary


def pending(issue: tracker.Issue) -> bool:
    completed = field(issue.body, COMPLETED)
    return completed is None or any(
        value is not None and moment(value) > moment(completed)
        for value in (field(issue.body, ASKED), field(issue.body, QUEUED)))


@functools.cache
def run_active(run: str) -> bool:
    return not forge.run_finished(run)


def busy(issue: tracker.Issue) -> bool:
    run = field(issue.body, RUN)
    return bool(field(issue.body, ASKED) and pending(issue) and run and run_active(run))


def requested_body(body: str) -> str:
    body = marked(body, ASKED, iso(now()))
    return marked(body, RUN, forge.run_id()) if forge.run_id() else body


def decision(issue: tracker.Issue | None, agent: bool, taken: int) -> str:
    if not agent:
        return msg("triage_decision_off")
    body = issue.body if issue else ""
    if issue and busy(issue):
        return msg("triage_decision_busy")
    failed = field(body, FAILED)
    asked = field(body, ASKED)
    if issue and pending(issue) and asked:
        if max(moment(asked), moment(failed or asked)) > now() - RETRY_DELAY:
            return msg("triage_decision_retry")
    completed, hours, limit = field(body, COMPLETED), int(config("cooldownHours")), int(config("limit"))
    if (issue and issue.open and completed and (not asked or moment(completed) >= moment(asked))
            and moment(completed) > now() - dt.timedelta(hours=hours)):
        return msg("triage_decision_recent", hours)
    if taken >= limit:
        return msg("triage_decision_limit", limit)
    return ""


def run_link() -> str:
    return f"[{msg('triage_episode_run')}]({forge.run_url()})"


def note_text(note: str) -> str:
    return text_templates.render("comment/alert-note", {"TriageNote": note})


def block(fresh: list[dict], note: str) -> str:
    items = []
    for alert in fresh:
        tags = {k: v for k, v in alert["labels"].items() if k not in ("alertname", "severity", config("component"))}
        items.append(msg("triage_episode_item", alert["labels"].get("alertname", ""),
                         " " + plain(series(tags)) if tags else "", moment(alert["startsAt"]).strftime("%H:%M")))
    return text_templates.render("comment/alert-episode", {
        "AlertStarted": moment(fresh[0]["startsAt"]).strftime("%Y-%m-%d %H:%M"), "AlertCount": len(fresh),
        "AlertItems": "\n".join(items), "TriageDecision": note, "RunLink": run_link()})


def issue_title(alert: dict) -> str:
    tags, notes = alert["labels"], alert.get("annotations", {})
    summary = notes.get("summary") or tags.get(config("component"), "")
    return msg("value_pair", tags.get("alertname", ""), summary[:80])


def issue_body(key: str, alert: dict, episode: str) -> str:
    tags, notes = alert["labels"], alert.get("annotations", {})
    quoted = "\n>\n".join("\n".join("> " + plain(line) for line in text.splitlines())
                          for text in (notes.get("summary"), notes.get("description")) if text)
    source = config("alertmanager")
    query = "?" + urllib.parse.urlencode({"alertmanager": source}) if source else ""
    return MARK.format("key", key) + "\n\n" + text_templates.render("issue/alert", {
        "AlertName": tags.get("alertname", ""), "AlertComponent": tags.get(config("component"), "-"),
        "AlertSeverity": tags.get("severity", ""), "AlertNotes": quoted, "AlertEpisode": episode,
        "AlertsLink": f"[{msg('triage_alerts_link')}]({grafana_url()}/alerting/groups{query})"})


def publish(requested: list[int]) -> None:
    value = json.dumps(requested)
    if forge.in_ci():
        forge.set_output("issues", value)
    else:
        print(f"issues={value}")


def select(agent: bool) -> None:
    active = firing()
    known: dict[str, tracker.Issue] = {}
    keys: dict[str, str] = {}
    for alert, issue in alert_issues().items():
        keys[alert], known[issue.key] = issue.key, issue
    loaded: set[str] = set()

    def load_once(key: str) -> tracker.Issue:
        if key not in loaded:
            known[key] = load_result(known[key])
            loaded.add(key)
        return known[key]

    for source in list(keys.values()):
        primary = primary_issue(known[source], known)
        if primary.key != source:
            keys.setdefault(field(primary.body, "key"), primary.key)
    for key in list(known):
        if known[key].open:
            load_once(key)
    requested: list[int] = []
    entries: list[dict] = []
    order = config("severities")
    try:
        for alert, key in keys.items():
            if known[key].open and pending(known[key]) and not field(known[key].body, "duplicate"):
                active.setdefault(alert, [])
        for alert in sorted(active, key=lambda k: (min((order.index(a["labels"]["severity"]) for a in active[k]), default=1),
                                                   moment(active[k][0]["startsAt"]) if active[k] else moment("1970-01-01T00:00:00Z"))):
            items = active[alert]
            source = known[keys[alert]] if alert in keys else None
            if source is None:
                source = find_alert_issue(alert)
                if source is not None:
                    known[source.key] = source
                    source = load_once(source.key)
            seen = field(source.body if source else None, SEEN)
            fresh = [item for item in items if seen is None or moment(item["startsAt"]) > moment(seen)]
            issue = primary_issue(source, known)
            if issue:
                issue = load_once(issue.key)
                source = known[source.key]
            if not fresh and (issue is None or not issue.open or not pending(issue)):
                continue
            note = decision(issue, agent, len(requested))
            wanted = not note
            note = note or msg("triage_decision_requested")
            if not fresh and not wanted:
                continue
            text = block(fresh, note) if fresh else note_text(msg("triage_retry_requested", forge.run_url()))
            same = (source.key if source else None) == (issue.key if issue else None)
            body = issue_body(alert, fresh[0], text) if issue is None else issue.body
            if fresh and same:
                body = marked(body, SEEN, fresh[-1]["startsAt"])
            if fresh:
                body = marked(body, QUEUED, iso(now()))
            if wanted:
                body = requested_body(body)
            created = issue is None
            if created:
                issue = tracker.create_issue(issue_title(fresh[0]), body, (config("label"),))
            else:
                tracker.update_issue(issue.key, body, reopen=True)
                issue = dataclasses.replace(issue, body=body, open=True)
            known[issue.key] = issue
            if wanted:
                requested.append(int(issue.key))
            if fresh and not same and source is not None:
                seen_body = marked(source.body, SEEN, fresh[-1]["startsAt"])
                tracker.update_issue(source.key, seen_body)
                known[source.key] = dataclasses.replace(source, body=seen_body)
            if not created:
                tracker.comment_issue(issue.key, text)
            if fresh:
                entries.append({
                    "alertname": headline(fresh[0]["labels"]),
                    "issue": int(issue.key),
                    "url": issue.url,
                    "since": int(moment(fresh[0]["startsAt"]).timestamp()) - 60,
                    "note": note,
                })
            print(msg("triage_recorded", alert, issue.key, note))
    finally:
        publish(requested)
        with open(episodes_file(), "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False)
    if not entries and not requested:
        print(msg("triage_quiet", ", ".join(order)))


def dispatch(number: str, agent: bool) -> None:
    if not number.isdigit():
        raise Refused(msg("triage_bad_issue", number))
    if not agent:
        raise Refused(msg("triage_agent_off"))
    issue = tracker.issue(number)
    if issue is None or config("label") not in issue.labels:
        raise Refused(msg("triage_not_alert", number, config("label")))
    issue = load_result(primary_issue(issue, {issue.key: issue}))
    if busy(issue):
        raise Refused(msg("triage_decision_busy"))
    tracker.update_issue(issue.key, requested_body(issue.body), reopen=True)
    publish([int(issue.key)])
    tracker.comment_issue(issue.key, note_text(msg("triage_manual", forge.run_url())))


def run_select(_: argparse.Namespace) -> int:
    if not forge.repo():
        raise Refused(msg("triage_no_repository"))
    provider = os.environ.get("AGENT_PROVIDER", "")
    agent = provider in agent_provider.KINDS and agent_provider.authorized(provider)
    number = os.environ.get("ISSUE", "").strip()
    if number:
        dispatch(number, agent)
    else:
        select(agent)
    return 0


def notifier() -> str:
    path = config("notify") or (NOTIFY if "notifications" in module_paths() else "")
    return os.path.join(ROOT, path) if path else ""


def link(_: argparse.Namespace) -> int:
    try:
        with open(episodes_file(), encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return 0
    notify = notifier()
    if not notify:
        return 0
    channel, thread = config("notifyChannel"), config("notifyThread")
    failed = False
    for entry in entries:
        env = dict(os.environ,
                   NOTIFY_KIND="alert-triage",
                   NOTIFY_TITLE=msg("triage_link_title", entry["alertname"]),
                   NOTIFY_BODY=msg("triage_link_body", entry["issue"], entry["url"], entry["note"]),
                   NOTIFY_SLACK_THREAD_SINCE=str(entry["since"]))
        if channel:
            env["NOTIFY_SLACK_CHANNEL"] = channel
        if thread:
            env["NOTIFY_SLACK_THREAD_MATCH"] = thread.replace("{}", re.escape(entry["alertname"]))
        try:
            failed |= subprocess.run([notify], env=env, timeout=NOTIFY_TIMEOUT, check=False).returncode != 0
        except subprocess.TimeoutExpired:
            print(msg("triage_link_timeout", entry["issue"], NOTIFY_TIMEOUT), file=sys.stderr)
            failed = True
    return 1 if failed else 0


def refers(text: str, number: str) -> bool:
    return number in {reference.number for reference in tracker.references(text)}


def fixed(number: str, value: str | None, agent: str, since: str) -> bool:
    if not value or not value.isascii() or not value.isdigit():
        return False
    change = forge.change(int(value))
    if change is None:
        return False
    if not (change.author == agent and refers(change.body, number)) and not any(
            note.author == agent and moment(note.created) >= moment(since) and refers(note.body, number)
            for note in forge.notes(change.number)):
        return False
    return change.state == "merged" or (change.state == "open" and forge.review_label("managedLabel") in change.labels
                                        and forge.review_requested(change.number, change.head_sha))


def published(_: argparse.Namespace) -> int:
    number, since = os.environ.get("ISSUE", "").strip(), os.environ.get("SINCE", "").strip()
    if not number.isdigit():
        raise Refused(msg("triage_bad_issue", number))
    issue = tracker.issue(number)
    if issue is None:
        raise Refused(msg("triage_not_alert", number, config("label")))
    if field(issue.body, RUN) != forge.run_id():
        raise Refused(msg("triage_superseded", number))
    agent = os.environ.get("AGENT_LOGIN", "").strip()
    shared = not agent or agent == author()
    complete = False
    if os.environ.get("AGENT_OUTCOME") == "success" and not shared:
        for note in tracker.issue_notes(number, since=since):
            if (note.author != agent or moment(note.updated or note.created) < moment(since)
                    or field(note.body, ANALYSIS) != number):
                continue
            outcome = field(note.body, "result")
            complete = outcome == "external" or outcome == "pr" and fixed(number, field(note.body, "pr"), agent, since)
            if complete:
                break
    asked = field(issue.body, ASKED) or since
    url = forge.run_url()
    receipt = note_text(msg("triage_completed", url) if complete
                        else msg("triage_agent_shared", url) if shared else msg("triage_unpublished", url))
    for name, value in ((ASKED, asked), (RUN, field(issue.body, RUN)),
                        (COMPLETED if complete else FAILED, asked if complete else iso(now()))):
        receipt += "\n" + MARK.format(name, value)
    tracker.comment_issue(number, receipt)
    if complete:
        return 0
    print(msg("triage_unpublished_log", number), file=sys.stderr)
    return 1


def analysis(args: argparse.Namespace) -> int:
    from authorship import unsigned
    from text_data import inert, unmarked
    from text_gate import refusal

    with open(args.body_file, encoding="utf-8") as handle:
        text = text_templates.render("comment/note", {
            "NoteType": text_templates.choice("NoteType", "alert"), "AgentReply": inert(handle.read().strip()),
            "SessionModel": agent_provider.session_model(), "SessionLink": agent_provider.session_link(),
            "SHA": git("rev-parse", "HEAD")})
    marks = [MARK.format(ANALYSIS, args.issue)]
    if args.pr:
        marks += [MARK.format("result", "pr"), MARK.format("pr", args.pr)]
    elif args.external:
        marks.append(MARK.format("result", "external"))
    body = text_templates.published("comment/note", text, None, "\n".join(marks))
    if len(body) > ANALYSIS_CHARS:
        print(msg("triage_analysis_long", len(body), ANALYSIS_CHARS), file=sys.stderr)
        return 1
    if reason := refusal(body, capped=False):
        print(reason, file=sys.stderr)
        return 1
    tracker.comment_issue(str(args.issue), body if tracker.html_comments else unmarked(unsigned(body)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Разбор алертов: отбор, ссылка в канале уведомлений, телеметрия.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("select", help="записать срабатывания и продолжить незавершенные разборы").set_defaults(run=run_select)
    commands.add_parser("link", help="ссылка на issue в треде алерта канала уведомлений").set_defaults(run=link)
    commands.add_parser("published", help="проверить результат разбора и записать успех или провал").set_defaults(run=published)
    note = commands.add_parser("analysis", help="разбор или итог разбора комментарием в issue с маркерами результата")
    note.add_argument("--issue", type=int, required=True)
    note.add_argument("--body-file", required=True)
    result = note.add_mutually_exclusive_group()
    result.add_argument("--pr", type=int, help="итог: исправление в PR с этим номером")
    result.add_argument("--external", action="store_true", help="итог: подтверждена внешняя причина")
    note.set_defaults(run=analysis)

    query = commands.add_parser("prom", help="запрос PromQL: мгновенный или за период")
    query.add_argument("expr")
    query.add_argument("--range", type=duration, help="период до --end, например 3h; без него - мгновенный")
    query.add_argument("--step", type=duration, help="шаг периода; по умолчанию около 60 точек")
    query.add_argument("--end", type=point, default=now().timestamp(),
                       help="конец: RFC3339 или unix-время; по умолчанию сейчас")
    query.add_argument("--limit", type=int, default=30, help="рядов в выводе")
    query.set_defaults(run=prom)

    window = argparse.ArgumentParser(add_help=False)
    window.add_argument("--since", type=duration, default=3600, help="период до --end, по умолчанию 1h")
    window.add_argument("--end", type=point, default=now().timestamp(),
                        help="конец: RFC3339 или unix-время; по умолчанию сейчас")

    logs = commands.add_parser("loki", parents=[window], help="запрос LogQL: строки журнала или метрика по ним")
    logs.add_argument("expr")
    logs.add_argument("--limit", type=int, default=100, help="строк журнала, самые новые")
    logs.set_defaults(run=loki)

    search = commands.add_parser("traces", parents=[window], help="поиск трасс Tempo запросом TraceQL")
    search.add_argument("expr")
    search.add_argument("--limit", type=int, default=20, help="трасс в выводе")
    search.set_defaults(run=traces)

    one = commands.add_parser("trace", help="дерево спанов одной трассы: смещение, длительность, служба, отказ")
    one.add_argument("trace_id", type=trace_id)
    one.add_argument("--limit", type=int, default=SPANS, help="спанов в выводе")
    one.set_defaults(run=trace)

    found = commands.add_parser("metrics", help="имена метрик Prometheus по регулярному выражению")
    found.add_argument("pattern", nargs="?", default="")
    found.set_defaults(run=metrics)

    streams = commands.add_parser("labels", help="метки потоков Loki или значения одной метки")
    streams.add_argument("name", nargs="?")
    streams.add_argument("--since", type=duration, default=3600, help="период, по умолчанию 1h")
    streams.set_defaults(run=loki_labels)

    commands.add_parser("alerts", help="алерты по пути alerts маршрута вместе с заглушенными").set_defaults(run=alerts)

    live = commands.add_parser("release", help="релиз, работавший в заданный момент, и его изменения от предыдущего")
    live.add_argument("--at", type=point, required=True,
                      help="момент: RFC3339 или unix-время, обычно начало отказа")
    live.set_defaults(run=release)

    args = parser.parse_args()
    try:
        return args.run(args) or 0
    except Refused as error:
        print(error, file=sys.stderr)
        return 1
    except (tracker.TrackerError, forge.ForgeError) as error:
        print(msg("triage_refused", error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
