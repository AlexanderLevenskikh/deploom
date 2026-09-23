# DepLoom - Валидация №4: policy-канон, staleness, deadline, heartbeat, Fast/Deep (F1-F6)

База сравнения: `3fccb91` (v0.2.119) -> HEAD `f408a69` (v0.2.120), релиз v0.2.121.
Среда: Windows, Python 3.14.3, Node v24.18.0; npm-registry офлайн (проверки через локальные tarball).

## 1. F1/P1 - канон TargetPolicy и честный acceptance-verdict (done)

- Канон - `acceptancePolicy`; `mergeTargetPolicy` (top-level побеждает; legacy-вложенность мигрирует и зеркалится на top-level); парные поля верифицируются кросс-язычно (Python writer / Node reader).
- Verdict честный: `unverifiable[]` -> UNKNOWN, `gates[]` -> REMEDIATION_REQUIRED; зелёный пресет H0/M0/L0/minLagOkPct=100; `report.policy` обязателен и совпадает по всем полям, иначе UNKNOWN; 0 не перекоёрсивается в 80.
- Цель прогнана целиком: UI intent -> env (`goalPolicyEnv`/audit job flags) -> Python (`DEPLOOM_BASELINE_TARGET_LEVEL/MIN_LAG_OK_PCT/ACCEPTANCE_POLICY_JSON`) -> snapshot/hash -> closure/acceptance.
- Цепочка доказана тестом `tests/test_f1_acceptance_evidence_chain.py` (7 тестов): real producer `manual_dependency_audit.build_report` -> real JSON -> real Node verdict из `desktop/dist-electron/acceptance-policy.js`; parity identity-hash Python==Node; input change -> UNKNOWN; mismatch policy -> UNKNOWN; green 3/10 -> REMEDIATION_REQUIRED.

## 2. F2/P1 - closure-высота над Yellow (done)

- Scope в health: unknown в знаменателе; `reached` = достижение по текущим критериям уровня + High-лимит из policy (`maxKnownHigh` - 5-й параметр targetClosure-функций).
- `desktop/scripts/check-target-closure.mjs` + Python-тесты (scope, unknown-denominator).

## 3. F3/P1 - жёсткий дедлайн (deadline supervision) (done)

- `run_supervised(phase, deadline, fn)`: daemon-worker + supervisor; бюджет = remaining минус резерв публикации; на истечении - DraftBudgetExceeded, partial-артефакт публикуется, процесс завершается.
- Обёрнуты header-фаза и CPU-heavy планировочные листья; `fetch_bytes` читает по 1 байту под дедлайном (`_bounded_read_max`).
- Тесты `DraftDeadlineHardBoundTests`: header trickle (2.6s), binary trickle, supervisor.

## 4. F4/P1 - byte-lockstep fingerprint и staleness (done)

- Входной identity = 6 manifest-файлов + `.dependency-roadmap/settings.project.json` + `settings.local.json` + `state/dashboard-state.json`; байтовый протокол `name\0bytes\0` идентичен Python и Node.
- Отсутствие inputHashes в manifest -> stale («проект не привязан к входным файлам (нет input-идентичности)»).
- Staleness сравнивает ПОЛНУЮ acceptance-политику (targetLevel/minLagOkPct/lagPolicyMonths/maxKnownHigh/maxKnownModerate/maxKnownLow) через `acceptancePolicyJson` в settings vs `currentPolicy.acceptancePolicy`; 3 независимых reason-push.
- PARTIAL-публикации привязываются к inventory-хешу (не пересчитываются при публикации).
- UI: stale-баннер + reason на карточке Draft и в PromptPreviewDialog; IntentDialog - segmented toggle (без native `<select>`).
- Тесты: `test_f4_staleness_covers_settings_dashboard_and_missing_identity` (settings edit/add, dashboard byte-change, High 1->2, lag 3->6, same policy fresh, missing identity); publish bind manifest->inventory hash.

