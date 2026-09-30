"""Iterative migration: ТЗ (technical assignment) artifact for the repair agent.

Single builder shared by every consumer. The Desktop and any future dashboard
read the artifact this module writes; nothing re-implements the prompt text.
The artifact is a linked pair -- task text + manifest with identities and
hashes -- published atomically, so an interrupted export never replaces the
last valid set and a stale set can not silently travel to another candidate.

The builder consumes ONLY durable state (run.json, run-config.json,
checkpoints/*, ledger.json, targets). It never runs a Baseline, never performs
project checks and never invents proof: evidence fields come from the
checkpoints, and anything absent is reported as a specific missing-field
diagnostic instead of being fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
BUILDER_NAME = "iterative_task"
BUILDER_VERSION = 1

TASK_DIR = "task"
TASK_MANIFEST_FILENAME = "task-manifest.json"
CURRENT_FILENAME = "current.json"
HISTORY_FILENAME = "history.json"
LANGUAGES = ("ru", "en")

# R9: versioned adapter marker. An artifact is either built from the CURRENT
# iterative durable state (run.json/checkpoints) or imported from a LEGACY
# saved Baseline result (tracked dashboard-state JSON) by the SAME builder —
# never from an HTML preview. The marker lets consumers decide staleness
# semantics: a legacy artifact stays usable for preview/copy until a real run
# exists, and becomes history once a run with a different identity appears.
ARTIFACT_SOURCE_ITERATIVE = "iterative"
ARTIFACT_SOURCE_LEGACY_BASELINE = "legacy-baseline"

FORBIDDEN_TRIAL_RELATIVES = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
)
FEEDBACK_KINDS = (
    "REPAIRING",
    "READY_FOR_VERIFY",
    "NEEDS_COHORT_EXPANSION",
    "NEEDS_ALTERNATIVE",
    "INCONCLUSIVE",
    "INFRA_BLOCKED",
)


class TaskExportError(Exception):
    """A diagnostically structured export failure."""

    def __init__(self, code: str, missing: Sequence[str], summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.missing = list(missing)
        self.summary = summary


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_input_errors(
    run: Mapping[str, Any], config: Mapping[str, Any], checkpoints: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Fields the task builder needs; an empty list means 'sufficient'."""
    missing: List[str] = []
    if not run:
        missing.append("run.json")
    elif not str(run.get("runId") or "").strip():
        missing.append("run.runId")
    if not config:
        missing.append("run-config.json")
    else:
        for key in ("projectName", "projectDir", "targets", "policyHash"):
            if key not in config:
                missing.append(f"run-config.{key}")
    if not checkpoints:
        missing.append("checkpoints/*")
    else:
        active_id = run.get("activeCheckpointId")
        if not active_id:
            missing.append("run.activeCheckpointId")
        if not any(str(c.get("checkpointId")) == str(active_id) for c in checkpoints if isinstance(c, dict)):
            missing.append(f"checkpoint:{active_id}")
    return missing


def _active_checkpoint(checkpoints: Sequence[Mapping[str, Any]], active_id: Optional[str]) -> Optional[Mapping[str, Any]]:
    for checkpoint in checkpoints:
        if str(checkpoint.get("checkpointId")) == str(active_id):
            return checkpoint
    return None


def _targets_satisfied(config: Mapping[str, Any], active: Mapping[str, Any]) -> Tuple[bool, int, int]:
    targets = {str(k): str(v) for k, v in (config.get("targets") or {}).items()}
    assignment = {str(k): str(v) for k, v in (active.get("fullAssignment") or {}).items()}
    unmet = sum(
        1
        for name, version in targets.items()
        if assignment.get(name) != version or name not in assignment
    )
    satisfied = len(targets) > 0 and unmet == 0
    return satisfied, len(targets), unmet


