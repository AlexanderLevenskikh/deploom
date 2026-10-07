"""Fail-closed acceptance of fresh independent checkpoint audit evidence."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def _number(value, low=0, high=math.inf):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def audit_recovery_reason(report, *, allow_canonical=True):
    from iterative_adaptive_budget import transient_audit_reason
    audit = report.get("audit") or {}
    if allow_canonical and report.get("auditComplete") is True and audit.get("engine") == "npm-lock-bridge" and audit.get("trusted") is True and audit.get("accuracy") == "npm-resolved-approximation":
        return "CANONICAL_YARN_AUDIT_REQUIRED"
    return transient_audit_reason(report)


def audit_engine_for_retry(requested, previous):
    if requested not in (None, "", "auto"):
        return requested
    if previous.get("retryableReason") and (previous.get("engine") == "yarn-inventory" or previous["retryableReason"] == "CANONICAL_YARN_AUDIT_REQUIRED"):
        return "yarn-inventory"
    return "auto"


def audit_policy_status(report):
    # A successful audit CLI proves collection, not compliance with the goal.
    audit = report.get("audit") or {}
    policy = report.get("policy") or {}
    if report.get("auditComplete") is not True or report.get("lagComplete") is not True:
        return "UNKNOWN"
    engine = audit.get("engine")
    if engine == "yarn-inventory":
        authoritative = audit.get("trusted") is True and (audit.get("canonicalInventory") or {}).get("complete") is True
    elif engine == "npm-lock-bridge":
        authoritative = audit.get("trusted") is True and audit.get("accuracy") == "canonical-yarn-match" and (audit.get("bridgeReconciliation") or {}).get("faithful") is True
    else:
        authoritative = engine in {"yarn-native", "npm-native", "pnpm-native"} or (audit.get("trusted") is True and audit.get("authoritative") is True)
    if not authoritative or not report.get("dependencyInputHash"):
        return "UNKNOWN"
    try:
        generated = datetime.fromisoformat(str(report.get("generatedAt") or "").replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - generated).total_seconds()
    except (ValueError, TypeError):
        return "UNKNOWN"
    if not -300 <= age <= 86400:
        return "UNKNOWN"
    totals = audit.get("packageTotals") or {}
    advisories = audit.get("advisoryTotals") or {}
    if not all(_number(totals.get(k)) for k in ("critical", "high", "unknown")):
        return "UNKNOWN"
    if totals["unknown"] or not _number(advisories.get("unknown")) or advisories["unknown"]:
        return "UNKNOWN"
    if policy.get("targetLevel") not in {"yellow", "green"} or not _number(policy.get("maxKnownCritical"), 0, 0):
        return "UNKNOWN"
    if not _number(policy.get("minLagOkPct"), 0, 100) or not _number(policy.get("maxKnownHigh"), 0, 99):
        return "UNKNOWN"
    if policy.get("lagPolicyMonths") not in {3, 6, 9, 12} or not _number(audit.get("lagOkPct"), 0, 100):
        return "UNKNOWN"
    failed = totals["critical"] > 0 or totals["high"] > policy["maxKnownHigh"]
    for severity, key in (("moderate", "maxKnownModerate"), ("low", "maxKnownLow")):
        if key in policy:
            if not _number(policy[key], 0, 99) or not _number(totals.get(severity)):
                return "UNKNOWN"
            failed = failed or totals[severity] > policy[key]
    green = policy["targetLevel"] == "green"
    failed = failed or (green and policy["maxKnownHigh"] != 0)
    failed = failed or audit["lagOkPct"] < (100 if green else policy["minLagOkPct"])
    return "FAIL" if failed else "PASS"
