"""Recovery checkpoints for mutable diagnostic trials; never acceptance proof."""
from pathlib import Path
import os

from artifact_integrity import build_artifact_tree_integrity
from substrate_identity import tool_build_id
from verification_stage_checkpoint import StageJournal, digest, command_tool_closure


def operation_environment(config):
    environment = dict(os.environ)
    runtime = config.get("runtime") or {}
    if runtime.get("nodePath"):
        from project_runtime import runtime_env_for_path
        environment.update(runtime_env_for_path(str(runtime["nodePath"])))
    environment["CI"] = "1"
    environment.setdefault("DEPLOOM_SOURCE_SCOPE_MODE", config.get("sourceScope") or "repository")
    return environment


def operation_journal(run_dir, run, config, candidate, operation):
    base_path = Path(run_dir) / 'checkpoints' / (str(candidate['baseCheckpointId']) + '.json')
    import json
    base = json.loads(base_path.read_text(encoding='utf-8-sig')) if base_path.exists() else {}
    context = {'runId': run['runId'], 'candidateId': candidate['candidateId'],
        'attemptId': candidate.get('attemptId', 0), 'baseSourceKey': base.get('sourceSnapshotKey'),
        'baseCheckpointId': candidate['baseCheckpointId'], 'assignment': candidate['fullAssignment'],
        'runtime': config.get('runtime'), 'policyHash': config.get('policyHash'),
        'verifyConfig': config.get('verifyConfig'), 'toolBuildId': tool_build_id(),
        'environmentHash': digest(operation_environment(config)), 'operation': operation,
        'runtimeClosure': command_tool_closure('node', Path(config['projectDir']), operation_environment(config))}
    return StageJournal(Path(run_dir) / 'operation-checkpoints', context)


def negative_evidence_scope(run_dir, config, checkpoint_id):
    """Compatibility history is reusable only under the same proof inputs."""
    import json
    base = json.loads((Path(run_dir) / 'checkpoints' / (checkpoint_id + '.json')).read_text(encoding='utf-8-sig'))
    environment = operation_environment(config)
    closure = command_tool_closure('node', Path(config['projectDir']), environment)
    if not closure or not base.get('sourceSnapshotKey'):
        return None
    return digest({'baseCheckpointId': checkpoint_id, 'sourceSnapshotKey': base['sourceSnapshotKey'],
        'configHash': digest(config), 'toolBuildId': tool_build_id(),
        'environmentHash': digest(environment), 'runtimeClosure': closure})


def trial_key(root, progress=None):
    return build_artifact_tree_integrity(Path(root), progress=progress,
        progress_label='migration trial checkpoint integrity').key


def completed_materialization(journal, root, assignment, observed, progress=None):
    row = journal.pass_record('materialize')
    if not row:
        return None
    evidence = row.get('evidence') or {}
    if evidence.get('workspaceRoot') != str(Path(root).resolve()) or not Path(root).is_dir():
        return None
    if trial_key(root, progress) != evidence.get('artifactKey'):
        return None
    state = evidence.get('candidate')
    if not isinstance(state, dict) or state.get('stage') != 'MATERIALIZED' or state.get('fullAssignment') != assignment:
        return None
    if observed() != evidence.get('observed'):
        return None
    return state


class DiagnosticCommandSequence:
    """Resume only a complete prefix whose exact post-command trial survives."""
    def __init__(self, journal, root, project, commands, environment):
        self.journal = journal
        self.root = Path(root)
        self.project = Path(project)
        self.commands = list(commands)
        self.environment = dict(environment)

    def stage(self, index):
        return 'precheck-command-' + str(index)

    def closure(self, command):
        return command_tool_closure(command, self.project, self.environment, self.root)

    def resume_prefix(self, progress=None):
        current = trial_key(self.root, progress)
        previous_after = None
        matched = 0
        for index, command in enumerate(self.commands, 1):
            row = self.journal.pass_record(self.stage(index))
            evidence = row.get('evidence') if row else None
            if not isinstance(evidence, dict) or evidence.get('command') != command or evidence.get('exitCode') != 0:
                break
            closure = self.closure(command)
            if not closure or evidence.get('toolClosure') != closure or evidence.get('supervision') != 'guaranteed-tree':
                break
            if previous_after is not None and evidence.get('beforeKey') != previous_after:
                break
            previous_after = evidence.get('afterKey')
            if current == previous_after:
                matched = index
        return matched

    def begin(self, index, command, progress=None):
        before = trial_key(self.root, progress)
        self.journal.write(self.stage(index), 'RUNNING', {'command': command, 'beforeKey': before})
        return before

    def complete(self, index, command, before, result, progress=None):
        quality = str(getattr(getattr(result, 'supervision', None), 'quality', ''))
        closure = self.closure(command)
        quiescent = quality == 'guaranteed-tree' and int(getattr(getattr(result, 'supervision', None), 'descendants_remaining', 0) or 0) == 0
        status = 'PASS' if result.returncode == 0 and quiescent else 'FAIL' if result.returncode > 0 else 'UNKNOWN'
        after = trial_key(self.root, progress) if status == 'PASS' else ''
        self.journal.write(self.stage(index), status, {'command': command, 'exitCode': result.returncode,
            'beforeKey': before, 'afterKey': after, 'toolClosure': closure, 'supervision': quality,
            'output': str(result.stdout or result.stderr or '')[-131072:]})

