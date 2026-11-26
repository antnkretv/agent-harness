from __future__ import annotations

import review_evidence
import review_policy
import process_config
import review_round
import review_verdict
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from ci_common import checks_state, forge, head_checks, msg, protected_status, release_change, threads_resolved
from manifest import entry_extensions
from pipeline_state import Blocked, Pending, digest, base_definitions, base_manifest, produced_by

CHECK_RUNS = 'pull_request_read:get_check_runs'


def cut_of(head: str, source: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        return False
    root = Path(__file__).resolve().parents[3]
    subprocess.run(["git", "-C", str(root), "fetch", "--quiet", "origin", source], capture_output=True, check=False, timeout=60)
    found = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", head, f"origin/{source}"],
                           capture_output=True, check=False, timeout=30)
    return found.returncode == 0


class Readiness:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def ready_readers(self) -> dict:
        return {'pull_request_read:get_status': self.ready_reviews, 'pull_request_read:get_review_comments': self.ready_threads, CHECK_RUNS: self.ready_checks}

    def reviewed(self) -> bool:
        return any(entry['process'] == 'review' for entry in self.pipeline.steps)

    def release_change(self, change) -> bool:
        try:
            project = base_manifest(change.base_sha, 'project/manifest.json')
            composition = base_manifest(change.base_sha, 'pipelines.json').get(self.pipeline.args.pipeline)
        except (Blocked, AttributeError, ValueError):
            return False
        git, flow = project.get('git', {}), entry_extensions(project.get('modules', {}).get('release'))
        release, source = git.get('releaseBranch'), git.get('baseBranch')
        return (bool(release and source) and 'tag-flow' in flow
                and change.base == release and composition == self.pipeline.composition
                and release_change(change.head, change.base)
                and cut_of(change.head_sha, source))

    def require_readers(self, ready: dict) -> None:
        required = set(self.ready_readers()) if self.reviewed() else {CHECK_RUNS}
        mismatch = {item.get('check') for item in ready.get('readiness', [])} ^ required
        if mismatch:
            raise Blocked(msg('runtime_unsupported_audit_composition') + ': ' + ', '.join(sorted(map(str, mismatch))))

    def ready_step(self) -> tuple[dict, dict]:
        found = produced_by(self.pipeline.steps, self.pipeline.processes, 'ready-pr')
        if not found:
            raise Blocked(msg('runtime_missing_process_definition') + 'ready-pr')
        return found

    def ready_inputs(self) -> None:
        change = self.pipeline.change()
        self.pipeline.claim(change.head, change)
        _, ready = self.ready_step()
        self.require_readers(ready)
        readers = self.ready_readers()
        for check in dict.fromkeys(item['check'] for item in ready['readiness']):
            readers[check](change)

    def ready_reviews(self, change) -> None:
        if self.pipeline.review() != 'approved':
            raise Blocked(msg('runtime_review_requested_changes'))

    def ready_threads(self, change) -> None:
        if not threads_resolved(change.number):
            raise Blocked(msg('runtime_review_threads_are_unreadable_or_unresolved'))

    def ready_checks(self, change) -> None:
        if not self.pipeline.args.required_check:
            raise Blocked(msg('runtime_ready_requires_required_check_for_every_mandatory_ci_context'))
        checks = head_checks(change.head_sha)
        for name in self.pipeline.args.required_check:
            if name not in checks or checks[name].state == 'pending':
                raise Pending(msg('runtime_missing_or_unsuccessful_required_check') + name)
            if checks[name].state != 'success':
                raise Blocked(msg('runtime_missing_or_unsuccessful_required_check') + name)
            if protected_status(name):
                login = self.pipeline.args.reviewer or forge.login()
                if getattr(checks[name], 'kind', '') != 'status' or getattr(checks[name], 'author', '') != login:
                    raise Blocked(msg('runtime_required_protected_status_has_an_unexpected_publisher') + name)
                if name == 'pipeline/ready' and (not self.ready_provenance(change, checks[name])):
                    raise Blocked(msg('runtime_current_protected_process_readiness_does_not_match_the_review_rou'))
            elif getattr(checks[name], 'kind', '') != 'job':
                raise Blocked(msg('runtime_required_ci_job_is_missing_a_same_name_status_is_insufficient') + name)
        args = self.pipeline.args
        state = checks_state(checks, lambda name: name == args.publish_context or (
            name in args.ignore_check and name not in args.required_check))
        if state == 'failure':
            raise Blocked(msg('runtime_a_pr_check_is_pending_or_failed'))
        if state == 'pending':
            raise Pending(msg('runtime_a_pr_check_is_pending_or_failed'))

    def audit(self) -> int:
        if not forge.in_ci() or not forge.run_url():
            raise Blocked(msg('runtime_process_audit_provenance_requires_its_read_only_ci_job'))
        self.pipeline.require_clean()
        change = self.pipeline.change()
        snapshot = self.pipeline.snapshot()
        if snapshot['head'] != change.head_sha:
            raise Blocked(msg('runtime_the_checkout_differs_from_the_pr_head'))
        actual = {}
        for stage in self.pipeline.steps:
            for step in self.pipeline.processes.get(stage['process'], {}).get('steps', []):
                observed = self.pipeline.evidence(step)
                if observed:
                    actual[step['id']] = observed
        actual.update(change=change.number, branch=change.head)
        task_context = self.pipeline.task_context()
        self.pipeline.audit_evidence = actual
        self.pipeline.audit_evidence['taskContext'] = task_context
        self.pipeline.audit_evidence['checks'] = {name: {'state': check.state, 'url': check.url} for name, check in head_checks(change.head_sha).items()}
        audits = {}
        if self.pipeline.audit_processes:
            review_round.install_cli(process_config.ci_kind())
        for name in self.pipeline.audit_processes:
            definition = self.pipeline.processes.get(name)
            if not definition or not definition.get('steps'):
                raise Blocked(msg('runtime_missing_process_definition') + name)
            result = self.pipeline.agent(definition, {'id': name, 'processAudit': True}, 'pipeline-audit/' + name, True)
            if result['decision'] != 'approve':
                raise Blocked(msg('runtime_independent_audit_requested_changes') + name)
            if self.pipeline.snapshot() != snapshot:
                raise Blocked(msg('runtime_review_source_base_or_policy_changed_during_analysis'))
            result['log'] = Path(result['log']).name
            result['invocation']['log'] = Path(result['invocation']['log']).name
            audits[name] = result
        if self.pipeline.task_context() != task_context:
            raise Blocked(msg('runtime_review_source_base_or_policy_changed_during_analysis'))
        report = {'v': 1, 'kind': 'pipeline-audit', 'pipeline': self.pipeline.args.pipeline, 'change': change.number, 'snapshot': snapshot, 'run': forge.run_url(), 'external': actual, 'processes': audits, 'requiredChecks': sorted(set(self.pipeline.args.required_check)), 'taskContext': digest(task_context)}
        Path(self.pipeline.args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(msg('runtime_pipeline_run_independently_audited_current_process_outcomes_histo'))
        return 0

    def readiness_fingerprint(self, change, task_context: str) -> str:
        return review_evidence.readiness_fingerprint(change, task_context)

    def ready_provenance(self, change, proof, live_task: str | None = None) -> bool:
        if forge.name == 'gitlab':
            return True
        if forge.name != 'github':
            return False
        try:
            live_task = review_evidence.task_context(change.number) if live_task is None else live_task
            required = sorted({name for name in self.pipeline.args.required_check if not protected_status(name)})
            snapshot = {'head': change.head_sha, 'base': change.base_sha, 'policy': review_evidence.policy_hash(change.base_sha), 'task': hashlib.sha256(live_task.encode()).hexdigest()}
            composition, processes = base_definitions(change.base_sha)
            audited_processes = self.pipeline.supported_audit_processes(composition, processes)
            evidence = forge.review_run(change.number, proof.url, snapshot)
            if not any((job.get('name') == 'pipeline-readiness' and job.get('conclusion') == 'success' for job in evidence['jobs'])):
                return False
            files = forge.review_artifact(proof.url, 'review-pipeline-audit')
            report = json.loads(files.get('pipeline-audit.json', b''))
            audited = report.get('snapshot', {})
            if report.get('v') != 1 or report.get('kind') != 'pipeline-audit' or report.get('pipeline') != 'change' or (report.get('change') != change.number) or (report.get('run') != proof.url) or any((audited.get(key) != snapshot[key] for key in ('head', 'base', 'policy'))) or (audited.get('composition') != digest(composition)) or (audited.get('task') != digest([live_task, 'change', '', change.number, required])) or (report.get('taskContext') != digest(live_task)) or (report.get('requiredChecks') != required) or (proof.description != review_evidence.readiness_fingerprint(change, digest(live_task), policy=snapshot['policy'])) or (set(report.get('processes', {})) != set(audited_processes)):
                return False
            for name, audit in report['processes'].items():
                invocation = audit.get('invocation', {})
                if audit.get('runner') != processes[name].get('auditRunner') or audit.get('decision') != 'approve' or audit.get('snapshot') != audited or (invocation.get('exit') != 0):
                    return False
                for record in (audit, invocation):
                    path = record.get('log', '')
                    if path not in files or hashlib.sha256(files[path]).hexdigest() != record.get('sha256'):
                        return False
                result = review_evidence.evidence_result(files, audit['log'])
                text = result.get('result', '')
                matches = list(re.finditer('^\\s*VERDICT:\\s*approve\\s*$', text, re.M))
                if result.get('is_error') is not False or result.get('subtype') != 'success' or len(matches) != 1 or (not text[:matches[0].start()].strip()) or text[matches[0].end():].strip():
                    return False
            return True
        except (forge.ForgeError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
            print(msg('readiness_unchecked', error), file=sys.stderr)
            return False

    def ready(self) -> int:
        self.ready_inputs()
        change = self.pipeline.change()
        if self.reviewed() or not self.release_change(change):
            self.ready_proof(change)
        head, base = (change.head_sha, change.base_sha)
        if change.state != 'open':
            raise Blocked(msg('runtime_ready_requires_an_open_pr'))
        if change.draft:
            forge.mark_ready(change.number)
        observed = self.pipeline.change()
        if observed.draft or observed.head_sha != head or observed.base_sha != base:
            raise Blocked(msg('runtime_head_base_changed_while_marking_ready'))
        return 0

    def ready_proof(self, change) -> None:
        checks = forge.commit_results(change.head_sha)
        proof = checks.get('pipeline/ready')
        contexts = [checks.get(review_verdict.context(entry.get('id', ''))) for entry in review_policy.passes()]
        login = self.pipeline.args.reviewer or (forge.login() if forge.in_ci() else '')
        if proof is None or proof.state != 'success' or (not proof.url) or (getattr(proof, 'author', '') != login) or (getattr(proof, 'kind', '') != 'status') or (getattr(proof, 'description', '') != self.readiness_fingerprint(change, digest(live_task := review_evidence.task_context(change.number)))) or any((check is None or check.url != proof.url or getattr(check, 'author', '') != login for check in contexts)) or (not self.ready_provenance(change, proof, live_task)):
            raise Blocked(msg('runtime_current_protected_process_readiness_does_not_match_the_review_rou'))

    def check_ready(self) -> int:
        target = None
        try:
            change = self.pipeline.change()
            target = review_verdict.Target(change.number, change.head_sha, forge.run_url(), change.base_sha, review_evidence.policy_hash(change.base_sha), review_evidence.task_hash(change.number))
            if not review_verdict.trusted_publisher(target):
                raise Blocked(msg('runtime_readiness_publication_requires_the_protected_ci_publisher_job'))
            report = json.loads(Path(self.pipeline.args.report).read_text(encoding='utf-8'))
            snapshot = report.get('snapshot', {})
            if report.get('v') != 1 or report.get('kind') != 'pipeline-audit' or report.get('pipeline') != self.pipeline.args.pipeline or (report.get('change') != change.number) or (report.get('run') != forge.run_url()) or (snapshot.get('head') != change.head_sha) or (snapshot.get('base') != change.base_sha) or (snapshot.get('policy') != target.policy) or (snapshot.get('composition') != digest(self.pipeline.composition)) or (snapshot.get('task') != self.pipeline.context) or (report.get('taskContext') != digest(self.pipeline.task_context())) or (report.get('requiredChecks') != sorted(set(self.pipeline.args.required_check))):
                raise Blocked(msg('runtime_process_audit_artifact_is_stale_incomplete_or_from_another_ci_run'))
            audits = report.get('processes', {})
            if set(audits) != set(self.pipeline.audit_processes):
                raise Blocked(msg('runtime_a_mandatory_process_audit_is_missing'))
            for name, audit in audits.items():
                if audit.get('runner') != self.pipeline.processes.get(name, {}).get('auditRunner'):
                    raise Blocked(msg('runtime_process_audit_runner_differs_from_the_trusted_policy') + name)
                paths = (audit.get('log', ''), audit.get('invocation', {}).get('log', ''))
                if any((not path or Path(path).name != path or (self.pipeline.work / path).resolve().parent != self.pipeline.work.resolve() for path in paths)):
                    raise Blocked(msg('runtime_process_audit_artifact_path_is_invalid'))
                record = {'outcome': 'success', 'snapshot': snapshot, 'audit': {**audit, 'log': str(self.pipeline.work / paths[0]), 'invocation': {**audit['invocation'], 'log': str(self.pipeline.work / paths[1])}}}
                if not self.pipeline.valid(record, snapshot):
                    raise Blocked(msg('runtime_process_audit_command_result_evidence_is_invalid') + name)
            deadline = time.monotonic() + self.pipeline.args.wait_minutes * 60
            waiting = False
            while True:
                try:
                    if not review_verdict.current(target):
                        raise Blocked(msg('runtime_review_plan_is_stale_or_its_base_policy_is_missing'))
                    self.ready_inputs()
                    break
                except Pending:
                    if time.monotonic() >= deadline:
                        raise
                    if not waiting:
                        print(msg('runtime_pipeline_run_waiting_for_current_review_and_mandatory_ci_results', format(self.pipeline.args.wait_minutes, 'g')), flush=True)
                        waiting = True
                    time.sleep(min(15, max(0, deadline - time.monotonic())))
            if not review_verdict.current(target) or report.get('taskContext') != digest(self.pipeline.task_context()):
                raise Blocked(msg('runtime_review_plan_is_stale_or_its_base_policy_is_missing'))
            if self.pipeline.args.publish_context:
                forge.set_commit_state(target.head, self.pipeline.args.publish_context, 'success', self.readiness_fingerprint(change, report['taskContext']), target.url)
                observed = forge.commit_results(target.head).get(self.pipeline.args.publish_context)
                if not observed or observed.state != 'success' or observed.url != target.url or (observed.author != forge.login()) or (observed.description != self.readiness_fingerprint(change, report['taskContext'])):
                    raise Blocked(msg('runtime_protected_readiness_status_was_not_observed_after_publication'))
            return 0
        except Exception:
            if target and self.pipeline.args.publish_context and review_verdict.trusted_publisher(target):
                forge.set_commit_state(target.head, self.pipeline.args.publish_context, 'failure', msg('runtime_ready_evidence_missing'), target.url)
            raise