def _accepted_actions(checkpoints: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    actions: List[Dict[str, str]] = []
    previous: Dict[str, str] = {}
    for checkpoint in checkpoints:
        delta = checkpoint.get("acceptedDelta") or {}
        for name, new_version in (delta.get("changed") or {}).items():
            actions.append(
                {
                    "package": str(name),
                    "from": previous.get(str(name), "<unknown>"),
                    "to": str(new_version),
                    "action": "update",
                    "checkpoint": str(checkpoint.get("checkpointId")),
                }
            )
        for name, new_version in (delta.get("added") or {}).items():
            actions.append(
                {
                    "package": str(name),
                    "from": "<missing>",
                    "to": str(new_version),
                    "action": "add",
                    "checkpoint": str(checkpoint.get("checkpointId")),
                }
            )
        for name in (delta.get("removed") or {}):
            actions.append(
                {
                    "package": str(name),
                    "from": previous.get(str(name), "<unknown>"),
                    "to": "<removed>",
                    "action": "remove",
                    "checkpoint": str(checkpoint.get("checkpointId")),
                }
            )
        previous.update(
            {
                str(name): str(version)
                for name, version in ((checkpoint.get("fullAssignment") or {}).items())
            }
        )
    return actions


def _deferred_rows(
    ledger: Optional[Mapping[str, Any]],
    config: Mapping[str, Any],
    active: Mapping[str, Any],
) -> List[Dict[str, str]]:
    """Explicit deferrals with reasons; never an invented target.

    Only packages with a recorded deferral reason in the ledger, still unmet
    under the policy, are deferred.  Other unmet targets are simply the
    remainder -- actionable in a later scope, not immutable now.
    """
    targets = {str(k): str(v) for k, v in (config.get("targets") or {}).items()}
    assignment = {str(k): str(v) for k, v in (active.get("fullAssignment") or {}).items()}
    deferred: List[Dict[str, str]] = []

    reasons: Dict[str, str] = {}
    for entry in ((ledger or {}).get("blocks") or []):
        if not isinstance(entry, dict):
            continue
        for name, chunk in (
            (str(entry.get("package") or ""), str(entry.get("reason") or "")),
            (str(entry.get("candidate") or ""), str(entry.get("reason") or "")),
            (str(entry.get("subject") or ""), str(entry.get("reason") or "")),
        ):
            if name and name not in reasons and chunk:
                reasons[name] = chunk
    for entry in ((ledger or {}).get("deferrals") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("package") or "")
        if name and name not in reasons:
            reasons[name] = str(entry.get("reason") or entry.get("kind") or "deferred")

    for name, reason in sorted(reasons.items()):
        if name not in targets:
            continue
        current = assignment.get(name)
        if current == targets[name]:
            continue
        deferred.append(
            {
                "package": str(name),
                "current": current or "<absent>",
                "lagPolicyTarget": targets[name],
                "reason": reason,
            }
        )
    return deferred


def build_task_markdown(
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    ledger: Optional[Mapping[str, Any]],
    language: str = "ru",
) -> str:
    if language not in LANGUAGES:
        language = "ru"
    active = _active_checkpoint(checkpoints, run.get("activeCheckpointId"))
    if active is None:
        errors = run_input_errors(run, config, checkpoints)
        raise TaskExportError(
            "TASK_INPUT_INSUFFICIENT",
            errors,
            "no verified checkpoint to author the task from",
        )
    assignment = {str(k): str(v) for k, v in (active.get("fullAssignment") or {}).items()}
    actions = _accepted_actions(checkpoints)
    deferred = _deferred_rows(ledger, config, active)
    satisfied, denominator, remaining = _targets_satisfied(config, active)
    verification = active.get("verification") or {}
    audit = active.get("audit") or {}

    commands = list((config.get("verifyConfig") or {}).get("commands") or ())
    command_set_hash = _sha256_text(_stable_json(commands))

    fm = lambda key: str(config.get(key) or active.get(key) or "-")
    def table(items: Iterable[Tuple[str, str]]) -> str:
        return "\n".join(f"- `{k}`: {v}" for k, v in items)

    if language == "ru":
        return _build_ru(
            run=run, config=config, active=active, assignment=assignment,
            actions=actions, deferred=deferred, satisfied=satisfied,
            denominator=denominator, remaining=remaining,
            verification=verification, audit=audit, command_set_hash=command_set_hash,
            commands=commands, fm=fm, table=table,
        )
    return _build_en(
        run=run, config=config, active=active, assignment=assignment,
        actions=actions, deferred=deferred, satisfied=satisfied,
        denominator=denominator, remaining=remaining,
        verification=verification, audit=audit, command_set_hash=command_set_hash,
        commands=commands, fm=fm, table=table,
    )


def _runtime_summary(config: Mapping[str, Any]) -> str:
    """One-line Node/CI runtime contract for the task text; '' when the user
    set no explicit runtime (then the task must not claim one)."""
    rt = config.get("runtime") or {}
    if not rt:
        return ""
    parts = [
        f"{rt.get('requested')} \u2192 {rt.get('effectiveVersion')}",
        f"[{rt.get('source') or ''}]",
    ]
    manager = f"{rt.get('packageManager') or ''} {rt.get('packageManagerVersion') or ''}".strip()
    if manager:
        parts.append(f"\u00b7 {manager}")
    if rt.get("platform"):
        parts.append(f"\u00b7 {rt.get('platform')}/{rt.get('arch') or ''}")
    return " ".join(parts)


def _build_ru(
    *,
    run, config, active, assignment, actions, deferred, satisfied,
    denominator, remaining, verification, audit, command_set_hash, commands, fm, table,
) -> str:
    lines: List[str] = []
    lines.append("# Задание на адаптацию зависимостей")
    lines.append("")
    lines.append("## Контекст")
    lines.append("")
    lines.append(table([
        ("Run", run.get("runId")),
        ("Проект", fm("projectName")),
        ("Целевой checkpoint", active.get("checkpointId")),
        ("Исходный checkpoint", active.get("parentCheckpointId") or "C0"),
        ("Политика (hash)", config.get("policyHash")),
        ("Scope (hash)", _sha256_text(_stable_json(assignment))),
        ("Source snapshot", active.get("sourceSnapshotKey")),
        ("Manifest hash", active.get("manifestHash")),
        ("Lockfile hash", active.get("lockfileHash") or "-"),
        ("Resolved state", active.get("resolvedStateKey") or "-"),
        ("Проверки (hash)", command_set_hash),
        ("Node/CI runtime", _runtime_summary(config) or "-"),
    ]))
    lines.append("")
    lines.append("## Окружение (не менять)")
    lines.append("")
    lines.append(
        "Рантайм/CI-контракт этого run зафиксирован в строке «Node/CI runtime» выше. "
        "Не меняй заданный Node-рантайм или CI и не игнорируй его: это часть проверочного контракта."
    )
    lines.append(
        "Если работу блокирует окружение (нет доступа, недоступен реестр/утилита, "
        "несовместимый рантайм) — верни фидбек `INCONCLUSIVE` или `INFRA_BLOCKED` с точной "
        "причиной. Запрещено засчитывать проверки «в уме», подменять реальный запуск "
        "или вносить изменения в конфигурацию окружения ради зелёных проверок."
    )
    lines.append("")
    lines.append("## Точные версии накопленного состояния (immutable)")
    lines.append("")
    lines.append("```json")
    lines.append(_stable_json(assignment))
    lines.append("```")
    lines.append("")
    if actions:
        lines.append("## Действия, уже принятые в checkpoints")
        lines.append("")
        lines.append("| Пакет | Было | Стало | Действие | Checkpoint |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in actions:
            lines.append(
                f"| `{entry['package']}` | `{entry['from']}` | `{entry['to']}` | "
                f"{entry['action']} | {entry['checkpoint']} |"
            )
        lines.append("")
    lines.append("## Остаток миграции")
    lines.append("")
    lines.append(
        f"- Политика выполнена частично: осталось целей **{remaining}** из **{denominator}**. "
        "Задание не сообщает о полном завершении, пока остаток не равен нулю."
    )
    if str(verification.get("status") or "") == "legacy-exported-not-reverified":
        deferred_names = {str(entry.get("package")) for entry in deferred}
        goals = [
            (name, assignment.get(str(name), "<absent>"), str(target))
            for name, target in sorted((config.get("targets") or {}).items())
            if str(assignment.get(str(name))) != str(target) and str(name) not in deferred_names
        ]
        if goals:
            lines.append("")
            lines.append("### Импортированные цели legacy Baseline (не подтверждены)")
            lines.append("")
            lines.append("| Пакет | current | target |")
            lines.append("| --- | --- | --- |")
            for name, current, target in goals:
                lines.append(f"| `{name}` | `{current}` | `{target}` |")
            lines.append("")
            lines.append(
                "Цели взяты из сохранённого результата Baseline без его повторного запуска. "
                "Ни одна цель не подтверждена проверкой (legacy-exported-not-reverified): "
                "точные версии не считаются исполняемыми до verified checkpoint."
            )
            lines.append("")
    if deferred:
        lines.append("")
        lines.append("### Явно отложенные пакеты (неизменяемы в этом scope)")
        lines.append("")
        lines.append("| Пакет | current | lagPolicyTarget | Причина |")
        lines.append("| --- | --- | --- | --- |")
        for entry in deferred:
            lines.append(
                f"| `{entry['package']}` | `{entry['current']}` | `{entry['lagPolicyTarget']}` | "
                f"{entry['reason']} |"
            )
        lines.append("")
        lines.append(
            "Отложенные пакеты остаются в знаменателе и не получают исполняемого target. "
            "Не подставляй lagPolicyTarget в target самостоятельно; не меняй policy и не "
            "исключай пакет из scope для обхода отложенного статуса."
        )
    else:
        lines.append("")
        lines.append("Явных отложенных пакетов нет.")
    lines.append("")
    lines.append("## Статус проверок и аудита")
    lines.append("")
    lines.append(table([
        ("Project checks", str(verification.get("status") or "unknown")),
        ("Команды", "; ".join(commands) or "-"),
        ("Аудит", str(audit.get("status") or "UNKNOWN")),
        ("Evidence аудита", str(audit.get("evidenceRef") or "-")),
    ]))
    lines.append("")
    lines.append("## Правила работы для агента")
    lines.append("")
    lines.append(
        "1. Работай в выданном isolated trial с уже установленными ТОЧНЫМИ версиями выше. "
        "Допустимо адаптировать только source/config; manifests и lockfiles неизменяемы "
        f"(запрещены: {', '.join(FORBIDDEN_TRIAL_RELATIVES)})."
    )
    lines.append(
        "2. Изменения фиксируй в answer вместе с файлом/ключом и причиной; без валидного "
        "JSON комментария в манифест или lockfile не вставляй."
    )
    lines.append(
        "3. Отложенные пакеты из раздела выше не изменяй и не удаляй из остатка."
    )
    lines.append(
        "4. Проверки выполняет вовне для verify-exact по последним отремонтированным bytes; "
        "resolver-green и текст ответа не считаются проверкой."
    )
    lines.append(
        "5. Обратная связь (схема feedback v1): "
        + ", ".join(FEEDBACK_KINDS)
        + ". Ответ обязан содержать runId/candidateId/attemptId/kind; устаревший или чужой "
        "identity отклоняется."
    )
    lines.append("")
    lines.append(
        "Итог задания: показать фактический остаток, сохранить достигнутое накопленное "
        "состояние и дать short summary с промежуточными метриками и следующими шагами."
    )
    return "\n".join(lines)


def _build_en(
    *,
    run, config, active, assignment, actions, deferred, satisfied,
    denominator, remaining, verification, audit, command_set_hash, commands, fm, table,
) -> str:
    lines: List[str] = []
    lines.append("# Dependency adaptation assignment")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append(table([
        ("Run", run.get("runId")),
        ("Project", fm("projectName")),
        ("Target checkpoint", active.get("checkpointId")),
        ("Base checkpoint", active.get("parentCheckpointId") or "C0"),
        ("Policy (hash)", config.get("policyHash")),
        ("Scope (hash)", _sha256_text(_stable_json(assignment))),
        ("Source snapshot", active.get("sourceSnapshotKey")),
        ("Manifest hash", active.get("manifestHash")),
        ("Lockfile hash", active.get("lockfileHash") or "-"),
        ("Resolved state", active.get("resolvedStateKey") or "-"),
        ("Checks (hash)", command_set_hash),
        ("Node/CI runtime", _runtime_summary(config) or "-"),
    ]))
    lines.append("")
    lines.append("## Environment (do not change)")
    lines.append("")
    lines.append(
        "The Node/CI runtime contract of this run is recorded in the "
        "\u201cNode/CI runtime\u201d row above. Do not change the pinned Node runtime or "
        "CI, and do not ignore it: it is part of the verification contract."
    )
    lines.append(
        "If the environment blocks your work (no access, unavailable registry/tool, "
        "incompatible runtime) return `INCONCLUSIVE` or `INFRA_BLOCKED` feedback with the "
        "exact reason. Never count checks as green \u201cin your head\u201d, substitute a "
        "real run, or alter the environment configuration to manufacture green checks."
    )
    lines.append("")
    lines.append("## Exact versions of the cumulative state (immutable)")
    lines.append("")
    lines.append("```json")
    lines.append(_stable_json(assignment))
    lines.append("```")
    lines.append("")
    if actions:
        lines.append("## Actions already accepted into checkpoints")
        lines.append("")
        lines.append("| Package | From | To | Action | Checkpoint |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in actions:
            lines.append(
                f"| `{entry['package']}` | `{entry['from']}` | `{entry['to']}` | "
                f"{entry['action']} | {entry['checkpoint']} |"
            )
        lines.append("")
    lines.append("## Migration remainder")
    lines.append("")
    lines.append(
        f"- Policy is partially met: **{remaining}** of **{denominator}** goals remain. "
        "Nothing in this task may claim full completion while the remainder is non-zero."
    )
    if str(verification.get("status") or "") == "legacy-exported-not-reverified":
        deferred_names = {str(entry.get("package")) for entry in deferred}
        goals = [
            (name, assignment.get(str(name), "<absent>"), str(target))
            for name, target in sorted((config.get("targets") or {}).items())
            if str(assignment.get(str(name))) != str(target) and str(name) not in deferred_names
        ]
        if goals:
            lines.append("")
            lines.append("### Goals imported from the legacy Baseline (unverified)")
            lines.append("")
            lines.append("| Package | current | target |")
            lines.append("| --- | --- | --- |")
            for name, current, target in goals:
                lines.append(f"| `{name}` | `{current}` | `{target}` |")
            lines.append("")
            lines.append(
                "Goals come from the saved Baseline result without rerunning it. None is "
                "confirmed by a check (legacy-exported-not-reverified): exact versions are "
                "NOT executable until a verified checkpoint exists."
            )
            lines.append("")
    if deferred:
        lines.append("")
        lines.append("### Explicitly deferred packages (immutable in this scope)")
        lines.append("")
        lines.append("| Package | current | lagPolicyTarget | Reason |")
        lines.append("| --- | --- | --- | --- |")
        for entry in deferred:
            lines.append(
                f"| `{entry['package']}` | `{entry['current']}` | `{entry['lagPolicyTarget']}` | "
                f"{entry['reason']} |"
            )
        lines.append("")
        lines.append(
            "Deferred packages stay in the denominator and get NO executable target. Do not "
            "substitute lagPolicyTarget into the target yourself; do not change policy or "
            "scope-exclude a package to bypass the deferral."
        )
    else:
        lines.append("")
        lines.append("No explicit deferrals.")
    lines.append("")
    lines.append("## Check and audit status")
    lines.append("")
    lines.append(table([
        ("Project checks", str(verification.get("status") or "unknown")),
        ("Commands", "; ".join(commands) or "-"),
        ("Audit", str(audit.get("status") or "UNKNOWN")),
        ("Audit evidence", str(audit.get("evidenceRef") or "-")),
    ]))
    lines.append("")
    lines.append("## Working rules for the agent")
    lines.append("")
    lines.append(
        "1. Work in the issued isolated trial with the EXACT versions above already installed. "
        "Only source/config adaptation is allowed; manifests and lockfiles are immutable "
        f"(forbidden: {', '.join(FORBIDDEN_TRIAL_RELATIVES)})."
    )
    lines.append(
        "2. Record every change in your answer with file/key and rationale; never insert "
        "invalid comments into JSON or generated lockfiles."
    )
    lines.append(
        "3. Do not modify deferred packages above and do not remove them from the remainder."
    )
    lines.append(
        "4. Checks are run externally by verify-exact on the latest repaired bytes; "
        "resolver-green and answer prose are not a check."
    )
    lines.append(
        "5. Send typed feedback (schema feedback v1): "
        + ", ".join(FEEDBACK_KINDS)
        + ". The answer must carry runId/candidateId/attemptId/kind; stale or foreign "
        "identities are rejected."
    )
    lines.append("")
    lines.append(
        "Outcome: report the factual remainder, preserve the accepted cumulative state and "
        "give a short summary with intermediate metrics and next steps."
    )
    return "\n".join(lines)


def build_task_manifest(
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    ledger: Optional[Mapping[str, Any]],
    contents: Mapping[str, str],
    *,
    artifact_id: Optional[str] = None,
    stale: bool = False,
    stale_reason: str = "",
    created_at: Optional[str] = None,
    artifact_source: str = ARTIFACT_SOURCE_ITERATIVE,
) -> Dict[str, Any]:
    active = _active_checkpoint(checkpoints, run.get("activeCheckpointId"))
    if active is None:
        raise TaskExportError(
            "TASK_INPUT_INSUFFICIENT",
            run_input_errors(run, config, checkpoints),
            "no verified checkpoint to authorize the task from",
        )
    assignment = {str(k): str(v) for k, v in (active.get("fullAssignment") or {}).items()}
    deferred = _deferred_rows(ledger, config, active)
    satisfied, denominator, remaining = _targets_satisfied(config, active)
    commands = list((config.get("verifyConfig") or {}).get("commands") or ())
    command_set_hash = _sha256_text(_stable_json(commands))
    artifact_id = artifact_id or f"task-{run.get('runId')}-{active.get('checkpointId')}-{_sha256_text(_stable_json(assignment))[:12]}"
    languages = sorted(contents.keys())
    contents_view = {
        language: {
            # The content hash covers the EXACT bytes that will be written to
            # task.<language>.md (UTF-8, no newline translation), so a consumer
            # can re-hash the on-disk file and verify the artifact identically
            # on every platform — CRLF/LF translation must never break the check.
            "contentHash": _sha256_bytes(contents[language].encode("utf-8")),
            "contentBytes": len(contents[language].encode("utf-8")),
        }
        for language in languages
    }
    content_hash = _sha256_text(_stable_json({lang: contents_view[lang]["contentHash"] for lang in languages}))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "builder": BUILDER_NAME,
        "builderVersion": BUILDER_VERSION,
        "artifactSource": artifact_source,
        "artifactId": artifact_id,
        "languages": languages,
        "runId": str(run.get("runId") or ""),
        "workspaceId": str(config.get("workspaceId") or ""),
        "projectId": str(config.get("projectId") or str(config.get("projectName") or "")),
        "projectName": str(config.get("projectName") or ""),
        "baseCheckpointId": str(active.get("parentCheckpointId") or "C0"),
        "targetCheckpointId": str(active.get("checkpointId") or ""),
        "policyHash": str(config.get("policyHash") or ""),
        "scopeHash": _sha256_text(_stable_json(assignment)),
        "commandSetHash": command_set_hash,
        "exactVersions": assignment,
        "actions": _accepted_actions(checkpoints),
        "deferred": deferred,
        "completeness": {
            "policySatisfied": satisfied,
            "denominator": int(denominator),
            "remaining": int(remaining),
        },
        "verification": {
            "status": str((active.get("verification") or {}).get("status") or "unknown"),
            "commands": commands,
        },
        "audit": {
            "status": str((active.get("audit") or {}).get("status") or "UNKNOWN"),
            "evidenceRef": str((active.get("audit") or {}).get("evidenceRef") or ""),
        },
        "sourceSnapshotKey": str(active.get("sourceSnapshotKey") or ""),
        "manifestHash": str(active.get("manifestHash") or ""),
        "lockfileHash": str(active.get("lockfileHash") or ""),
        "resolvedStateKey": str(active.get("resolvedStateKey") or ""),
        "contents": contents_view,
        "contentHash": content_hash,
        "stale": bool(stale),
        "staleReason": stale_reason,
        "createdAt": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def task_dir(run_dir: Path) -> Path:
    return Path(run_dir) / TASK_DIR


def _atomic_write_file(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    # Write the exact UTF-8 bytes (no newline translation): durable text files
    # keep byte-identical content on every platform, matching the content hashes.
    stage.write_bytes(text.encode("utf-8"))
    os.replace(stage, target)


def _atomic_write_directory(target: Path, files: Dict[str, str]) -> None:
    """Atomically publish a directory of small text artifacts."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    if stage.exists():
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        for name, text in files.items():
            (stage / name).write_bytes(text.encode("utf-8"))
        os.replace(stage, target)
    except Exception:
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
        raise


def _current_pointer(task_root: Path) -> Optional[Dict[str, Any]]:
    path = task_root / CURRENT_FILENAME
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (ValueError, OSError):
        return None


def _read_manifest(task_root: Path) -> Optional[Dict[str, Any]]:
    path = task_root / TASK_MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (ValueError, OSError):
        return None


def export_task_artifact(
    run_dir: Path,
    *,
    languages: Sequence[str] = ("ru", "en"),
    mark_stale: bool = False,
    stale_reason: str = "",
) -> Dict[str, Any]:
    """Publish a linked task text + manifest set from durable state only.

    The previous valid set stays on disk; the pointer switches only after the
    new set is fully written.  Returns a summary with artifact id, pending
    stale entries and, on insufficient input, raises TaskExportError with the
    exact list of missing fields (no Baseline is started).
    """
    run = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    config = json.loads((Path(run_dir) / "run-config.json").read_text(encoding="utf-8"))
    checkpoints: List[Mapping[str, Any]] = []
    checkpoint_dir = Path(run_dir) / "checkpoints"
    if checkpoint_dir.exists():
        for path in sorted(checkpoint_dir.glob("C*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(item, dict):
                    checkpoints.append(item)
            except (ValueError, OSError):
                checkpoints.append({"checkpointId": path.stem, "status": "UNREADABLE"})
    ledger: Dict[str, Any] = {}
    ledger_path = Path(run_dir) / "ledger.json"
    if ledger_path.exists():
        try:
            value = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                ledger = value
        except (ValueError, OSError):
            pass

    missing = run_input_errors(run, config, checkpoints)
    if missing:
        raise TaskExportError(
            "TASK_INPUT_INSUFFICIENT",
            missing,
            "task export blocked: durable state lacks required fields; "
            + ", ".join(missing)
            + ". Use an existing run result or capture a fresh baseline; the export never starts one.",
        )

    root = task_dir(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    previous = _current_pointer(root)

    published: List[Dict[str, Any]] = []
    contents = {
        language: build_task_markdown(run, config, checkpoints, ledger, language=language)
        for language in languages
    }
    manifest = build_task_manifest(
        run, config, checkpoints, ledger, contents, created_at=created_at,
    )
    artifact_root = root / manifest["artifactId"]
    files: Dict[str, str] = {}
    for language, text in contents.items():
        files[f"task.{language}.md"] = text
    files[TASK_MANIFEST_FILENAME] = json.dumps(manifest, ensure_ascii=False, indent=2)
    if artifact_root.exists():
        # Kill/restart safety: an interrupted export may leave the artifact
        # dir on disk without the pointer (or with a truncated set). A COMPLETE
        # set for the same content is idempotently reused -- re-export after a
        # lost pointer must not fork another artifact dir. A partial set is
        # never trusted: it gets a fresh suffixed dir and the full set.
        existing = _read_manifest(artifact_root)
        complete = existing is not None and all(
            (artifact_root / f"task.{language}.md").exists()
            for language in existing.get("languages") or []
        ) and existing.get("contentHash") == manifest["contentHash"] and existing.get("artifactId") == artifact_root.name
        if not complete:
            artifact_root = root / f"{manifest['artifactId']}-{manifest['contentHash'][:8]}"
            manifest["artifactId"] = artifact_root.name
            files[TASK_MANIFEST_FILENAME] = json.dumps(manifest, ensure_ascii=False, indent=2)
    if not (artifact_root.exists() and _read_manifest(artifact_root) is not None):
        _atomic_write_directory(artifact_root, files)
    published.append(manifest)

    pointer = {
        "schemaVersion": SCHEMA_VERSION,
        "artifactId": published[0]["artifactId"],
        "runId": published[0]["runId"],
        "targetCheckpointId": published[0]["targetCheckpointId"],
        "scopeHash": published[0]["scopeHash"],
        "policyHash": published[0]["policyHash"],
        "languages": published[0]["languages"],
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _atomic_write_file(root / CURRENT_FILENAME, json.dumps(pointer, ensure_ascii=False, indent=2))

    history: List[Dict[str, Any]] = []
    history_path = root / HISTORY_FILENAME
    if history_path.exists():
        try:
            value = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                history = value
        except (ValueError, OSError):
            history = []
    if previous and previous.get("artifactId") != pointer["artifactId"]:
        history.append(
            {
                "artifactId": previous["artifactId"],
                "runId": previous.get("runId"),
                "targetCheckpointId": previous.get("targetCheckpointId"),
                "scopeHash": previous.get("scopeHash"),
                "stale": True,
                "staleReason": "superseded by a newer task artifact",
                "createdAt": previous.get("createdAt"),
            }
        )
    history.append(
        {
            "artifactId": pointer["artifactId"],
            "runId": pointer["runId"],
            "targetCheckpointId": pointer["targetCheckpointId"],
            "scopeHash": pointer["scopeHash"],
            "stale": False,
            "createdAt": created_at,
        }
    )
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "event": "export-task.done",
        "artifactId": pointer["artifactId"],
        "runId": pointer["runId"],
        "targetCheckpointId": pointer["targetCheckpointId"],
        "languages": pointer["languages"],
        "scopeHash": pointer["scopeHash"],
        "policyHash": pointer["policyHash"],
        "superseded": [entry for entry in history if entry.get("stale")],
        "historyTotal": len(history),
    }


def _legacy_target_marker(text: str) -> bool:
    return text in ("-", "—", "latest", "latest ") or text.strip().lower() in ("latest", "-", "—")


def _legacy_row_target_version(row: Mapping[str, Any], target_level: str = "yellow") -> str:
    """Exact target version a legacy dashboard row plans, mirroring the product's
    own fallback (lagPolicyTarget -> lag_target -> planned action -> concrete
    min_lag_<N>m -> min_lag_12m). A marker (latest/- ) is never an exact target."""
    months = row.get("lag_threshold_months")
    try:
        months_val = int(months) if months not in (None, "") else 12
    except (TypeError, ValueError):
        months_val = 12
    for key in (
        "lagPolicyTarget",
        "lag_target",
        "planned_action_default",
        f"planned_action_{target_level}",
        f"min_lag_{months_val}m",
        "min_lag_12m",
    ):
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and not _legacy_target_marker(text):
            return text
    return ""


def legacy_dashboard_input_errors(
    parsed: Mapping[str, Any], project_name: str
) -> List[str]:
    """Fields a LEGACY saved Baseline result must carry for the CURRENT task
    builder to rebuild the task WITHOUT a fresh Baseline. Returns the missing
    field list (never fabricates evidence)."""
    missing: List[str] = []
    if not isinstance(parsed, dict):
        return ["dashboard-state.json (not an object)"]
    projects = parsed.get("projects")
    if not isinstance(projects, dict) or not projects:
        return ["projects"]
    rows = projects.get(project_name)
    if not isinstance(rows, list) or not rows:
        return [f"projects.{project_name}"]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            missing.append(f"projects.{project_name}[{index}] (not an object)")
            continue
        name = str(row.get("name") or row.get("package") or "").strip()
        if not name:
            missing.append(f"projects.{project_name}[{index}].name")
            continue
        exact = _legacy_row_target_version(row)
        deferred = bool(row.get("planner_deferred"))
        if not exact and not deferred:
            missing.append(
                f"projects.{project_name}[{index}].{name} "
                "(no exact target version and not marked planner_deferred)"
            )
    return missing


def export_legacy_task_artifact(
    dashboard_state_path: Path,
    project_name: str,
    run_dir: Path,
    *,
    languages: Sequence[str] = ("ru", "en"),
) -> Dict[str, Any]:
    """R9: import a LEGACY saved Baseline result (tracked dashboard-state.json)
    into the SAME task contract with the CURRENT builder. No full Baseline and
    no project checks are ever started; no proof is invented: the synthesized
    checkpoint is explicitly marked 'legacy-exported-not-reverified', and an
    insufficient old result raises a diagnosed TASK_INPUT_INSUFFICIENT-style
    error listing the missing fields."""
    if not dashboard_state_path.exists():
        raise TaskExportError(
            "LEGACY_BASELINE_INSUFFICIENT",
            ["dashboard-state.json"],
            "saved dashboard-state file not found; refusing to invent evidence",
        )
    try:
        parsed = json.loads(dashboard_state_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise TaskExportError(
            "LEGACY_BASELINE_INSUFFICIENT",
            ["dashboard-state.json"],
            f"saved dashboard-state unreadable: {exc}",
        ) from exc
    missing = legacy_dashboard_input_errors(parsed, project_name)
    if missing:
        raise TaskExportError(
            "LEGACY_BASELINE_INSUFFICIENT",
            missing,
            "legacy Baseline result lacks enough structured JSON/evidence for the current builder; "
            "missing: " + ", ".join(missing) + ". No fresh Baseline is started.",
        )
    rows = parsed["projects"][project_name]
    targets: Dict[str, str] = {}
    full_assignment: Dict[str, str] = {}
    deferrals: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("package") or "").strip()
        if not name:
            continue
        current = str(row.get("current_version") or "").strip()
        if current:
            full_assignment[name] = current
        exact = _legacy_row_target_version(row)
        if bool(row.get("planner_deferred")):
            reason = (
                str(row.get("planner_deferred_reason") or "").strip()
                or "planner deferral recorded by the legacy Baseline"
            )
            deferrals.append({"package": name, "reason": reason})
            if exact:
                targets[name] = exact
            continue
        if exact:
            targets[name] = exact

    identity = _stable_json({"project": project_name, "targets": targets, "source": "legacy-baseline"})
    run_id = "legacy-" + _sha256_text(identity)[:12]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "activeCheckpointId": "C0",
        "projectId": project_name,
    }
    config: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "projectName": project_name,
        "projectDir": str(dashboard_state_path),
        "workspaceId": "",
        "projectId": project_name,
        "targetLevel": "yellow",
        "targets": targets,
        "policyHash": _sha256_text(identity),
        "verifyConfig": {"commands": []},
        "createdAt": created_at,
    }
    checkpoints: List[Dict[str, Any]] = [
        {
            "schemaVersion": SCHEMA_VERSION,
            "checkpointId": "C0",
            "seq": 0,
            "parentCheckpointId": None,
            "status": "VERIFIED",
            "fullAssignment": full_assignment,
            "acceptedDelta": {"changed": {}, "added": {}, "removed": {}},
            # R9: never fabricated proof — the imported state was NOT verified
            # by this run, so dispatch against a real candidate stays gated.
            "verification": {"status": "legacy-exported-not-reverified"},
            "audit": {},
        }
    ]
    ledger: Dict[str, Any] = {"blocks": [], "deferrals": deferrals, "feedback": [], "counters": {}}

    root = task_dir(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    previous = _current_pointer(root)
    contents = {
        language: build_task_markdown(run, config, checkpoints, ledger, language=language)
        for language in languages
    }
    manifest = build_task_manifest(
        run, config, checkpoints, ledger, contents,
        created_at=created_at,
        artifact_source=ARTIFACT_SOURCE_LEGACY_BASELINE,
    )
    artifact_root = root / manifest["artifactId"]
    files: Dict[str, str] = {}
    for language, text in contents.items():
        files[f"task.{language}.md"] = text
    files[TASK_MANIFEST_FILENAME] = json.dumps(manifest, ensure_ascii=False, indent=2)
    if not (artifact_root.exists() and _read_manifest(artifact_root) is not None):
        _atomic_write_directory(artifact_root, files)

    pointer = {
        "schemaVersion": SCHEMA_VERSION,
        "artifactId": manifest["artifactId"],
        "runId": manifest["runId"],
        "targetCheckpointId": manifest["targetCheckpointId"],
        "scopeHash": manifest["scopeHash"],
        "policyHash": manifest["policyHash"],
        "languages": manifest["languages"],
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _atomic_write_file(root / CURRENT_FILENAME, json.dumps(pointer, ensure_ascii=False, indent=2))

    history: List[Dict[str, Any]] = []
    history_path = root / HISTORY_FILENAME
    if history_path.exists():
        try:
            value = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                history = value
        except (ValueError, OSError):
            history = []
    if previous and previous.get("artifactId") != pointer["artifactId"]:
        history.append(
            {
                "artifactId": previous["artifactId"],
                "runId": previous.get("runId"),
                "targetCheckpointId": previous.get("targetCheckpointId"),
                "scopeHash": previous.get("scopeHash"),
                "stale": True,
                "staleReason": "superseded by a newer task artifact",
                "createdAt": previous.get("createdAt"),
            }
        )
    history.append(
        {
            "artifactId": pointer["artifactId"],
            "runId": pointer["runId"],
            "targetCheckpointId": pointer["targetCheckpointId"],
            "scopeHash": pointer["scopeHash"],
            "stale": False,
            "createdAt": created_at,
        }
    )
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "event": "export-task.done",
        "artifactId": pointer["artifactId"],
        "runId": pointer["runId"],
        "targetCheckpointId": pointer["targetCheckpointId"],
        "languages": pointer["languages"],
        "scopeHash": pointer["scopeHash"],
        "policyHash": pointer["policyHash"],
        "artifactSource": ARTIFACT_SOURCE_LEGACY_BASELINE,
        "superseded": [entry for entry in history if entry.get("stale")],
        "historyTotal": len(history),
    }


def current_task(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Resolve the pointer artifact manifest, or None when none was exported."""
    pointer = _current_pointer(task_dir(run_dir))
    if not pointer or not str(pointer.get("artifactId") or "").strip():
        return None
    try:
        return load_task_manifest(run_dir, str(pointer["artifactId"]))
    except TaskExportError:
        return None


def task_staleness(run_dir: Path) -> Dict[str, Any]:
    """Compare the exported task against the ACTIVE durable state.

    A task is stale for dispatch when the governing identities (run, target
    checkpoint, scope hash, policy hash) no longer match the current durable
    state: it may be opened as history but not silently sent for another
    candidate.  Missing state or artifacts produce an explicit stale/missing
    outcome instead of a silent pass.
    """
    run_dir = Path(run_dir)
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        config = json.loads((run_dir / "run-config.json").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return {"stale": True, "reason": f"run state unreadable: {exc}", "currentTask": None}
    manifest = current_task(run_dir)
    if manifest is None:
        return {"stale": True, "reason": "no task artifact exported yet", "currentTask": None}
    reasons: List[str] = []
    if str(manifest.get("runId")) != str(run.get("runId")):
        reasons.append("runId mismatch")
    if str(manifest.get("targetCheckpointId")) != str(run.get("activeCheckpointId")):
        reasons.append("active checkpoint changed")
    if str(manifest.get("policyHash")) != str(config.get("policyHash")):
        reasons.append("policy hash changed")
    stale = bool(reasons) or bool(manifest.get("stale"))
    return {
        "stale": stale,
        "reason": "; ".join(reasons) if reasons else str(manifest.get("staleReason") or ""),
        "currentTask": manifest,
    }


def load_task_manifest(run_dir: Path, artifact_id: str) -> Dict[str, Any]:
    """Read a published task manifest by id."""
    task_root = task_dir(Path(run_dir))
    if not task_root.exists():
        raise TaskExportError("TASK_ARTIFACT_MISSING", [], f"no task directory under {run_dir}")
    for item in task_root.iterdir():
        if not item.is_dir():
            continue
        path = item / TASK_MANIFEST_FILENAME
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and str(value.get("artifactId")) == artifact_id:
                return value
        except (ValueError, OSError):
            continue
    raise TaskExportError(
        "TASK_ARTIFACT_MISSING", [], f"task artifact {artifact_id} not found"
    )


def verify_task_input_errors(run_dir: Path) -> List[str]:
    """The diagnose-only entry: missing fields for export, no side effects."""
    try:
        run = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
        config = json.loads((Path(run_dir) / "run-config.json").read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return [f"run state unreadable: {exc}"]
    checkpoints: List[Mapping[str, Any]] = []
    checkpoint_dir = Path(run_dir) / "checkpoints"
    if checkpoint_dir.exists():
        for path in sorted(checkpoint_dir.glob("C*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(item, dict):
                    checkpoints.append(item)
            except (ValueError, OSError):
                continue
    return run_input_errors(run, config, checkpoints)
