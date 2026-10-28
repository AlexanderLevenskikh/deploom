"""Versioned planning intent shared by headless Draft and its agent prompt.

These bounds authorize candidate selection, never execution or a proof of runtime
compatibility. Legacy callers retain pinned targets and the health-policy goal.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

DECISION_CONTRACT_VERSION = 1
MINIMUM_CHANGE = "minimum-change-to-policy"
LATEST_APPROVED = "latest-compatible-in-approved-lines"


def normalize_decision_contract(intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = intent or {}
    target_mode = value.get("targetMode", "pinned")
    objective = value.get("objective", MINIMUM_CHANGE)
    if target_mode not in ("pinned", "advisory"):
        raise ValueError("DECISION_CONTRACT_INVALID: targetMode must be pinned or advisory")
    if objective not in (MINIMUM_CHANGE, LATEST_APPROVED):
        raise ValueError("DECISION_CONTRACT_INVALID: unsupported objective")
    mutable = value.get("mutablePackages")
    if mutable is not None:
        if not isinstance(mutable, list) or any(not isinstance(n, str) or not n.strip() for n in mutable):
            raise ValueError("DECISION_CONTRACT_INVALID: mutablePackages must be null or a list of package names")
        mutable = sorted(set(n.strip() for n in mutable))
    raw_lines = value.get("approvedVersionLines", {})
    if not isinstance(raw_lines, dict):
        raise ValueError("DECISION_CONTRACT_INVALID: approvedVersionLines must be an object")
    approved: dict[str, list[int]] = {}
    for name, majors in raw_lines.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(majors, list):
            raise ValueError("DECISION_CONTRACT_INVALID: version lines must map package names to major-number lists")
        if any(type(major) is not int or major < 0 for major in majors):
            raise ValueError("DECISION_CONTRACT_INVALID: approved version lines must be nonnegative integer majors")
        approved[name.strip()] = sorted(set(majors))
    policies = value.get("policies") or {}
    if mutable is not None and isinstance(policies, dict):
        conflicts = sorted(str(n) for n, p in policies.items() if p == "required" and n not in mutable)
        if conflicts:
            raise ValueError("DECISION_CONTRACT_INVALID: required update outside mutablePackages: " + ", ".join(conflicts))
    return {
        "version": DECISION_CONTRACT_VERSION,
        "targetMode": target_mode,
        "objective": objective,
        "mutablePackages": mutable,
        "approvedVersionLines": dict(sorted(approved.items())),
    }


def contract_from_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = (snapshot or {}).get("intentJson") or ""
    if isinstance(raw, dict):
        intent = raw
    else:
        try:
            intent = json.loads(raw) if raw else {}
        except (TypeError, ValueError) as exc:
            raise ValueError("DECISION_CONTRACT_INVALID: intentJson must contain valid JSON") from exc
    if not isinstance(intent, dict):
        raise ValueError("DECISION_CONTRACT_INVALID: intent must be an object")
    return normalize_decision_contract(intent)


def package_is_mutable(name: str, contract: Mapping[str, Any]) -> bool:
    mutable = contract.get("mutablePackages")
    return mutable is None or name in mutable


def _major(version: str) -> int | None:
    try:
        return int(version.lstrip("v").split(".", 1)[0])
    except (TypeError, ValueError, AttributeError):
        return None


def version_line_allowed(name: str, current: str, candidate: str, contract: Mapping[str, Any]) -> bool:
    # The current version is a constraint-bearing fallback, even outside the
    # approved update lines. A bound never forces an unverified upgrade.
    if candidate == current:
        return True
    if not package_is_mutable(name, contract):
        return False
    approved = contract.get("approvedVersionLines") or {}
    if name in approved:
        return _major(candidate) in approved[name]
    if contract.get("objective") == LATEST_APPROVED:
        current_major = _major(current)
        return current_major is not None and _major(candidate) == current_major
    return True


def decision_contract_prompt(contract: Mapping[str, Any], language: str) -> list[str]:
    """One authority contract in both supported prompt languages."""
    pinned = contract["targetMode"] == "pinned"
    header = "## Контракт решений" if language == "ru" else "## Decision contract"
    lines = [header, "", "```json", json.dumps(dict(contract), ensure_ascii=False, sort_keys=True, indent=2), "```", ""]
    if language == "ru":
        lines += [
            "- Требования пользователя и уже выданные разрешения определяют scope. Draft сам по себе не разрешает исполнение; повторное подтверждение уже разрешённого действия не требуется.",
            "- Режим задачи задаёт пользователь: PLAN_ONLY означает только анализ и план, без install, выполнения проверок проекта, изменений проекта и создания в нём migration-документов. Все инструкции ниже об исполнении, аудитах, checkpoints, проверках и DEVELOPER_UPGRADE_GUIDE.md / MIGRATION_REPORT.md применяются только к уже разрешённому пользователем EXECUTE. В PLAN_ONLY укажи необходимые проверки и документы как будущие шаги, не выполняя их.",
            (
                "- pinned: точные target — согласуемое назначение версий. Применяй его только в рамках разрешённой пользователем работы. Отклонение требует явного нового назначения и перепланирования затронутых ограничений до применения."
                if pinned else
                "- advisory: target — рекомендация. Разрешены обоснованные альтернативы в уже разрешённых пользователем границах; запиши decision diff (предложено → выбрано, причина, evidence) и перепланируй затронутые ограничения до применения."
            ),
            "- mutablePackages=null разрешает планировать все пакеты; [] запрещает изменения. Пакеты вне списка остаются неизменными, но сохраняются как ограничения совместимости. approvedVersionLines задаёт разрешённые major для обновлений, а не исключения из графа.",
            "- Для latest-compatible-in-approved-lines пакет без заданных major остаётся в текущем major. POLICY_SATISFIED означает выполнение health-политики, но не достижение новейших совместимых версий. POLICY_SATISFIED и рекомендация planner оставить/defer пакет не являются пользовательским pin; explicit user exclusions, keep-current и mutablePackages являются границами пользователя. Статический план не доказывает глобальную оптимальность свежести или runtime-совместимость.",
            "- Перепланирование означает явное обновление выбранного назначения, затронутых ограничений и evidence. Оно не требует механического повтора идентичного MCP-запроса. В advisory разрешённое пользователем локальное решение с decision diff и проверкой затронутых связей допустимо; не выдавай его за подтверждённое Core.",
            "- Известный конфликт совместимости требует перепланирования затронутой компоненты. UNKNOWN/неполное покрытие не доказывает конфликт: продолжай независимые компоненты только при обоснованной независимости. Не повторяй идентичный запрос без нового evidence или изменённых ограничений.",
            "- Изменения входных manifest/lock/config, политики, scope или выбранного назначения делают прежний план устаревшим. Зафиксируй новое назначение и свежие проверки; локальный выбор не объявляй подтверждённым Core. Если требуется выход за разрешённые пользователем границы, запроси именно недостающее разрешение.",
        ]
    else:
        lines += [
            "- User requirements and existing authorization define scope. A Draft does not itself authorize execution; do not request confirmation again for an already authorized action.",
            "- The user task determines the stage: PLAN_ONLY means analysis and planning only, without install, project checks, project edits or migration documents inside the project. All execution, audit, checkpoint, check and DEVELOPER_UPGRADE_GUIDE.md / MIGRATION_REPORT.md instructions below apply only to user-authorized EXECUTE. In PLAN_ONLY list the required checks and documents as future steps without performing them.",
            (
                "- pinned: exact targets define the proposed assignment. Apply it only within the work the user authorized. A deviation requires an explicit replacement assignment and replanning of affected constraints before applying it."
                if pinned else
                "- advisory: targets are recommendations. Evidence-backed alternatives within existing user-authorized bounds are allowed; record a decision diff (proposed → selected, reason, evidence) and replan affected constraints before applying it."
            ),
            "- mutablePackages=null permits planning all packages; [] permits no changes. Packages outside the list remain unchanged and still constrain compatibility. approvedVersionLines lists allowed update majors; it does not exclude graph nodes.",
            "- Under latest-compatible-in-approved-lines an unspecified package stays in its current major. POLICY_SATISFIED denotes the health-policy goal, not the newest compatible versions. POLICY_SATISFIED and planner keep/defer recommendations are not user pins; explicit user exclusions, keep-current and mutablePackages are user bounds. A static plan proves neither global freshness optimality nor runtime compatibility.",
            "- Replanning means explicitly updating the selected assignment, affected constraints and evidence. It does not require mechanically repeating an identical MCP request. In advisory, an authorized local decision with a decision diff and evaluation of affected relationships is allowed; do not label it as Core-confirmed.",
            "- A known compatibility conflict requires replanning the affected component. UNKNOWN/incomplete coverage is not proof of a conflict: continue independent components only with evidence of independence. Do not repeat an identical request without new evidence or changed constraints.",
            "- Changed input manifests/lock/config, policy, scope or selected assignment invalidate the previous plan. Record the new assignment and fresh checks; do not label a local choice as Core-confirmed. Ask for only the missing authorization when going beyond user-approved bounds.",
        ]
    return lines + [""]
