# Draft Modes: Revalidation #8 — B1 (Fast-гейт по точной версии) и B2 (тема харнесса + видимость CTA)

Дата: 2026-09-24. База: v0.2.125. Релиз: v0.2.126. Статус: **B1 и B2 закрыты, полный регресс зелёный** (Python 992 OK skipped=4, Desktop 45/45 check-скриптов, сборка, oxlint 0 errors, браузерный DOM-E2E ок).

## Итог

| Пункт | Приоритет | Суть | Статус |
|---|---|---|---|
| B1 | P1 | Fast-гейт пропускал оценку выбранной версии, когда текущий агрегат ≤ лимита: 0→H:1 при H=0, 0→unknown и H:1→H:2 при H=1 считались удовлетворяющими | закрыт |
| B2 | P2 | Dev-harness не грузил `index.css` → `.button.primary` белым текстом на прозрачном фоне (в проде не воспроизводилось); заодно найден и закрыт дефект контраста primary в тёмной теме (2.41:1) | закрыт |

Все негативные сценарии **сначала зафиксированы падающими** (`tests/test_revalidation_8_issues.py`, 11 тестов; первый прогон — 6 красных), затем исправлен гейт (TDD-фикс до изменения модели) и починена тема харнесса.

---

## B1 — Гейт должен оценивать ТОЧНУЮ выбранную версию каждого активного пакета и каждой severity (P1)

Корень: в `_candidate_satisfies_fast_policy` (dependency_live_roadmap_generator.py, security-цикл) вычислялся `overstated = [r for r in active if концентрация findings на ТЕКУЩЕЙ версии > 0]` и при `aggregate <= limit: continue` оценка пропускалась вовсе. Точно-версионная проверка делалась только по строкам, у которых текущая версия уже превышала лимит. Поэтому кандидат мог:

- перевести чистую строку (0) на версию с H:1 при политике H=0 → гейт возвращал True;
- перевести строку на версию без OSV-доказательств вообще (unknown) → гейт возвращал True;
- вырастить H:1 → H:2 на выбранной версии при лимите H=1 → гейт возвращал True (агрегат текущих равен лимиту → continue).

Решение — замена security-цикла: для **каждого** активного пакета и **каждой** severity (C/H/M/L) теперь берётся ТОЧНО выбранная версия (`candidate_targets.get(r.name) or current_targets.get(r.name) or r.current_version`) и `_severity_at_candidate_version(r, planned, severity)` считает количество именно по ней:

- `planned == current` → измерение `current_vulns` (строка, которую кандидат не трогает);
- иначе evidence-мап `vuln_evidence_by_version` для выбранной версии;
- отсутствие evidence для ТОЧНОЙ версии → `None` (unknown) → гейт возвращает `False` (Fast никогда не «угадывает безопасность»);
- агрегат по всем активным строкам, сравнение с лимитом: `remaining > limit → False`; `remaining ≤ limit → True`.

Это сохраняет N3: частичный фикс, оставляющий ровно разрешённое количество findings, по-прежнему удовлетворяет политике. Сохранён и принцип N2: «unknown» возможен только для обновляемых строк без evidence — строки, не упомянутые кандидатом, всегда имеют `current_vulns`.

Проверено тестами (failing-first → green):

| Сценарий | Ожидание | Было до фикса |
|---|---|---|
| 0 (текущая чистая) → выбор 2.0.0 с H:1, лимит H=0 | False | True (баг) |
| 0 → выбор 2.0.0 без evidence | False | True (баг) |
| H:1 → выбор 2.0.0 с H:2, лимит H=1 | False | True (баг) |
| 0 → 0 (evidence `0`), лимит H=0 | True | True |
| 2×H:1, фикс одной строки → остался H:1, лимит H=1 | True | True |
| 2×H:1, без фикса (агрегат 2) | False | False |
| Verified-assignment с вносимым H:1 → `fast_policy_satisfied(False)` → `None` и для fast, и для deep | None | — (эта версия была True → событие могло эмититься) |

Плюс source-контракт «реальная цепочка»: `_continue_after_verified_candidate` вызывает `_verified_assignment_satisfies_policy`, которая вызывает `_candidate_satisfies_fast_policy` на assigning-инкумбента, и только затем `anytime.fast_policy_satisfied(...)` перед `progressive-fast-policy-satisfied` — т.е. гейт применяется именно к проверенной связке, а не только к обёртке-булеану.

