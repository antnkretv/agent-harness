from __future__ import annotations

from session_results import native_result, session_result
import review_evidence
import process_config
import review_policy
import review_verdict
import session_cost
import session_log
import text_templates
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from ci_common import ROOT, bash, agent_run_command, change_env, forge, get, tracker, msg, named_scripts, log_tail
from task_workspace import Workspace
from manifest import get_record
from pipeline_state import Blocked, Pending, SourceTree, Journal, digest, file_hash, ordered, produced_by, MERGE_FACTS
from pipeline_readiness import CHECK_RUNS, Readiness


def workflow_gate(gate: str) -> bool:
    return gate.endswith(('.yml', '.yaml'))


@dataclass(frozen=True)
class Fact:
    observe: str = ''
    history: bool = False
    first: bool = False
    act: bool = False
    finish: bool = False
    evidence: str = ''


FACTS = {
    'branch': Fact('observe_claim', history=True, first=True, evidence='evidence_claim'),
    'merged-base': Fact('observe_base', history=True, first=True, evidence='observe_base'),
    'commits': Fact('observe_commits', history=True, evidence='observe_commits'),
    'simplification-checkpoint': Fact('observe_clean', history=True, first=True),
    'draft-pr': Fact('observe_draft', history=True, first=True),
    'release-pr': Fact('observe_release'),
    'ready-pr': Fact('observe_ready', act=True),
    'merge-authorization': Fact('observe_authorized', history=True),
    **{name: Fact('observe_merged', history=True) for name in MERGE_FACTS},
    'closed-issue': Fact('observe_closed', history=True, first=True),
    'retired-workspace': Fact(finish=True),
    'version-tag': Fact('observe_delivery'),
    'release-notice': Fact('observe_delivery'),
    'back-merge-pr': Fact('observe_delivery'),
}


def facts(step: dict) -> list[Fact]:
    return [FACTS[name] for name in step.get('produces', []) if name in FACTS]


