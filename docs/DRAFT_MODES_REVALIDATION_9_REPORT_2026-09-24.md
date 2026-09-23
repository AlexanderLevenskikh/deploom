# Draft Modes: Revalidation #9 — C1 (уязвимость OSV без severity)

Дата: 2026-09-24. База: v0.2.126. Статус: **C1 закрыт, полный регресс зелёный** (Python 1006 OK skipped=4, Desktop 45/45 check-скриптов, сборка, oxlint 0 errors).

## Итог

| Пункт | Приоритет | Суть | Статус |
|---|---|---|---|
| C1 | P1 | OSV-entry без `database_specific.severity`/CVSS получал `severity_rank=0` → `vuln_summary` = `U:1`; `U` не проверялся Fast-гейтом (0→U:1, текущая U:1 и mixed H:1,U:1 проходили), health отдавал `security_unknown=0`, а `target-closure.ts` предпочитал `security_unknown` значению `unknown` → Yellow closure закрывался при `unknown=1` | закрыт |

Негативные сценарии **зафиксированы падающими** (`tests/test_revalidation_9_issues.py`, 14 тестов; первый прогон: 8 failures + 2 errors), затем исправлены генератор, health, Draft-план и Node-модуль closure (TDD-порядок: тесты до изменений), после чего весь регресс зелёный.

---

## Единое определение достаточности security evidence (C1)

Введено и задокументировано одно определение, общее для текущей и точно выбранной версии (`_summary_has_unrated_vulns` / `_row_has_unrated_vulns`):

- `U>0` — **обнаруженная** уязвимость без оценки серьёзности; это НЕ доказанный ноль и не может подтверждать достижение policy;
- явное отсутствие evidence (`None`/`unknown`, `_row_security_known=False`) — OSV не оценил строку вовсе — остаётся отдельным состоянием;
- доказанный ноль (`0` / пустой список OSV) — отдельное состояние, не блокирует.

Три состояния больше не смешиваются: `vuln_summary([]) == "0"` (ноль) ≠ `vuln_summary([{'id':'OSV-UNRATED'}]) == "U:1"` (обнаружено, без серьёзности) ≠ отсутствие оценки.

## Fast-гейт

`_candidate_satisfies_fast_policy`: в `severity_limits` добавлено измерение `("U", 0)`. Проход идёт по ТОЙ ЖЕ схеме, что C/H/M/L (B1 — каждый активный пакет, точная выбранная версия через `_severity_at_candidate_version(r, planned, "U")`): `U>0` на фактически выбранной версии → остаток > 0 → `False`; нет данных для точной версии → `None` → `False` (N2 сохранён). Для `0` и разрешённого остатка известных severity поведение прежнее (проверено позитивами). Результат гейта кормит `anytime.fast_policy_satisfied` → `VERIFIED_POLICY_SATISFIED_FAST` не эмитится ни для fast, ни для deep.

Проверено:

| Сценарий | Ожидание | Было до фикса |
|---|---|---|
| текущая `U:1` (без обновления), C=0/H=0 | False | True (баг) |
| 0 → выбор `2.0.0` с evidence `U:1`, C=0/H=0 | False | True (баг) |
| H:1 → выбор `2.0.0` с `H:1;U:1`, H-лимит 1 | False | True (баг; H позволен, U блокирует) |
| 0 → `0` | True | True |
| H:1 → `H:1` при лимите 1 | True | True |
| выбор без evidence (N2) | False | False |

## Health

`compute_project_health`:
- добавлено поле `ProjectHealth.security_unrated` — строки с `U>0` на текущей версии (отдельное от «OSV недоступен»);
- `security_unknown = vuln_unknown_rows + security_unrated_rows` — проект с `U>0` больше не даёт полное security-покрытие;
- `unknown` не тронут: `totals["U"] + vuln_unknown_rows` — счётчики не удваивают одно и то же (U-findings агрегируются в `unknown`, строки — в `security_unknown`/`security_unrated`; `U:2` в одной строке = `unknown:2`, `security_unrated:1`);
- `reason` теперь различает причины: «security неизвестна: N» (OSV не оценил) и «уязвимости без оценки серьёзности: N» (U).

## Draft-план и отчёт

`build_draft_plan`: строки с `U>0` попадают в счётчик `unknown-security` (и, как следствие, в `metadata.unknown`/`unknownPackages` и сводку manifest), со своим machine-readable reason `уязвимость без оценки серьёзности (U) для этого пакета в этом Draft run`; `clarity` остаётся `security`. Причина для неоценённой OSV-строки не изменена («OSV/security state unknown…») — состояния различимы по причинам, счётчик не удваивает.

## Closure (desktop/electron/target-closure.ts)

Исправлен приоритетный баг: `security_unknown:0` больше не является авторитетным поверх `unknown`. Теперь
`securityUnknown = max(security_unknown, unknown, security_unrated)` (любой из трёх определён → берётся максимум; все три отсутствуют → недостаточно данных, как раньше). Yellow и Green остаются `reached=false`, пока любое из значений > 0. Старые фикстуры (только `security_unknown:0`, без `unknown`) не изменили поведения — 45/45 check-скриптов и N5/N6/N7-контракты зелёные.

Приёмка «health → roadmap → targetClosureFromRoadmap» прогнана на реальном health от генератора (строкa `U:1`, пустой план): yellow/green `reached=false`, `securityUnknown>0`.

## Регресс

- Python: **1006 OK (skipped=4)** — 992 накопленных + 14 новых revalidation_9; первый прогон новых тестов: 8 failures + 2 errors, после фикса все зелёные; T3/N2/N3/N5/H1/B1-контракты не регрессировали.
- Desktop: `tsc` (electron + `-b`) и `vite build` — OK; oxlint — 0 errors; **45/45** `check-*` скриптов (включая `check:target-closure`, `check:acceptance-policy`, `check:progressive-contract`).

## Electron

Браузерный harness и физический Electron-прогон разделены. В этой среде живого Electron-job не запускалось (MCP-браузер подключается только к Chromium; ранее подтверждённая проводка `baselineResume='restart'` → `DEPLOOM_BASELINE_RESUME=restart` в main.ts:2462 остаётся единственным UI-путём, который в C1 не менялся — правки только в генераторе/health/closure). Ручная процедура из раунда #8 действительна: «Начать заново» → prepare-dialog → submit; проверить в логе job `DEPLOOM_BASELINE_RESUME=restart` и видимый прогресс/артефакт; отмена сохраняет прежний результат. Харнессный submit за физический прогон не выдаётся.

## Изменённые файлы

- `dependency_live_roadmap_generator.py` — C1: `_summary_has_unrated_vulns`/`_row_has_unrated_vulns` (единое определение), Fast-гейт `("U",0)`, `ProjectHealth.security_unrated`, health `security_unknown` + причины, `build_draft_plan` U-счётчик/причины.
- `desktop/electron/target-closure.ts` — C1: `securityUnknown = max(security_unknown, unknown, security_unrated)`.
- `tests/test_revalidation_9_issues.py` — 14 failing-first тестов.
