from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class CohortSuggestion:
    cohort_id: str
    label: str
    predicate: str
    subject: str
    packages: Tuple[str, ...]
    required_packages: Tuple[str, ...]
    confidence: float
    reasons: Tuple[str, ...]
    authority: str = "DIAGNOSTIC_HINT"

    def to_json(self) -> dict[str, object]:
        value = asdict(self)
        value["id"] = value.pop("cohort_id")
        value["packages"] = list(self.packages)
        value["requiredPackages"] = list(self.required_packages)
        value.pop("required_packages", None)
        value["reasons"] = list(self.reasons)
        return value


def _normal(name: str) -> str:
    return str(name or "").strip().lower()


def _basename(name: str) -> str:
    return _normal(name).rsplit("/", 1)[-1]


def _vite(name: str) -> bool:
    value = _normal(name)
    return (
        value in {"vite", "vitest", "rollup"}
        or value.startswith("@vitejs/")
        or value.startswith("vite-")
        or value.startswith("@sentry/vite")
        or value.startswith("@storybook/builder-vite")
        or value.startswith("unplugin")
    )


def _storybook(name: str) -> bool:
    value = _normal(name)
    return value == "storybook" or value.startswith("@storybook/") or "storybook" in value


def _eslint(name: str) -> bool:
    value = _normal(name)
    return (
        value == "eslint"
        or value.startswith("eslint-")
        or value.startswith("@typescript-eslint/")
        or value == "@babel/eslint-parser"
    )


def _stylelint(name: str) -> bool:
    value = _normal(name)
    return value == "stylelint" or value.startswith("stylelint-") or value.startswith("@stylelint/")


def _webpack(name: str) -> bool:
    value = _normal(name)
    base = _basename(value)
    return (
        value == "webpack"
        or value.startswith("webpack-")
        or value.startswith("@storybook/builder-webpack")
        or value == "eslint-import-resolver-webpack"
        or base.endswith("-loader")
    )


def _typescript(name: str) -> bool:
    value = _normal(name)
    return (
        value == "typescript"
        or value.startswith("@typescript-eslint/")
        or value in {"ts-node", "ts-jest", "typescript-eslint"}
    )


def _react(name: str) -> bool:
    value = _normal(name)
    return (
        value in {"react", "react-dom", "@types/react", "@types/react-dom"}
        or value.startswith("@vitejs/plugin-react")
        or value.startswith("@storybook/react")
    )


_RULES = (
    ("vite-build", "Vite / build tooling", {"rollup", "vite", "vitest"}, _vite),
    ("storybook", "Storybook", {"storybook"}, _storybook),
    ("eslint", "ESLint", {"eslint"}, _eslint),
    ("stylelint", "Stylelint", {"stylelint"}, _stylelint),
    ("webpack", "Webpack / build tooling", {"webpack"}, _webpack),
    ("typescript", "TypeScript tooling", {"typescript", "tsc"}, _typescript),
    ("react", "React", {"react", "react-dom"}, _react),
)


def _rule_for(predicate_family: str, subject: str):
    family = _normal(predicate_family)
    normalized_subject = _normal(subject)
    base = _basename(normalized_subject)
    for cohort_id, label, subjects, matcher in _RULES:
        if normalized_subject in subjects or base in subjects or matcher(normalized_subject):
            return cohort_id, label, matcher
    if family.startswith("duplicate-type-universe") and base == "rollup":
        return "vite-build", "Vite / build tooling", _vite
    return None


def infer_baseline_cohort(
    *,
    predicate: str,
    direct_packages: Iterable[str],
    policy_by_package: Optional[Mapping[str, str]] = None,
    repeated_count: int = 1,
    max_packages: int = 12,
) -> Optional[CohortSuggestion]:
    # Navigation-only: this may change search order/user policy, never proof authority.
    raw_predicate = str(predicate or "").strip()
    if not raw_predicate:
        return None

    family, separator, subject = raw_predicate.partition(":")
    if not separator:
        family, subject = raw_predicate, ""
    subject = subject.strip()

    direct = tuple(sorted({str(name) for name in direct_packages if str(name).strip()}))
    if not direct:
        return None
    normalized_direct = {_normal(name): name for name in direct}
    policies = {str(k): str(v) for k, v in (policy_by_package or {}).items()}

    reasons: list[str] = []
    candidates: set[str] = set()
    known_rule = _rule_for(family, subject)
    if known_rule is not None:
        cohort_id, label, matcher = known_rule
        candidates.update(name for name in direct if matcher(name))
        reasons.append("known-ecosystem")
    else:
        cohort_id = "interaction"
        label = "Связанная compatibility-когорта"

    normalized_subject = _normal(subject)
    if normalized_subject in normalized_direct:
        candidates.add(normalized_direct[normalized_subject])
        reasons.append("predicate-direct-package")

    base = _basename(subject)
    if base and len(base) >= 4:
        lexical = {
            name for name in direct
            if base in _basename(name) or _basename(name) in base
        }
        if lexical:
            candidates.update(lexical)
            reasons.append("predicate-name-neighborhood")

    if not candidates:
        return None

    required = tuple(sorted(
        name for name in candidates if policies.get(name, "auto") == "required"
    ))
    deferrable = [
        name for name in candidates if policies.get(name, "auto") == "auto"
    ]
    if not deferrable:
        return None

    def rank(name: str) -> tuple[int, str]:
        value = _normal(name)
        if value == normalized_subject:
            return (0, value)
        if value in {
            "vite", "rollup", "vitest", "eslint", "stylelint",
            "webpack", "typescript", "react", "react-dom",
        }:
            return (1, value)
        if subject and base and base in _basename(name):
            return (2, value)
        return (3, value)

    deferrable = sorted(deferrable, key=rank)[: max(1, int(max_packages))]

    confidence = 0.56
    if known_rule is not None:
        confidence = 0.78
    if int(repeated_count or 0) >= 2:
        confidence += 0.08
        reasons.append("repeated-predicate")
    if len(deferrable) >= 3:
        confidence += 0.05
        reasons.append("multi-package-cohort")
    if normalized_subject in normalized_direct:
        confidence += 0.04
    confidence = min(0.95, round(confidence, 2))

    return CohortSuggestion(
        cohort_id=cohort_id,
        label=label,
        predicate=raw_predicate,
        subject=subject,
        packages=tuple(deferrable),
        required_packages=required,
        confidence=confidence,
        reasons=tuple(dict.fromkeys(reasons)),
    )
