# Draft Modes: Revalidation #10 - C2 (дедлайн Draft должен вмещать здоровое чтение реестра)

Дата: 2026-09-24. Релиз: не выпущен (готов к commit/tag по решению). Итог: **C2 закрыт, все гейты зелёные** (Python 1013 OK skipped=4, Desktop 45/45 check-скриптов, build, oxlint 0 errors).

## Задача

| Id | Приоритет | Суть | Статус |
|---|---|---|---|
| C2 | P1 | Draft-прогон для `partner-form` (run-5fadef80) вернул «registry metadata unavailable» для всех 77 пакетов и пустой prompt, хотя Nexus доступен. Разобраться и починить так, чтобы при доступном реестре Draft реально читал метаданные и строил план | закрыта |

Поведение при **недоступном** реестре оставлено как есть (по решению пользователя): честный пустой prompt с `unknown-metadata` — это штатная картина нештатной ситуации, дополнительная секция не нужна.

---

## Диагностика (почему «реестр недоступен», хотя Nexus жив)

Найдено по реальным артефактам прогона (`...\artifacts\runs\run-5fadef80-...\draft\result.json`, `plan.json`):

- `result.json`: `status: DRAFT_PARTIAL`, `elapsedMs: 14008`, `deadline.deadlineSeconds: 15.0`, `remainingMs: 991`, `partialReason: "Draft deadline (15.0s) exceeded at network-read; metadata may be incomplete."`, `metadata.metadataTotal: 76 / metadataKnown: 0`.
- `plan.json`: у всех строк `latest: "registry unavailable"`, `status: unknown-metadata`.

То есть прогон **не** столкнулся с недоступным реестром — он **истратил весь 15-секундный бюджет** внутри чтения метаданных и опубликовался как PARTIAL, а все строки остались в инвентарном состоянии «без метаданных».

Проверка окружения с этой же машины:

```
trust_env=False: HTTP 200 in 1.19s bytes=7076520 (registry payload) proxies=(none)
trust_env=True : HTTP 200 in 1.08s bytes=7076520 proxies=(none)
HTTPS_PROXY/HTTP_PROXY/ALL_PROXY env: (unset)
```

Nexus (`https://nexus.kontur.host/repository/kontur-npm-group`) отвечает ~1.1с на документ ~7МБ. Прокси не виноваты: `--use-system-proxy` выключен по умолчанию (`trust_env=False`), но прямой доступ работает.

**Корневая причина**: жёсткий дефолт `15.0s` неразрешим по времени для реального проекта. 76 зависимостей × (много-мегабайтные registry-JSON + TLS + постройка кандидатов) физически не укладываются в 15с: прогон умер на первом же многомегабайтном чтении тела и пометил **весь** список как «registry unavailable» при живом реестре. Дополнительно в `_prefetch_registry_metadata` (строка ~4199) в Draft намеренно не работает параллельный пул — метаданные читаются построчно и каждый запрос ограничен остатком бюджета (`_budgeted_timeout`), поэтому 15с — узкое место.

## Фикс

`dependency_live_roadmap_generator.py`:

- Константы: `DRAFT_DEADLINE_BASE_SECONDS` (15.0), `DRAFT_DEADLINE_PER_PACKAGE_SECONDS` (1.5), `DRAFT_DEADLINE_MAX_SECONDS` (300.0) — переопределяемы env (`DEPLOOM_DRAFT_DEADLINE_*`), как остальные constants.
- Хелпер `scale_draft_deadline_seconds(total_packages, base=…, per_package=…, max_seconds=…)`: `base + per_package * N`, с полом по `base` и потолком по `max` (один гигантский проект не превращает «быстрый план» в бесконечный).
- Отслеживание «явности» дедлайна: `draft_deadline_explicit = (args.draft_deadline_seconds is not None) or non-empty DEPLOOM_DRAFT_DEADLINE_SECONDS`.
- После локального инвентаря (T4), **до** сетевого скана: если Draft и дедлайн не задан явно — `deadline_clock.deadline_seconds = scale_draft_deadline_seconds(total_packages)`, env-пин и `[info] Draft deadline auto-scaled to Ns for M package(s)`. Clock расширяется in place (`started` не сбрасывается — инвентарь уже прошёл), поэтому `result.json.deadline` и все бюджетные проверки видят новое значение.
- Явный `--draft-deadline-seconds` / env — **verbatim**, без scale (проверено тестом).

Для repro-нагрузки (77 строк) дефолт станет `15 + 76×1.5 ≈ 129s` вместо 15s.

## Тесты (failing-first)

`tests/test_revalidation_10_issues.py` — 7 тестов; первый прогон: 6 errors (хелпера/констант не существовало; прошёл только explicit-контракт), после фикса все зелёные:

- юнит: рост с числом пакетов (76→129.0, 3→19.5, 2→18.0, 0→15.0); пол/потолок; уважение явной кривой (base/per_package/max); `DeadlineClock` после in-place расширения отдаёт scaled `deadlineSeconds` и корректный `remaining`;
- интеграция (реальный subprocess генератора, registry на закрытом порту `127.0.0.1:1`): без явного дедлайна манифест фиксирует `deadlineSeconds == scale(3) == 19.5` и `[info] …auto-scaled…`; при этом возврат 0 и честный PARTIAL/READY без метаданных (пустой prompt — как решил пользователь);
- интеграция: явный `--draft-deadline-seconds 7.5` → `deadlineSeconds == 7.5`, авто-scale строка отсутствует;
- интеграция «реестр доступен»: локальный `_MockRegistry` (packuments «dist-tags+versions» с tarball внутри настроенного registry и валидные mini-tar'ы) → `metadata.metadataTotal == 3`, `metadata.metadataKnown == 3` (было бы 0 на старом дедлайне), `status in (DRAFT_READY, DRAFT_PARTIAL)` — метаданные реально прочитаны до любых помех OSV (OSV-недоступность толерантна и не влияет на метаданные).

## Регресс

- Python: **1013 OK (skipped=4)** — 1006 накопленных + 7 новых revalidation_10; T1/T5/T8/T4/бюджетные отсечки (`_budgeted_timeout`, trickle-header/body, reserve) не регрессировали; интеграционные Draft-тесты с явными дедлайнами (0.05/7.5/60) не затронуты.
- Desktop: `tsc` (electron + `-b`) и `vite build` — OK; oxlint — 0 errors; **45/45** `check-*` скриптов.

## Изменённые файлы

- `dependency_live_roadmap_generator.py` — C2: константы `DRAFT_DEADLINE_*`, `scale_draft_deadline_seconds`, `draft_deadline_explicit`, авто-scale после инвентаря.
- `tests/test_revalidation_10_issues.py` — 7 failing-first тестов (юнит + интеграция с локальным mock-registry).