## 5. F5/P2 - живой прогресс heartbeat (done)

- Daemon-heartbeat `[draft-progress]` каждые `DRAFT_PROGRESS_HEARTBEAT_SECONDS` (env, default 2.0); замораживается терминальным event; stage-pct <= 99 без status, 100 только при status.
- UI: монотонный elapsed (backend `elapsedSec` + локальное продолжение), 1s тик, retained stage-counters (сброс при смене step), продолжение бюджета локально, stale после 8s.
- Auto-open потребляет runId (ack) только после подтверждённой доставки prompt (`openDraftPrompt` -> Promise<boolean>).
- Тесты `DraftProgressHeartbeatTests` + `check-progressive-contract.mjs` (F5 sentinels).

## 6. F6/P1 - Fast/Deep настоящая стратегия + real-resource fixture (done)

- `--mode draft/fast/deep` теперь реальная стратегия, а не транспортный флаг: `main()` пишет `DEPLOOM_MODE` в окружение; `AutomaticBudgetPolicy.strategy` = fast|deep; новый `BaselineCompletionStatus.VERIFIED_POLICY_SATISFIED_FAST`.
- Fast останавливает дорогой поиск на ПЕРВОМ verified-назначении, удовлетворяющем выбранную policy (свой маленький бюджет); Deep продолжает улучшать в большом бюджете и сохраняет лучший verified-результат при ошибке/timeout.
- Бюджеты: fast -> 2 попытки / 300 c; deep -> 12 / 3600 c; `verify` сохраняет legacy executionMode mapping (закреплено `test_block_phi_execution_modes.py`).
- Real-resource fixture: vendored `tests/fixtures/real-lib/kontur-verified-fixture-lib` (offline `npm pack` -> `file:` tarball); `build.js` реально `require`-ит и использует библиотеку.
  - Совместимый кандидат: file:1.0.0 -> real `npm install`/`npm test` (REAL_LIB_test_OK) + Draft plan.
  - Несовместимый кандидат: `^2.0.0` против установленной 1.0.0 -> реальный npm отказывается устанавливать; Draft (planning-only, без install) планирует обновление на реальный `^2.0.0`.
  - Двухзапусковый policy-chain теперь сравнивает РЕАЛЬНЫЙ verdict для кандидата: yellow80 -> ACCEPTED, green90 -> REMEDIATION_REQUIRED (реальный producer -> реальный файл -> реальный Node reader) — не только settings/hash/prompt.
- Честное ограничение: живой Electron-сценарий (клик -> Python -> reader -> IPC -> prompt) в этом окружении не запускался; покрытие на unit + cross-language + real-fixture уровне.

## 7. Регресс и известные ограничения

- Целевой набор валидации: 198 тестов green (draft_baseline_contract, draft_flow_integration_physical, f1_acceptance_evidence_chain, dependency_roadmap, block_psi53_search_depth_ux, block_phi_execution_modes, block_psi_anytime, block_psi42, block_psi53_repeated, progressive_baseline, progress_envelope, block_psi3/psi4, proof_handoff_firewall).
- Полный прогон: 1160+ passed; структурный тест `test_proof_handoff_firewall` обновлён под новую форму F3 (вызов verified-резолвера внутри `run_supervised`), суть контракта сохранена.
- Починено pre-existing падение `tests/regression/test_lockfile_consistency.py::...test_current_checkout_cli_refreshes_yarn_lock_before_dashboard_analysis`: `dashboardLockfile` в отчёте теперь стабильное имя lockfile (`yarn.lock`), а не машинно-зависимый абсолютный путь.
- `desktop`: все `check:*` скрипты ALL_SCRIPTS_OK; `tsc -p tsconfig.electron.json` и `tsc -b` green.
- Sanitization `check-public-sanitization.py`: OK (публичная поверхность чистая).
- Офлайн среда: npm-registry недоступен; аудит/OSV-evidence мокаются (кандидат реальный); живые npm-install/typecheck/test проверены на локальных tarball.
