from __future__ import annotations
import hashlib
import io
import json
import os
import re
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
import forge
import rest
from manifest import get
from github_transport import TOKEN
from platform_messages import msg

EVIDENCE_BYTES = 16 * 1024 * 1024
EVIDENCE_FILES = 64
EVIDENCE_RUNS = 32


class ReviewEvidence:
    def __init__(self, forge):
        self.forge = forge
        self._evidence_cache = {}
        self._evidence_runs = set()

    def _evidence(self, key: tuple, read, seconds: int=15):
        now = time.monotonic()
        cached = self._evidence_cache.get(key)
        if cached and now - cached[0] < seconds:
            return cached[1]
        value = read()
        if key[0] == 'artifact':
            artifacts = [entry for entry in self._evidence_cache if entry[0] == 'artifact']
            if len(artifacts) >= 8:
                self._evidence_cache.pop(artifacts[0])
        if len(self._evidence_cache) >= EVIDENCE_RUNS * 8:
            self._evidence_cache.pop(next(iter(self._evidence_cache)))
        self._evidence_cache[key] = (now, value)
        return value

    def _review_run_id(self, url: str) -> str:
        found = re.fullmatch(re.escape(self.forge._url('actions', 'runs')) + '/([1-9][0-9]*)', url)
        if not found:
            raise forge.ForgeError(msg('github_evidence_untrusted', url))
        run = found.group(1)
        if run not in self._evidence_runs and len(self._evidence_runs) >= EVIDENCE_RUNS:
            raise forge.ForgeError(msg('github_evidence_untrusted', url))
        self._evidence_runs.add(run)
        return run

    def _evidence_items(self, run: str, kind: str) -> list:
        source = self._evidence((run, 'run'), lambda: self.forge._call('GET', 'actions', 'runs', run))
        attempt = str(source.get('run_attempt') or '')
        if not attempt.isdecimal():
            raise forge.ForgeError(msg('github_evidence_untrusted', run))

        def read():
            result = []
            for page in range(1, 5):
                parts = ('attempts', attempt) if kind == 'jobs' else ()
                body = self.forge._call('GET', 'actions', 'runs', run, *parts, f'{kind}?per_page=100&page={page}')
                rows = body.get(kind)
                if not isinstance(rows, list):
                    break
                result.extend(rows)
                if len(rows) < 100 or (isinstance(body.get('total_count'), int) and len(result) >= body['total_count']):
                    return result
            raise forge.ForgeError(msg('github_evidence_untrusted', run))
        return self._evidence((run, kind, attempt), read)

    def review_run(self, number: int, url: str, snapshot: dict) -> dict:
        run_id = self._review_run_id(url)
        run = self._evidence((run_id, 'run'), lambda: self.forge._call('GET', 'actions', 'runs', run_id))
        workflow = get('processes.review.workflow', '', file='methodology.json')
        repository = run.get('repository') or {}
        if str(run.get('id')) != run_id or repository.get('full_name') != self.forge.repo() or (not repository.get('id')) or (run.get('path') != workflow) or (run.get('event') not in ('pull_request_target', 'issue_comment', 'workflow_run')):
            raise forge.ForgeError(msg('github_evidence_untrusted', url))
        source = self._evidence(('workflow', run.get('workflow_id')), lambda: self.forge._call('GET', 'actions', 'workflows', str(run.get('workflow_id') or '')), 300)
        if not workflow or source.get('path') != workflow:
            raise forge.ForgeError(msg('github_evidence_untrusted', url))
        stamp = run.get('head_sha') or ''
        if run['event'] == 'pull_request_target':
            pulls = run.get('pull_requests') or []
            associated = pulls[0] if len(pulls) == 1 else {}
            target_base = associated.get('base') or {}
            if associated.get('number') != number or (associated.get('head') or {}).get('sha') != snapshot.get('head') or target_base.get('sha') != snapshot.get('base') or ((target_base.get('repo') or {}).get('id') != repository['id']) or (run.get('head_sha') not in (snapshot.get('head'), snapshot.get('base'))):
                raise forge.ForgeError(msg('github_evidence_untrusted', url))
            if not isinstance(target_base.get('sha'), str) or not re.fullmatch('[0-9a-f]{40}', target_base['sha']):
                raise forge.ForgeError(msg('github_evidence_untrusted', url))
        else:
            default = self._evidence(('default',), self.forge.default_branch, 300)
            if not default or run.get('head_branch') != default:
                raise forge.ForgeError(msg('github_evidence_untrusted', url))
            current = self._evidence(('default-head', default), lambda: self.forge.branch_head(default))
            if not re.fullmatch('[0-9a-f]{40}', stamp) or not re.fullmatch('[0-9a-f]{40}', current):
                raise forge.ForgeError(msg('github_evidence_untrusted', url))
            if stamp != current:
                compared = self._evidence(('ancestor', stamp, current), lambda: self.forge._call('GET', 'compare', f'{stamp}...{current}?per_page=1'), 300)
                if compared.get('status') not in ('ahead', 'identical') or (compared.get('merge_base_commit') or {}).get('sha') != stamp:
                    raise forge.ForgeError(msg('github_evidence_untrusted', url))
        files = self.review_artifact(url, 'review-plan')
        try:
            plan = json.loads(files.get('plan.json', b''))
        except (ValueError, UnicodeError):
            raise forge.ForgeError(msg('github_evidence_untrusted', url)) from None
        if not isinstance(plan, dict) or plan.get('v') != 2 or plan.get('yielded') or (plan.get('change') != number) or (plan.get('url') != url) or ({key: plan.get('base_sha' if key == 'base' else key) for key in snapshot} != snapshot) or (plan.get('reviewer') != 'github-actions[bot]') or (not isinstance(plan.get('passes'), list)):
            raise forge.ForgeError(msg('github_evidence_untrusted', url))
        return {'plan': plan, 'jobs': self._evidence_items(run_id, 'jobs')}

    def _artifact_zip(self, artifact: str) -> bytes:
        import urllib.request

        class Redirect(urllib.request.HTTPRedirectHandler):

            def redirect_request(self, request, fp, code, message, headers, newurl):
                from urllib.parse import urlsplit
                if urlsplit(newurl).scheme != 'https':
                    raise forge.ForgeError(msg('github_evidence_untrusted', artifact))
                redirected = super().redirect_request(request, fp, code, message, headers, newurl)
                if redirected is not None:
                    redirected.remove_header('Authorization')
                return redirected
        address = f"{self.forge.api.rest_url.rstrip('/')}/{self.forge.api.path('actions', 'artifacts', artifact, 'zip')}"
        token = self.forge.api.token() or self.forge.api.gh('auth', 'token', '--hostname', self.forge.api.host).strip()
        if not token:
            raise forge.ForgeUnavailable(msg('forge_no_token', TOKEN))
        request = urllib.request.Request(address, headers={'Accept': 'application/vnd.github+json', 'Authorization': f'bearer {token}', 'User-Agent': 'harness-github-adapter'})
        try:
            with urllib.request.build_opener(Redirect()).open(request, timeout=rest.TIMEOUT) as answer:
                data = answer.read(EVIDENCE_BYTES + 1)
        except OSError as error:
            code = getattr(error, 'code', None)
            reason = getattr(error, 'reason', None)
            tunnel = re.search('Tunnel connection failed: (\\d{3})\\b', str(reason))
            if code is None and tunnel:
                code = int(tunnel.group(1))
            why = f'HTTP {code}' if isinstance(code, int) else type(reason if isinstance(reason, BaseException) else error).__name__
            raise forge.ForgeError(msg('github_evidence_unread', artifact, why), code) from None
        if len(data) > EVIDENCE_BYTES:
            raise forge.ForgeError(msg('github_evidence_untrusted', artifact))
        return data

    def review_jobs(self, url: str) -> list:
        return self._evidence_items(self._review_run_id(url), 'jobs')

    def review_requested(self, number: int, head: str) -> bool:
        current = self.forge.change(number)
        if current is None or current.head_sha != head:
            return False
        if forge.review_label() in current.labels:
            return True
        base = self.forge._url('actions', 'runs')
        prefix = get('processes.review.statusPrefix', 'review', file='methodology.json') + '/'
        workflow = get('processes.review.workflow', '', file='methodology.json')
        runs = set()
        for name, status in self.forge.commit_results(head).items():
            found = re.fullmatch(re.escape(base) + '/([0-9]+)/?', status.url)
            if name.startswith(prefix) and status.author == 'github-actions[bot]' and found:
                runs.add(found.group(1))
        for run_id in sorted(runs, key=int, reverse=True):
            run = self._evidence((run_id, 'run'), lambda: self.forge._call('GET', 'actions', 'runs', run_id))
            if (run.get('event') != 'pull_request_target' or run.get('display_title') != self.forge.round_title(number)
                    or str(run.get('path') or '').split('@', 1)[0] != workflow
                    or (run.get('repository') or {}).get('full_name', '').casefold() != self.forge.repo().casefold()):
                continue
            pulls = run.get('pull_requests') or []
            associated = [pull for pull in pulls if pull.get('number') == number]
            if pulls and (not associated or any((pull.get('head') or {}).get('sha', head) != head for pull in associated)):
                continue
            if any(job.get('name') == 'prepare' and job.get('conclusion') != 'skipped'
                   for job in self.review_jobs(f'{base}/{run_id}')):
                current = self.forge.change(number)
                return current is not None and current.head_sha == head
        return False

    def review_artifact(self, url: str, name: str) -> dict[str, bytes]:
        run_id = self._review_run_id(url)
        artifacts = self._evidence_items(run_id, 'artifacts')
        matches = [item for item in artifacts if item.get('name') == name]
        if len(matches) != 1 or not isinstance(matches[0].get('id'), int):
            raise forge.ForgeError(msg('github_evidence_untrusted', url + ' ' + name))
        artifact = matches[0]
        run = self._evidence((run_id, 'run'), lambda: self.forge._call('GET', 'actions', 'runs', run_id))
        if not artifact.get('created_at') or not run.get('run_started_at') or artifact['created_at'] < run['run_started_at']:
            raise forge.ForgeError(msg('github_evidence_untrusted', url + ' ' + name))
        if artifact.get('expired') is True:
            raise forge.ForgeExpired(msg('github_evidence_expired', url + ' ' + name))
        if not 0 < artifact.get('size_in_bytes', 0) <= EVIDENCE_BYTES:
            raise forge.ForgeError(msg('github_evidence_untrusted', url + ' ' + name))

        def read():
            data = self._artifact_bytes(artifact)
            digest = artifact.get('digest')
            if digest and digest != 'sha256:' + hashlib.sha256(data).hexdigest():
                raise forge.ForgeError(msg('github_evidence_untrusted', url + ' ' + name))
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries = archive.infolist()
                    if len(entries) > EVIDENCE_FILES or sum((item.file_size for item in entries)) > EVIDENCE_BYTES or any((item.file_size > EVIDENCE_BYTES or item.flag_bits & 1 for item in entries)):
                        raise ValueError
                    result = {}
                    for item in entries:
                        path = PurePosixPath(item.filename)
                        if item.is_dir() or path.name != item.filename or '\\' in item.filename or (path.name in ('', '.', '..')) or (item.filename in result) or (item.external_attr >> 16 & 61440 == 40960):
                            raise ValueError
                        result[item.filename] = archive.read(item)
                    return result
            except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
                raise forge.ForgeError(msg('github_evidence_untrusted', url + ' ' + name)) from None
        return self._evidence(('artifact', artifact['id']), read, 300)

    def _artifact_bytes(self, artifact: dict) -> bytes:
        cache = os.environ.get('HARNESS_GITHUB_ARTIFACT_CACHE', '')
        if not cache:
            return self._artifact_zip(str(artifact['id']))
        digest = artifact.get('digest') or ''
        if not isinstance(digest, str) or not re.fullmatch('sha256:[0-9a-f]{64}', digest):
            raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id'])))
        path = Path(cache, str(artifact['id']) + '.zip')
        try:
            info = path.lstat()
        except FileNotFoundError:
            return self._artifact_zip(str(artifact['id']))
        except OSError:
            raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id']))) from None
        if not stat.S_ISREG(info.st_mode) or info.st_size > EVIDENCE_BYTES:
            raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id'])))
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            with os.fdopen(os.open(path, flags), 'rb') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id'])))
                data = handle.read(EVIDENCE_BYTES + 1)
        except OSError:
            raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id']))) from None
        if len(data) > EVIDENCE_BYTES:
            raise forge.ForgeError(msg('github_evidence_untrusted', str(artifact['id'])))
        return data