class VerificationResultCheckpoint:
    """Persist concluded verification independently of its controller transition."""
    def __init__(self, journal, root, project, config, environment):
        self.journal, self.root, self.project = journal, Path(root), Path(project)
        self.config, self.environment = config, dict(environment)

    def closures(self):
        commands = self.config.get('commands') or []
        return [command_tool_closure(command, self.project, self.environment, self.root) for command in commands]

    def begin(self, progress=None):
        before = trial_key(self.root, progress)
        self.journal.write('verification-result', 'RUNNING', {'inputKey': before})
        return before

    def complete(self, before, result, progress=None):
        import dataclasses
        from baseline_constraint_verifier import BaselineVerifyResult
        if not isinstance(result, BaselineVerifyResult):
            # Test doubles/non-authoritative adapters cannot publish recovery evidence.
            self.journal.write('verification-result', 'UNKNOWN')
            return
        self.journal.write('verification-result', 'COMPLETED', {
            'inputKey': before, 'afterKey': trial_key(self.root, progress),
            'closures': self.closures(), 'result': dataclasses.asdict(result)})

    def lookup(self, proof_cache_dir, progress=None):
        from baseline_constraint_verifier import BaselineVerifyResult, BaselineProjectFailure
        from block_v_prepared_artifact import configure_prepared_artifact_store, load_prepared_artifact_record
        from verification_proof import VerificationProofStore
        row = self.journal.read('verification-result')
        if not row or row.get('status') != 'COMPLETED':
            return None
        evidence = row.get('evidence') or {}
        payload = evidence.get('result')
        if not isinstance(payload, dict) or payload.get('kind') not in {'passed', 'dependency', 'preparation', 'project'}:
            return None
        if evidence.get('inputKey') != evidence.get('afterKey') or trial_key(self.root, progress) != evidence.get('inputKey'):
            return None
        closures = self.closures()
        if evidence.get('closures') != closures or (self.config.get('commands') and not all(closures)):
            return None
        try:
            failures = tuple(BaselineProjectFailure(**item) for item in payload.get('project_failures', []))
            if any(item.exit_code < 0 or item.exit_code in {124, 137} for item in failures):
                return None
            if payload.get('ok'):
                key = payload.get('preparation_proof_key')
                if not key or not proof_cache_dir:
                    return None
                store = VerificationProofStore(Path(proof_cache_dir))
                if store.lookup_pass('preparation', key) is None:
                    return None
                configure_prepared_artifact_store(proof_cache_dir)
                prepared = load_prepared_artifact_record(key, self.project, progress=progress)
                if prepared is None:
                    return None
                if (prepared.get('observedResolvedHash') != payload.get('observed_resolved_hash')
                    or prepared.get('observedResolvedVersions') != payload.get('observed_resolved_versions')):
                    return None
                refs = payload.get('command_checkpoint_refs') or []
                if len(refs) != len(self.config.get('commands') or []):
                    return None
                from verification_stage_checkpoint import adapter_artifacts
                import json
                for command, reference in zip(self.config.get('commands') or [], refs):
                    receipt = json.loads(Path(reference).read_text(encoding='utf-8-sig'))
                    checksum = receipt.pop('digest', None)
                    if checksum != digest(receipt) or receipt.get('status') != 'PASS':
                        return None
                    if receipt.get('evidence', {}).get('adapterArtifacts', {}) != adapter_artifacts(command):
                        return None
            restored = dict(payload)
            restored['project_failures'] = failures
            restored['command_checkpoint_refs'] = tuple(payload.get('command_checkpoint_refs') or ())
            return BaselineVerifyResult(**restored)
        except (OSError, ValueError, TypeError, KeyError):
            return None