Гарантии не затронуты: глубокий/последний верифицированный инкумбент не деградирует (структура lag-проверок не менялась; весь регресс, включая N2/N3 из раунда #7 и старые fast-гейт тесты, зелёный). Draft по-прежнему не запускает физическую верификацию.

---

## B2 — Харнесс должен грузить ту же базовую тему, что и прод (P2)

Корень: `desktop/src/e2e/dialog-harness.tsx` импортировал только `../App.css`. `.button.primary` в App.css — `background: var(--blue)`, а `--blue` определён в `desktop/src/index.css`, который грузит `desktop/src/main.tsx` (`import './index.css'`). В харнессе computed-style primary был `color: rgb(255,255,255)` на `background: rgba(0,0,0,0)` — «видимость» CTA в DOM-E2E ничего не означала. В проде это не воспроизводилось (main.tsx грузит тему).

Исправления:

1. **Тема харнесса**: добавлен `import '../index.css'` перед `'../App.css'` (тот же порядок, что в проде).
2. **RU/EN**: в харнесс добавлен переключатель языка (через `LanguageProvider.setLanguage`, localStorage) и runtime-проба `window.__harnessStyles()` — computed `background`/`color`/геометрия primary + viewport, плюс `document.documentElement.lang`. Проба позволяет автоматически, а не «на глаз», проверять CTA.
3. **Найден и закрыт дефект контраста в тёмной теме**: в тёмной теме `--blue = #6ea8ff` давал primary `white on #6ea8ff` = **2.41:1** (fail WCAG AA). Введены токены `--blue-fill`/`--blue-fill-hover` = `#1769e0`/`#0f5bc7` во ВСЕХ трёх блоках темы (`:root`, `html[data-theme='dark']`, `@media (prefers-color-scheme: dark)`), `.button.primary` переведён на `var(--blue-fill, var(--blue))`. Контраст стал **5.08:1 в светлой и тёмной** теме.

DOM-E2E (реальный браузер, vite dev `/e2e-harness.html`, диалог restart, 77 пакетов):

| Проверка | bg primary | цвет | контраст | CTA в viewport |
|---|---|---|---|---|
| EN, 800×600 | `rgb(23,105,224)` | белый | 5.08:1 | да (bottom 575 ≤ 600) |
| RU, 800×600 («Начать заново и запустить») | `rgb(23,105,224)` | белый | 5.08:1 | да (bottom 575 ≤ 600) |
| RU, короткое окно 800×450 | `rgb(23,105,224)` | белый | 5.08:1 | да (bottom 426 ≤ 450) |
| EN, минимальное окно Electron 1080×700 (main.ts minWidth/minHeight) | `rgb(23,105,224)` | белый | 5.08:1 | да (bottom 675 ≤ 700) |
| Тёмная тема (emulate `prefers-color-scheme: dark`) | `rgb(23,105,224)` (было `#6ea8ff`, 2.41:1) | белый | 5.08:1 | да |

Скриншоты сохранены в `assets/revalidation-8-dialog-cta-en.png`, `-ru.png`, `-minwindow.png` и добавлены в `scripts/public-binary-allowlist.json` (sha256). Примечание: в этой сессии модель не принимает изображения на вход, поэтому «видимость» проверена структурно (computed-style + геометрия + контраст) и по тексту подписи кнопки; сами файлы — артефакты для просмотра человеком (и для CI-sanitization).

Про «масштабирование»: размеры окна Electron задаются в DIP == CSS px (`electron/main.ts` BrowserWindow, minWidth 1080/minHeight 700), поэтому масштабирование ОС/DPI не уменьшает CSS-viewport — проверка на минимальном окне 1080×700 является границей. Дополнительный CSS-zoom 1.25 поверх окна 800×450 (искусственный случай короче любого реального окна, 360 CSS px высоты) уводит футер диалога за видимую область без внешнего скролла — в реальном приложении такой высоты достичь нельзя (minHeight 700).

Реальный Electron-прогон «Начать заново → prepare-dialog → submit → job» в этой среде не выполнялся (те же ограничения, что в раунде #7: MCP-браузер не подключается к процессу Electron, а живой прогон с прерванным базлайном требует интерактивной сессии). Проводка подтверждена по коду: submit-интент `baselineResume='restart'` → `main.ts:2462` инжектит `DEPLOOM_BASELINE_RESUME: 'restart'` в окружение job'а; ретраевый путь принудительно `auto` (`main.ts:5832`). Ручная процедура: открыть Flow → «Начать заново» → prepare-dialog → запуск; в логе job'а проверить `DEPLOOM_BASELINE_RESUME=restart` и видимый прогресс/артефакт; закрытие/отмена диалога не трогает сохранённый предыдущий результат. Харнессный submit за такой прогон не выдаётся.

---

## Регресс

- Python: **992 OK (skipped=4)** — 981 накопленных + 11 новых revalidation_8; негативные сценарии B1 сначала красные (6 failures на первом прогоне), после фикса все зелёные; N2/N3 и старые fast-гейт тесты не регрессировали.
- Desktop: `tsc` (electron + `-b`) и `vite build` — OK; oxlint — 0 errors (2 warnings в dev-харнессе, как и раньше); **45/45** `check-*` скриптов — OK (включая `check:baseline-intent`, `check:i18n`, `check:target-closure`, `check:acceptance-policy`, `check:progressive-contract`).
- Скриншоты зааллоулистены; новых бинарников вне allowlist нет.

## Изменённые файлы

- `dependency_live_roadmap_generator.py` — B1: security-цикл `_candidate_satisfies_fast_policy` переведён на оценку точной версии каждого активного пакета для каждой severity.
- `tests/test_revalidation_8_issues.py` — новый: 11 тестов (B1 гейт/цепочка + B2 контракты темы).
- `desktop/src/e2e/dialog-harness.tsx` — B2: `import '../index.css'`, RU/EN-переключатель, `__harnessStyles`-проба.
- `desktop/src/index.css` — B2: токены `--blue-fill`/`--blue-fill-hover` во всех блоках темы.
- `desktop/src/App.css` — B2: `.button.primary` на контраст-безопасный fill с fallback.
- `scripts/public-binary-allowlist.json` — 3 новых PNG.
- `assets/revalidation-8-dialog-cta-*.png` — артефакты скриншотов.