class Pipeline:

    def __init__(self, args: argparse.Namespace, worktree: str):
        self.runtime = SourceTree(worktree)
        self.readiness = Readiness(self)
        self.args = args
        self.composition = get_record(args.pipeline, {}, file='pipelines.json')
        self.processes = process_config.processes()
        if not self.composition or not self.composition.get('steps'):
            raise Blocked(msg('runtime_unknown_or_empty_pipeline') + args.pipeline)
        if args.command in ('audit', 'check-ready', 'ready') and args.pipeline != 'change':
            raise Blocked(msg('runtime_stage_needs_an_external_executor') + args.pipeline)
        self.steps = ordered(self.composition['steps'])
        if args.command in ('audit', 'check-ready', 'ready'):
            self.audit_processes = self.supported_audit_processes()
        if not args.until:
            if args.pipeline != 'change':
                raise Blocked(msg('runtime_explicit_stop_required') + args.pipeline)
            ready = produced_by(self.steps, self.processes, 'ready-pr')
            if not ready:
                raise Blocked(msg('runtime_missing_process_definition') + 'ready-pr')
            args.until = ready[1]['id']
        targets = {entry['id'] for entry in self.steps}
        for entry in self.steps:
            targets.update((step['id'] for step in self.processes.get(entry['process'], {}).get('steps', [])))
            targets.update((entry['id'] + '/' + step['id'] for step in self.processes.get(entry['process'], {}).get('steps', [])))
        if args.until not in targets:
            raise Blocked(msg('runtime_missing_process_definition') + args.until)
        self.task = Path(args.task).read_text(encoding='utf-8').strip() if args.task else self.task_context()
        if not self.task:
            raise Blocked(msg('runtime_task_context_is_empty'))
        self.context = digest([self.task, args.pipeline, args.issue, args.change, sorted(args.required_check)])
        self.journal = None
        if args.command in ("audit", "check-ready", "ready"):
            if not args.change or (args.command != "ready" and not args.report):
                raise Blocked(msg("runtime_stateless_ci_verification_requires_report_and_change"))
            self.work = Path(args.report).resolve().parent if args.report else Path(tempfile.mkdtemp(prefix="pipeline-ready-"))
            self.work.mkdir(parents=True, exist_ok=True)
            self.state = {"v": 1, "context": self.context, "records": {}, "transitions": {}}
        else:
            self.journal = Journal(self.runtime, self.context)
            self.work = self.journal.directory
            self.state = self.journal.load()
        self.records = self.state["records"]

    def supported_audit_processes(self, composition: dict | None=None, processes: dict | None=None) -> tuple[str, ...]:
        steps = ordered(composition['steps']) if composition is not None else self.steps
        processes = self.processes if processes is None else processes
        references = [step['process'] for step in steps]
        audited = [name for name in references if processes.get(name, {}).get('audit')]
        draft = produced_by(steps, processes, 'draft-pr')
        ready = produced_by(steps, processes, 'ready-pr')
        if len(set(references)) != len(references) or references.count('review') != 1 or not audited or not draft:
            raise Blocked(msg('runtime_unsupported_audit_composition'))
        known_gates = ('check-all.py', 'check-commits.py', 'check-pr-issue-link.py')
        for name in [*audited, draft[0]['process']]:
            definition = processes.get(name, {})
            if not definition.get('steps'):
                raise Blocked(msg('runtime_missing_process_definition') + name)
            for step in definition['steps']:
                actor = step.get('actor', definition.get('actor', 'agent'))
                gates = step.get('gates', [])
                command = step.get('command') or []
                if (actor not in ('agent', 'command') or any(not any(named_scripts([gate], name) for name in known_gates) for gate in gates)
                        or actor == 'command' and (command[:1] != ['python3'] or len(command) != 2 or not named_scripts(command[1:], 'check-all.py'))):
                    raise Blocked(msg('runtime_unsupported_audit_composition') + ': ' + name + '/' + step['id'])
        if len(processes[draft[0]['process']]['steps']) != 1:
            raise Blocked(msg('runtime_unsupported_audit_composition') + ': ' + draft[0]['process'])
        if not ready or ready[1].get('requires') or ready[1].get('gates') or (ready[1].get('actor') != 'agent'):
            raise Blocked(msg('runtime_unsupported_audit_composition') + ': ' + (ready[0]['process'] + '/' + ready[1]['id'] if ready else 'ready-pr'))
        self.readiness.require_readers(ready[1])
        return tuple(audited)

    def task_context(self) -> str:
        if self.args.command not in ('audit', 'check-ready', 'ready') or not self.args.change:
            raise Blocked(msg('runtime_pipeline_execution_requires_task'))
        return review_evidence.task_context(self.args.change)

    def save(self) -> None:
        if self.journal is None:
            raise Blocked(msg("runtime_stateless_ci_verification_requires_report_and_change"))
        self.journal.save(self.state)

    def change(self):
        if not self.args.change:
            known = self.state.get('change', 0)
            candidates = [known] if known else forge.open_changes(self.runtime.git('branch', '--show-current'))
            if len(candidates) != 1:
                raise Blocked(msg('runtime_a_unique_pr_or_change_is_required_for_pr_assertions'))
            self.args.change = candidates[0]
            self.state['change'] = candidates[0]
            self.save()
        found = forge.change(self.args.change)
        if found is None or not getattr(found, 'base_sha', ''):
            raise Blocked(msg('runtime_pr_or_its_current_base_sha_is_unavailable'))
        return found

    def snapshot(self) -> dict:
        change = self.change() if self.args.change or self.state.get('change') else None
        base = getattr(change, 'base_sha', '') or forge.branch_head(self.args.base.removeprefix('origin/'))
        if not base or self.runtime.git('rev-parse', '--verify', base, check=False) != base:
            raise Blocked(msg('runtime_current_remote_base_is_unavailable_locally_fetch_it_before_contin'))
        return {'head': self.runtime.git('rev-parse', 'HEAD'), 'base': base, 'source': self.runtime.source(), 'policy': review_evidence.policy_hash(base), 'composition': digest(self.composition), 'task': self.context}

    def require_clean(self) -> None:
        if self.runtime.git('status', '--porcelain'):
            raise Blocked(msg('runtime_a_committed_clean_source_snapshot_is_required'))

    def review(self, request: bool=False) -> str:
        if self.args.command != 'check-ready':
            self.require_clean()
        change = self.change()
        if self.args.command != 'check-ready' and change.head_sha != self.runtime.git('rev-parse', 'HEAD'):
            raise Blocked(msg('runtime_the_checkout_differs_from_the_pr_head'))
        files = forge.change_files(change.number)
        if files is None:
            raise Blocked(msg('runtime_changed_files_are_unavailable'))
        login = self.args.reviewer or (forge.login() if forge.in_ci() else '')
        if not login:
            raise Blocked(msg('runtime_review_verification_requires_reviewer_with_the_protected_publishe'))
        task = review_evidence.task_hash(change.number)
        policy = review_evidence.policy_hash(change.base_sha)
        marks = []
        for item in reversed(forge.reviews(change.number)):
            mark = review_evidence.review_mark(change.number, item, review_policy.headline(item.body), item.body) if item.author == login else None
            if mark:
                marks.append(mark)
        marks.reverse()
        routes = review_policy.route(marks, list(files), change.head_sha, change.base_sha, policy, task,
                                    change.fetch_ref, change.base)
        if not routes:
            raise Blocked(msg('runtime_no_mandatory_review_passes_configured'))
        states = forge.commit_results(change.head_sha)
        missing = [route for route in routes if route.outcome not in ('approved', 'not-applicable')]
        if not missing:
            snapshot = {'head': change.head_sha, 'base': change.base_sha, 'policy': policy, 'task': task}
            carried = True
            for route in routes:
                check = states.get(review_verdict.context(route.name))
                if not check or check.state == 'pending':
                    carried = False
                elif check.state != 'success' or getattr(check, 'author', '') != login or (not review_evidence.trusted_status(change.number, check, snapshot, route.name, marks)):
                    raise Blocked(msg('runtime_missing_successful_trusted_review_check') + route.name)
            if carried:
                return 'approved'
        else:
            current_marks = [mark for mark in marks if mark.sha == change.head_sha and mark.base == change.base_sha and (mark.policy == policy) and (mark.task == task)]
            if any((review_policy.counts(current_marks, entry.get('marker', ''), change.head_sha, change.base_sha, policy, task)[1] == review_policy.BLOCK for entry in review_policy.passes())):
                return 'changes'
            if any((route.outcome == 'budget-exhausted' for route in missing)):
                raise Blocked(msg('runtime_review_budget_exhausted_without_a_current_approval'))
        waiting = {'head': change.head_sha, 'base': change.base_sha, 'policy': policy, 'task': task}
        if request and self.state.get('review-request') != waiting:
            forge.request_review(change.number)
            self.state['review-request'] = waiting
            self.save()
        raise Pending(msg('runtime_mandatory_review_results_are_pending_resume_after_the_trusted_rev'))

    def related_issue(self, branch: str, change=None) -> str:
        body_refs = review_evidence.body_references(change) if change else {}
        references = review_evidence.task_references(change, body_refs) if change else {}
        named = tracker.branch_issue(branch)
        issue = str(self.args.issue or named or "")
        if not issue and references:
            if len(references) != 1:
                raise Blocked(msg("runtime_ambiguous_task_issue"))
            issue = str(next(iter(references)))
        if named and issue != str(named):
            raise Blocked(msg("runtime_task_and_branch_registration_do_not_match"))
        if change and issue and issue not in body_refs:
            raise Blocked(msg("runtime_pr_does_not_reference_the_task_issue"))
        return issue

    def claim(self, branch: str, change=None, actor: bool = False) -> dict:
        key = self.related_issue(branch, change)
        if not key:
            if not branch or branch in ("HEAD", self.args.base.removeprefix("origin/")):
                raise Blocked(msg("runtime_a_direct_task_requires_its_context_and_a_feature_branch"))
            return {"task": self.context, "branch": branch}
        issue = tracker.issue(key)
        if not issue or not issue.open:
            raise Blocked(msg("runtime_task_and_branch_registration_do_not_match"))
        if not getattr(issue, "assignees", ()) or actor and tracker.login() not in issue.assignees:
            raise Blocked(msg("runtime_the_tracker_does_not_confirm_the_current_assignee"))
        named = str(tracker.branch_issue(branch) or "") == key
        if not named and branch not in getattr(tracker.issue_branches(key), "linked", {}):
            raise Blocked(msg("runtime_the_tracker_does_not_confirm_the_linked_branch"))
        return {"issue": issue.key, "branch": branch, "assignees": list(issue.assignees)}

    def external(self, step: dict) -> dict:
        found = {}
        for fact in facts(step):
            if fact.observe:
                found.update(getattr(self, fact.observe)())
        return found

    def evidence(self, step: dict) -> dict:
        found = {}
        for fact in facts(step):
            if fact.evidence:
                found.update(getattr(self, fact.evidence)())
        return found

    def observe_claim(self) -> dict:
        change = self.change() if self.args.change or self.state.get('change') else None
        return self.claim(self.runtime.git('branch', '--show-current'), change, actor=True)

    def evidence_claim(self) -> dict:
        change = self.change()
        return self.claim(change.head, change)

    def observe_base(self) -> dict:
        base = self.snapshot()['base']
        result = subprocess.run(['git', '-C', self.runtime.path, 'merge-base', '--is-ancestor', base, 'HEAD'], check=False)
        if result.returncode:
            raise Blocked(msg('runtime_current_base_has_not_been_merged_into_head'))
        return {'base': base}

    def observe_commits(self) -> dict:
        self.require_clean()
        if not self.runtime.git('rev-list', self.snapshot()['base'] + '..HEAD'):
            raise Blocked(msg('runtime_no_implementation_commits_relative_to_base'))
        return {'head': self.runtime.git('rev-parse', 'HEAD')}

    def observe_clean(self) -> dict:
        self.require_clean()
        return {'head': self.runtime.git('rev-parse', 'HEAD')}

    def observe_release(self, bind: bool = False) -> dict:
        self.require_clean()
        change = self.change()
        if change.state != 'open' or not change.draft or change.head_sha != self.runtime.git('rev-parse', 'HEAD'):
            raise Blocked(msg('runtime_the_forge_does_not_confirm_the_current_draft_pr'))
        self.claim(change.head, change)
        if bind:
            Workspace(self.runtime.path).bind(change.number)
        return {'change': change.number, 'head': change.head_sha}

    def observe_draft(self) -> dict:
        return self.observe_release(bind=True)

    def observe_ready(self) -> dict:
        self.readiness.ready_inputs()
        change = self.change()
        if change.draft:
            raise Blocked(msg('runtime_the_pr_is_still_draft'))
        return {'change': change.number, 'head': change.head_sha, 'base': change.base_sha}

    def observe_authorized(self) -> dict:
        self.readiness.ready_inputs()
        change = self.change()
        if change.state != 'open' or change.draft:
            raise Blocked(msg('runtime_before_merge_requires_the_current_open_ready_pr'))
        return {'change': change.number, 'head': change.head_sha, 'base': change.base_sha}

    def observe_merged(self) -> dict:
        change = self.change()
        if change.state != 'merged':
            raise Blocked(msg('runtime_merge_requires_a_separate_human_authorization_and_an_observed_mer'))
        return {'change': change.number, 'state': 'merged', 'head': change.head_sha}

    def observe_closed(self) -> dict:
        change = self.change()
        key = self.related_issue(change.head, change)
        issue = tracker.issue(key) if key else None
        closing = bool(key and review_evidence.task_references(change).get(key))
        if change.state != 'merged' or (key and (issue is None or (closing and issue.open))):
            raise Blocked(msg('runtime_merged_pr_and_any_linked_task_closure_have_not_both_been_observed'))
        return {'change': change.number, 'issue': issue.key if issue else '', 'closing': closing, 'open': issue.open if issue else None}

    def observe_delivery(self) -> dict:
        raise Blocked(msg('runtime_release_completion_requires_observed_release_workflow_and_back_me'))

    def based(self, argv: list[str]) -> list[str]:
        if any(named_scripts(argv[1:2], name) for name in ('check-all.py', 'check-commits.py')):
            return [*argv, '--base', self.args.base]
        return [*argv]

    def command(self, argv: list[str], key: str) -> dict:
        if not argv or not all((isinstance(item, str) and item for item in argv)):
            raise Blocked(msg('runtime_missing_executable_command') + key)
        path = self.work / (key.replace('/', '-') + '.log')
        env = dict(os.environ)
        if self.args.change:
            change = self.change()
            env.update(change_env(change))
        with path.open('w', encoding='utf-8') as handle:
            result = subprocess.run(argv, cwd=self.runtime.path, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            print(log_tail(path), end="", file=sys.stderr, flush=True)
            raise Blocked(msg('pipeline_run_command_failed', ' '.join(argv), path))
        return {'argv': argv, 'exit': result.returncode, 'log': str(path), 'sha256': file_hash(path)}

    def agent(self, definition: dict, step: dict, key: str, audit: bool) -> dict:
        reference = step.get('auditRunner' if audit else 'runner') or definition.get('auditRunner' if audit else 'runner')
        if not reference or not process_config.effort(str(reference)):
            raise Blocked(msg('runtime_missing_agent_runner') + str(reference))
        selected = process_config.selected_runner(str(reference))
        kind = selected['kind']
        registered = {item['id']: item['prompt'] for item in process_config.agent_prompts()}
        owners = definition.get('steps', []) if step.get('processAudit') else []
        prompts = [item['prompt'] for item in [*owners, step] if item.get('prompt')]
        missing = next((prompt for prompt in prompts if prompt not in registered), None)
        if missing:
            raise Blocked(msg('runtime_missing_registered_prompt') + missing)
        paths = list(dict.fromkeys(filter(None, [definition.get('doc', ''), step.get('doc', ''),
                                                 *(item.get('doc', '') for item in owners),
                                                 *(registered[prompt] for prompt in prompts)])))
        files = dict.fromkeys((path.partition('#')[0] for path in paths), 1)
        docs = [Path(ROOT, path).read_text(encoding='utf-8') if '#' not in path else text_templates.section(path)
                for path in paths if '#' not in path or path.partition('#')[0] not in paths]
        sections = ', '.join(paths)
        before = self.snapshot()
        text = text_templates.prompt('stage/audit' if audit else 'stage/execute', {
            'StageKey': key, 'StageSnapshot': json.dumps(before, sort_keys=True), 'StageTask': self.task,
            'StageSections': sections, 'StageInstructions': '\n\n'.join(docs)})
        if step.get('processAudit'):
            text += '\n' + text_templates.prompt('stage/process-audit', {
                'StageDefinition': json.dumps(definition, sort_keys=True),
                'StageObservations': json.dumps(self.audit_evidence, sort_keys=True, ensure_ascii=False)})
        path = self.work / (key.replace('/', '-') + ('.audit' if audit else '.agent'))
        prompt_file, log = (str(path) + '.prompt.md', str(path) + '.jsonl')
        Path(prompt_file).write_text(text, encoding='utf-8')
        allowed = get('processes.review.allowedTools', '', file='methodology.json') if audit else selected.get('allowedTools', '')
        disallowed = ','.join(filter(None, [selected.get('disallowedTools', ''), get('processes.review.disallowedTools', '', file='methodology.json') if audit else '']))
        argv = agent_run_command(kind, 'review' if audit else 'agent', selected['model'], selected.get('effort', ''), prompt_file, log, workdir=self.runtime.path, allowed=allowed, disallowed=disallowed, minutes=self.args.timeout_minutes,
                                detached=getattr(self.args, "unallocated", True), purpose='process-audit' if audit else '')
        Path(log).unlink(missing_ok=True)
        try:
            invocation = self.command(argv, key + ('-audit' if audit else '-agent'))
        finally:
            if self.journal is not None and Path(log).is_file():
                spent = session_cost.usage(session_result(log) or {})
                self.state['usage'] = session_cost.total([self.state.get('usage', {}), spent])
                items = session_log.journal(log)
                self.state['documents'] = session_log.merged(self.state.get('documents'), files,
                                                             session_log.reads(items, str(self.runtime.path), skills=True))
                self.state['model'] = session_log.model_of(native_result(items), {}) or selected['model']
                self.save()
        result = session_result(log)
        if not result or result.get('is_error'):
            raise Blocked(msg('runtime_agent_produced_no_successful_result') + key)
        decision = 'completed'
        if audit:
            if self.snapshot() != before:
                raise Blocked(msg('runtime_source_snapshot_changed_during_independent_audit') + key)
            text = result.get('result', '')
            matches = list(re.finditer('^\\s*VERDICT:\\s*(approve|changes|blocked)\\s*$', text, re.I | re.M))
            if len(matches) != 1 or not text[:matches[0].start()].strip():
                raise Blocked(msg('runtime_missing_structured_substantive_audit_verdict') + key)
            decision = matches[0].group(1).lower()
        return {'runner': reference, 'invocation': invocation, 'log': log, 'sha256': file_hash(log), 'decision': decision, 'snapshot': self.snapshot()}

    def valid(self, record: dict, snapshot: dict) -> bool:
        if record.get('outcome') != 'success' or record.get('snapshot') != snapshot:
            return False
        for command in record.get('commands', []):
            if command.get('exit') != 0 or not Path(command.get('log', '')).is_file():
                return False
            if file_hash(command['log']) != command.get('sha256'):
                return False
        audit = record.get('audit')
        if audit:
            invocation = audit.get('invocation', {})
            if audit.get('decision') != 'approve' or audit.get('snapshot') != snapshot or invocation.get('exit') != 0 or (not Path(invocation.get('log', '')).is_file()) or (file_hash(invocation['log']) != invocation.get('sha256')) or (not Path(audit.get('log', '')).is_file()) or (file_hash(audit['log']) != audit.get('sha256')):
                return False
            result = session_result(audit['log'])
            text = result.get('result', '') if result and (not result.get('is_error')) else ''
            if not re.search('(?m)^VERDICT: approve\\s*$', text) or not text.partition('VERDICT:')[0].strip():
                return False
        return bool(record.get('commands') or audit or record.get('external'))

    def observed_gates(self, step: dict) -> set:
        if not any(item.get('check') == CHECK_RUNS for item in step.get('readiness', [])):
            return set()
        return {gate for gate in step.get('gates', []) if workflow_gate(gate)}

    def run_step(self, definition: dict, step: dict, key: str) -> str:
        if step.get('session'):
            return 'success'
        actor = step.get('actor', definition.get('actor', 'agent'))
        observed_gates = self.observed_gates(step)
        if actor in ('human', 'pipeline') and (not self.external(step)):
            raise Blocked(msg('runtime_stage_needs_an_external_executor') + key)
        record = self.records.get(key, {})
        snapshot = self.snapshot()
        known = facts(step)
        acts = any(fact.act for fact in known)
        historical = any(fact.history for fact in known) or step.get('scope') == 'round'
        if historical and self.valid(record, record.get('snapshot', {})) and all((record.get('snapshot', {}).get(field) == snapshot[field] for field in ('task', 'policy', 'composition'))):
            observed = self.external(step)
            old_head = record.get('snapshot', {}).get('head', '')
            ancestry = subprocess.run(['git', '-C', self.runtime.path, 'merge-base', '--is-ancestor', old_head, snapshot['head']], capture_output=True, check=False) if old_head else None
            stable_transform = step.get('scope') == 'round' and record.get('snapshot', {}).get('source') == snapshot['source']
            if (observed or stable_transform) and ancestry is not None and (ancestry.returncode == 0):
                return 'success'
        origin, _, name = step.get('ref', '').partition('/')
        if origin and any(self.valid(self.records.get(stage['id'] + '/' + name, {}), snapshot)
                          for stage in self.steps if stage['process'] == origin):
            return 'success'
        actor = step.get('actor', definition.get('actor', 'agent'))
        expected = bool(record.get('audit')) if actor == 'agent' and not acts else True
        if actor == 'command':
            argv = self.based(step.get('command', []))
            expected = any(command.get('argv') == argv for command in record.get('commands', []))
        expected = expected and len(record.get('commands', [])) >= len(set(step.get('gates', [])) - observed_gates)
        if expected and self.valid(record, snapshot):
            if acts:
                self.external(step)
            return 'success'
        if self.args.command == 'verify':
            raise Blocked(msg('runtime_missing_or_stale_mandatory_stage_evidence') + key)
        record = {'outcome': 'running', 'input': snapshot, 'snapshot': snapshot, 'commands': []}
        self.records[key] = record
        self.save()
        actor = step.get('actor', definition.get('actor', 'agent'))
        if actor in ('human', 'pipeline'):
            record['external'] = self.external(step)
            if not record['external']:
                raise Blocked(msg('runtime_stage_needs_an_external_executor') + key)
        elif acts:
            self.readiness.ready_inputs()
            before = self.snapshot()
            self.readiness.ready()
            if self.snapshot() != before:
                raise Blocked(msg('runtime_head_base_changed_while_marking_ready'))
            record['external'] = self.external(step)
        elif actor == 'command':
            before = self.snapshot()
            record['commands'].append(self.command(self.based(step.get('command', [])), key))
            if self.snapshot() != before:
                raise Blocked(msg('runtime_command_changed_the_verified_source_snapshot') + key)
        elif actor == 'agent':
            if any(fact.first for fact in known):
                try:
                    record['external'] = self.external(step)
                except Blocked:
                    record['agent'] = self.agent(definition, step, key, False)
            else:
                record['agent'] = self.agent(definition, step, key, False)
            record['external'] = self.external(step)
            record['audit'] = self.agent(definition, step, key, True)
            if record['audit']['decision'] != 'approve':
                record['outcome'] = record['audit']['decision']
                self.save()
                return record['outcome']
        else:
            raise Blocked(msg('runtime_stage_needs_an_external_executor') + key)
        for gate in step.get('gates', []):
            if gate in step.get('command', []) or gate in observed_gates:
                continue
            if workflow_gate(gate):
                raise Blocked(msg('runtime_workflow_completion_must_be_observed_externally') + gate)
            argv = self.based([sys.executable, gate] if gate.endswith('.py') else [bash(), gate])
            before = self.snapshot()
            record['commands'].append(self.command(argv, key + '-gate-' + Path(gate).stem))
            if self.snapshot() != before:
                raise Blocked(msg('runtime_a_gate_changed_the_verified_snapshot') + gate)
        record['snapshot'] = self.snapshot()
        record['outcome'] = 'success'
        self.save()
        return 'success'

    def run_stage(self, stage: dict, definition: dict, skipped: tuple = ()) -> tuple[str, bool]:
        for step in ordered(definition.get('steps', [])):
            if step['id'] in skipped:
                continue
            key = stage['id'] + '/' + step['id']
            outcome = self.run_step(definition, step, key)
            if outcome != 'success':
                return outcome, False
            if any(fact.finish for fact in facts(step)):
                Workspace(self.runtime.path).finish(self.change().number)
            if self.args.until in (key, step['id']):
                print(msg('pipeline_run_verified', key))
                return 'success', True
        return 'success', False

    def run(self) -> int:
        if self.args.change and self.change().state == 'merged':
            stage, step = self.readiness.ready_step()
            index = self.steps.index(stage)
            ready = self.records.get(stage['id'] + '/' + step['id'], {})
            if not self.valid(ready, ready.get('snapshot', {})):
                raise Blocked(msg('runtime_merged_pr_has_no_prior_verified_ready_record_for_this_task_no_act'))
            if ready.get('external', {}).get('head') != self.change().head_sha:
                raise Blocked(msg('runtime_merged_head_differs_from_the_previously_verified_ready_head'))
            merged = next(position for position, item in enumerate(self.processes[stage['process']]['steps'])
                          if any(fact.observe == 'observe_merged' for fact in facts(item)))
            before = tuple(item['id'] for item in self.processes[stage['process']]['steps'][:merged])
            for entry in self.steps[index:]:
                skipped = before if entry is stage else ()
                outcome, reached = self.run_stage(entry, self.processes.get(entry['process'], {}), skipped)
                if outcome != 'success':
                    raise Blocked(msg('runtime_post_merge_completion_is_blocked'))
                if reached or self.args.until == entry['id']:
                    return 0
            return 0
        completed, cursor = (set(), 0)
        while cursor < len(self.steps):
            stage = self.steps[cursor]
            name = stage['id']
            if not set(stage.get('requires', [])).issubset(completed):
                raise Blocked(msg('runtime_incomplete_process_dependencies') + name)
            definition = self.processes.get(stage['process'])
            if not definition or not definition.get('steps'):
                raise Blocked(msg('runtime_missing_process_definition') + stage['process'])
            print('pipeline-run: ' + name, flush=True)
            if stage['process'] == 'review':
                outcome = self.review(request=self.args.command == 'run')
            else:
                outcome, reached = self.run_stage(stage, definition)
                if reached:
                    return 0
            if outcome == 'changes':
                transition = stage.get('transitions', {}).get('changes')
                if not transition or self.args.command == 'verify':
                    raise Blocked(msg('runtime_changes_require_implementation') + name)
                visits = self.state['transitions'].get(name, 0) + 1
                if visits >= transition.get('maxVisits', 0):
                    raise Blocked(msg('runtime_bounded_changes_budget_exhausted'))
                self.state['transitions'][name] = visits
                target = transition['target']
                cursor = next((index for index, entry in enumerate(self.steps) if entry['id'] == target))
                affected = {entry['id'] for entry in self.steps[cursor:]}
                self.state['records'] = {key: value for key, value in self.records.items() if key.partition('/')[0] not in affected}
                self.records = self.state['records']
                completed -= affected
                self.save()
                continue
            if outcome not in ('success', 'approved'):
                raise Blocked(msg('runtime_process_did_not_approve') + name + ': ' + outcome)
            completed.add(name)
            if self.args.until == name:
                return 0
            cursor += 1
        return 0
