# DepLoom

Публичный monorepo DepLoom (desktop-приложение пока сохраняет совместимое имя Dependency Flow): Python CLI находится в корне, Electron-приложение — в `desktop/`. Installer включает tool и его Python-зависимости, поэтому версии desktop и tool всегда совпадают.

Новый командный workspace Desktop создаёт самостоятельно: локально инициализирует Git-репозиторий, `.dependency-roadmap/settings.project.json`, рабочие каталоги и начальный commit. Внешний `dependency-roadmap-template` для новых workspace больше не требуется. Уже созданные template-based workspace продолжают подключаться как обычные существующие Git-репозитории без миграции; legacy IPC с явно переданным `templateRemote` также сохраняет прежний clone/upstream/origin сценарий.

Версия продукта хранится в `VERSION` и обязана совпадать с `desktop/package.json`. Тег `vX.Y.Z` запускает GitHub Actions release pipeline в `AlexanderLevenskikh/deploom` и публикует публичный GitHub Release с installer, blockmap и `latest.yml`; desktop обновляется только из этого GitHub repository без клиентского API-ключа.

### Публикация самого DepLoom

Для публичного репозитория используется только SSH remote `git@github.com:AlexanderLevenskikh/deploom.git` и ветка `master`. Скрипт `push-github-branch-and-tag.ps1` умеет начать прямо из распакованного sanitized archive без `.git`: он создаёт свежую историю на `master`, настраивает/проверяет `origin` через SSH, запускает release validation, push ветки и тега.

Первая публичная публикация текущей версии `0.2.0`:

```powershell
.\push-github-branch-and-tag.ps1 `
  -Commit "Initial public release: DepLoom 0.2.0" `
  -Tag v0.2.0
```

Для следующего обычного patch-release `-Tag` можно не указывать: общий release helper автоматически увеличит patch version, проверит чистый release commit и только после validation отправит `master` и новый tag. Если `origin` уже указывает на тот же GitHub repository через HTTPS, GitHub-скрипт безопасно переключит его на SSH; любой другой remote считается ошибкой.
CLI-инструмент для квартального аудита npm/yarn-зависимостей: строит MD/JSON/HTML отчёт, считает статус проекта, подбирает целевые версии, группирует зависимости по стратегии работ и генерирует промпт для агента из HTML.

## Полный FLOW в Desktop

Во время Автопилота промежуточное завершение отдельной agent/generate/audit-итерации не считается завершением всей миграции и не создаёт misleading OS-уведомление «Миграция завершена». Финальное уведомление приходит только когда у Autopilot действительно больше нет следующего действия.

Основной пользовательский путь состоит из этапов: подготовка, baseline, миграция/перепланирование, верификация, независимый аудит, release-ветка, state-коммит и публикация. Их по-прежнему можно запускать вручную, но основной режим — **«Автопилот до результата»**: Desktop сам проходит оставшиеся этапы по порядку, автоматически строит свежий agent prompt из Dashboard и перехватывает recoverable-ошибки встроенным recovery-agent. Публикация запускается Автопилотом только когда для проекта уже включён `git.push`; никакого неожиданного push из-за одного клика не появляется.

Baseline теперь является владельцем version planning: constraint solver решает peer/environment graph целиком, затем materialization loop фактически устанавливает candidate assignment до создания agent-веток. Failing combinations локализуются параллельно и превращаются в learned `nogood` constraints. Группы после этого — только способ нарезать уже доказанный план; Executor чинит код, но не перебирает версии заново.

Начиная с `v0.1.125` Baseline различает **hard constraints** и **verification interactions**. Обычная опубликованная `dependency` между двумя прямыми пакетами проекта не объявляется ложным peer/equality constraint (package manager вправе поставить nested copy), но связывает пакеты для project-level диагностики. Если такое сочетание реально ломает type/config/toolchain check, verifier локализует версионную комбинацию и только тогда обучает solver точному `nogood`.

Внутренние этапы не становятся «best effort»: красная work-ветка не merge-ится, обязательные type/lint/build проверки принадлежат оркестратору, а cumulative `merged` перепроверяется после merge. Supervisor/Planner может безопасно перестроить план, автоматически замкнуть доказанный direct peer companion среди уже существующих прямых зависимостей или **временно отложить** недостижимый target/cohort. Такое временное откладывание не является exclusion: пакет остаётся в health и финальном списке blockers. Новый baseline очищает эти временные решения и пробует построить лучший план заново.

Начиная с `v0.1.82` recoverable-состояния считаются внутренней работой Автопилота, а не поводом звать разработчика. Stale scope, обычный replan, exhausted residual strategy, build/type/lint regression и recoverable merge/release failure сначала проходят Supervisor/Executor/recovery loop. Красный пользовательский stop предназначен для недоверенного Git-state, нарушения immutable scope/safety-инварианта, устойчиво недоступной инфраструктуры или дефекта самой тулзы. Replan после уже выполненных merge строится от cumulative `*-merged`, поэтому готовая работа не появляется в плане заново.

Начиная с `v0.1.84` ephemeral worktree occupancy вообще не сохраняется как долговременный package deferral: старые `PARALLEL_USER_WORKTREE_BLOCKED`, указывающие в собственный `%TEMP%/dependency-flow-worktrees`, автоматически очищаются перед residual replan. Windows-пути нормализуются по slash/case, поэтому собственный worker не должен становиться «user-owned».

Начиная с `v0.1.83` **дополнительная Git-ветка тоже не является ошибкой пользователя**. Исторические continuation refs, созданные Dependency Flow, автоматически восстанавливаются из сохранённых планов при совпадении package scope; остальные refs остаются в quarantine и не попадают в `merged` без детерминированного adoption. Сам факт существования лишнего ref не блокирует completion. Даже transient infrastructure failure сначала получает до трёх автоматических retry. Красный stop оставлен для состояний, где продолжение способно потерять/подменить код: неизвестный активный `MERGE_HEAD`, недоверенное Git-состояние, pinned source/release mismatch, пользовательские изменения перед destructive handoff или устойчиво сломанная инфраструктура/сама тулза.

Независимые группы могут исполняться параллельно через отдельные tool-managed Git worktree (`autonomy.maxParallelGroups`, по умолчанию `2`, поддерживается до `12`). Каждая группа получает отдельную agent-session и свой checkout; совместимые/пересекающиеся scope не запускаются одновременно. Готовые ветки затем merge-ятся **строго последовательно**, и после каждого merge выполняется cumulative verification. Ошибка одного worker не отменяет зелёный результат остальных.

Зацикливание ограничивается не только числом попыток, а **фактическим прогрессом**: Autopilot сравнивает cumulative merged commit и health (`lagOk/total`, Critical/High). Смена только candidate/residual package list больше не считается прогрессом; один полный Supervisor→migration→audit цикл без изменения Git/health закрывает этот путь. Реально изменившийся merged commit позволяет продолжать даже при том же проценте, например после compatibility/source repair. Retry агента использует свежий контекст/checkpoint вместо многомиллионного старого диалога. Если новый архитектурный scope нельзя добавить безопасно, Supervisor по умолчанию предпочитает закрыть остаточный исполнимый план на 100%, а blocker оставить для финального handoff, вместо остановки всего FLOW на `APPROVAL_REQUIRED`. Инфраструктурные и safety-инварианты этим механизмом не обходятся.

**Best-effort разрешён только на самом release:** когда автоматический execution plan исчерпан, нет Critical-уязвимостей, но выбранный health-level всё ещё недостижим без нового scope. При этом обычные project gates, final gates и repository hooks остаются обязательными. После успешного best-effort release Desktop создаёт `.dependency-roadmap/desktop/handoff/<project>-<target>-best-effort.md` с достигнутым состоянием и оставшимися blockers — его можно сразу передать другому агенту, не повторяя уже зелёную работу.

Для Yarn Classic независимый аудит по умолчанию создаёт изолированный `package-lock.json` и выполняет обычный `npm audit` через настроенный registry; рядом сохраняется команда воспроизведения. Перед release пользовательские изменения уходят в именованный safety stash. State-коммит использует whitelist командных путей, не добавляет локальные downloads/cache и спокойно завершается, если новых изменений нет. Последний этап публикует сохранённую release-ветку проекта и только затем командный workspace; успешный статус означает, что выполнены оба push.

## Установка

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-solver.txt
```

`requirements.txt` содержит только переносимые Python-зависимости. `z3-solver` поставляется отдельно, потому что его wheel содержит нативный `libz3` и зависит от ОС/архитектуры; `desktop/scripts/prepare-tool.mjs` сам выбирает корректную platform wheel для package target.

### Exact constraint solver

`z3-solver` устанавливается из `requirements-solver.txt` и является authoritative backend по умолчанию. Production Baseline не принимает heuristic fallback как доказанный план: `unknown`/timeout или `unsat` локально откладывают только затронутый independent component на current assignment и не подменяются custom search; отсутствие/поломка bundled Z3 остаётся явной ошибкой среды. Legacy custom solver можно включить только явно для диагностики/сравнения.

```json
{
  "constraintVerification": {
    "solverBackend": "z3",
    "compareLegacySolver": false,
    "persistentLearning": true,
    "learningReproductions": 2
  }
}
```

Для диагностического сравнения можно включить legacy comparator на небольших компонентах:

```json
{
  "constraintVerification": {
    "solverBackend": "z3",
    "compareLegacySolver": true,
    "legacyComparatorMaxComponentSize": 8
  }
}
```

Реальный npm/Yarn/pnpm resolver остаётся proof-oracle. Воспроизводимые dependency-resolution nogoods сохраняются в environment-scoped cache; project/build/infra/unknown failures в persistent cache не попадают.

Долгие deterministic verification/localization фазы имеют heartbeat и hard watchdog. По умолчанию один subprocess ограничен 600 секундами, целая isolated assignment verification — 3600 секундами, localization cycle — 7200 секундами, heartbeat приходит каждые 15 секунд. Последняя фаза также сохраняется в `.dependency-roadmap/state/baseline-verification-progress.json`. Auto-discovery в adaptive mode запускает lint/type/style/script checks, затем build и `test:unit`/`test`; обычные source-migration ошибки остаются Executor work и не становятся solver constraints.

```json
{
  "constraintVerification": {
    "timeoutSeconds": 600,
    "attemptTimeoutSeconds": 3600,
    "localizationTimeoutSeconds": 7200,
    "progressIntervalSeconds": 15
  }
}
```

## Запуск

Основной FLOW запускается из установленного Dependency Flow. Standalone CLI для automation и диагностики:

```bash
python dependency_live_roadmap_generator.py --project-settings "<team-workspace>/.dependency-roadmap/settings.project.json"
```

Desktop development:

```bash
cd desktop
npm ci
npm run dev
```

Скрипт сам ищет конфиги в таком порядке:

1. `.dependency-roadmap/settings.project.json` — пушимый проектный конфиг.
2. `.dependency-roadmap/settings.local.json` — локальный игнорируемый override.
3. `settings.project.json`.
4. `settings.local.json`.
5. `settings.json` — legacy.

Порядок слияния:

```txt
defaults внутри скрипта
→ settings.project.json
→ settings.local.json
→ CLI args
```

CLI-аргументы всегда сильнее настроек.

## Пример

```bash
python dependency_live_roadmap_generator.py \
  --project-settings .dependency-roadmap/settings.project.json \
  --local-settings .dependency-roadmap/settings.local.json \
  --html-out .dependency-roadmap/artifacts/report.html
```

## Прокси и корпоративный registry

По умолчанию тулза **не наследует системный proxy Windows/env** для HTTP-запросов. Это защищает от ситуации, когда `requests` пытается ходить в registry через старый локальный прокси вроде `127.0.0.1:10809`, хотя браузер открывает registry напрямую.

Обычно для корпоративного registry ничего включать не надо:

```json
{
  "registry": "https://registry.npmjs.org",
  "useSystemProxy": false
}
```

Если окружение действительно требует системный proxy для registry/OSV, можно явно включить его:

```bash
python dependency_live_roadmap_generator.py --use-system-proxy
```

Или в локальном конфиге:

```json
{ "useSystemProxy": true }
```

## Группы работ

Группы отражают очередь и стратегию работы, а не только тип пакета или semver:

1. **Срочные и несложные security/removal миграции** — быстро снять Critical/High, обновить до безопасной версии или выпилить простое использование.
2. **Нужные и относительно простые runtime/API/CI обновления** — compatible replacement, import-only fork, minor/small-major, CI/tooling update с малым diff и понятным smoke.
3. **Runtime/API/CI изменения, не доказанные как простые** — router, SignalR, DnD/PDF/upload/CI, если есть риск поведения, неясный changelog или нужны подгруппы.
4. **Сложные/platform/blocked миграции** — stylelint/Vite/TS/UI/auth/shared/published widget/latest-vulnerable; отдельная задача и владелец.
5. **Остальное / lag / DEV** — низкоприоритетные DEV/local/test/storybook/mkcert хвосты без влияния на runtime/CI/release.

HTML и Markdown отчёты показывают для каждой строки, какие уязвимости работа должна снять или какой остаточный риск остаётся.

## Артефакты

- `artifacts/*.md/html/json` — воспроизводимые отчёты, обычно игнорируются.
- `state/dashboard-state.json` — сохраняемые решения по группам, подгруппам, lag-policy и комментариям; его стоит пушить. Во встроенной вкладке Desktop изменения planning-state сохраняются на диск автоматически и сами ставят свежую «Верификацию» в очередь, поэтому после исключения/возврата/смены group или lag-policy roadmap и targets пересчитываются без отдельного ручного шага. При открытии HTML напрямую в браузере по-прежнему нужно вручную нажать «Экспортировать state», положить файл по этому пути и затем запустить «Верификация». Один и тот же файл копит решения по всем пакетам сразу — добавление нового исключения не должно и не должно было заменять уже сохранённые (см. CHANGELOG v0.1.44: старое поведение при устаревшем локальном черновике заменяло весь state целиком, из-за чего более раннее исключение могло молча потеряться).
- `history/events.jsonl`, `history/index.md`, `history/runs/*`, `history/snapshots/*`, `history/baselines/*` — долговременная append-only история; её стоит пушить.

## Выбор минимально рискованного target

Целевые версии для перехода `до жёлтого` / `до зелёного` выбираются не только по номеру группы. Для Yellow порядок принципиален: сначала строится полный пул lag-кандидатов, затем peer/cohort/registry-проверки определяют реально исполнимое множество, и только после этого greedy убирает самые дорогие необязательные цели. План сохраняет запас 85% над жёсткой границей Yellow 80%. Если совместимый максимум ниже 80%, ничего полезного не отбрасывается: весь безопасный остаток выполняется, а недостигнутая цель оформляется как best-effort handoff после обычных проверок. Для финального сокращения используется консервативный greedy-score:

- C/H уязвимости получают сильный приоритет, потому что основной процесс — снижение security-рисков;
- затем учитывается примерная стоимость/риск: runtime/API/CI, group 3/4, platform/build/UI/auth/shared, major/minor/patch;
- major-обновления React/React-DOM, React UI, Vite/Webpack, stylelint/ESLint/TypeScript и похожие platform-миграции получают большой штраф;
- дешёвые DEV/local/test хвосты могут быть выбраны раньше тяжёлой group 4, если они безопаснее и помогают дойти до lag-критерия;
- если у platform/build пакета есть C/H, security-приоритет всё равно поднимает его выше обычного lag/DX.

Идея: достичь Ж/З минимально рискованным набором работ, не таща React/Vite/UI-major только ради процента актуальности, когда есть более дешёвые кандидаты.

Кандидат-версия должна быть не только достижимой по датам/vuln-политике, но и реально пригодной: помимо дешёвой проверки «tarball скачивается» (`registry_version_is_installable`), для версий с заявленными TypeScript-декларациями (`types`/`typings`/`exports["."].types`, включая вложенные условия `import`/`require` у dual ESM/CJS пакетов) дополнительно скачивается и разбирается сам архив — если объявленный путь к `.d.ts` в нём физически отсутствует, версия считается непригодной. Наблюдалось вживую: внутреннее зеркало реестра репаковало `date-fns@4.2.0` с 2887 файлами, но фактически без деклараций типов — tarball при этом честно скачивался, `npm/yarn install` проходил, а typed-сборка падала. Первая версия этой проверки смотрела только на плоский `exports["."].types` и пропускала сам date-fns, потому что там `types` объявлен на уровень глубже, внутри условий `import`/`require` — это тоже пришлось исправить. `min_by_lag`/`min_by_vuln` уже перебирают кандидатов от текущей версии вверх и берут первую подходящую, поэтому непригодная версия просто пропускается в пользу следующей без отдельного цикла повторов. Если пригодной версии не находится вплоть до `latest`, пакет остаётся в обычном deferred-состоянии с объясняющей причиной — так же, как при недостижимом реестре — и не блокирует остальной scope.

## Экспорт из HTML

Основные действия в HTML:

- **Выгрузить промпт** — по умолчанию формирует компактную автономную задачу для моделей с малым контекстом. В prompt остаются точные counts/hash, `compact-v1` manifest и branch plan; подробные release-intelligence/evidence агент читает из roadmap JSON только для текущего пакета. Полный диагностический формат выбирается отдельно. Scope по умолчанию включает `update` и `deferred`; режим «только видимые строки» оставлен для осознанного частичного scope.
- **Промпт для ветки** — выпадающий список показывает каждую ветку Branch plan всех проектов независимо от текущих фильтров; кнопка формирует промпт, ограниченный ровно этой веткой: только её manifest rows, её release dossier, branch plan из одного элемента, без merge/release команд и без `## Final validation` (эти шаги выполняет оркестратор, а не агент). Пока это ручной diagnostic-экспорт — автономный цикл «отдельная короткая сессия агента на каждую branch-группу вместо одной растущей на весь прогон» ещё не подключён.
- **Подробно комментировать сложные изменения/конфиги** — чекбокс задаёт явную `codeCommentPolicy` для compact/full prompt. Включённый режим просит объяснять только неочевидные причины и compatibility-ограничения в изменённых местах; выключенный запрещает миграционный дневник и лишние пояснения, сохраняя необходимые комментарии корректности. Policy сохраняется в dashboard state, переносится в run state/evidence для continuation.
- **Выгрузить ТЗ** — создаёт человеко-читаемый Markdown для постановки задачи: цель, исходное состояние, scope обновлений, исключения/deferred, branch plan, риски, проверки и критерии приёмки.
- **Выгрузить package.json и предложения** — формирует ручной patch-фрагмент по четырём секциям: `dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies`.
- **Сохранить снимок дашборда** — сохраняет полный текущий roadmap и UI state в browser history; JSON можно скачать и импортировать.
- **Настроить** у строки — меняет группу, подгруппу, lag-policy 3/6/9/12 месяцев и комментарий; здесь же пакет можно исключить из текущего расчёта с обязательной причиной, не скрывая его из отчёта. Для массового исключения отметьте строки чекбоксами и нажмите **«Исключить выбранные»** — одна причина применяется ко всей выбранной пачке; **«Вернуть выбранные»** возвращает их в scope.
- **Подробнее** в колонке release notes — показывает breaking changes, migration notes, deprecations, требования, покрытие диапазона и источники.
- **История / сравнение** — позволяет открыть полный исторический dashboard и сравнить любые два файловых или browser snapshot.

Во все команды агента попадает `registry` из settings с запретом самовольно переключаться на public npm registry. Экспорт package.json не заменяет install/lockfile: после применения нужны install, audit/outdated diff, тесты и проверка changelog/release notes.

## Компактный prompt для моделей с малым контекстом

HTML dashboard теперь по умолчанию экспортирует формат `compact`:

- повторно используемые инструкции вынесены в `AGENT_RUNBOOK.md` workspace;
- точный scope хранится как `compact-v1` JSON с `columns + rows`;
- подробный release intelligence остаётся в `dependency-roadmap.json` и читается пакет за пакетом;
- после каждой work branch агент сохраняет checkpoint state;
- `validate_dependency_update.py` принимает как старый object manifest, так и `compact-v1`.

Формат `full` доступен для диагностики, но он существенно больше. При ограниченном контексте выбирайте один проект, а для крупных проектов — одну группу на сессию. При `CONTINUE_REQUIRED` новая сессия продолжает работу по `*_state.json`, не перечитывая старый чат.

## Проверка после merge

После работы агента или мерджа нескольких групп прогоняйте машинную приёмку результата:

```bash
python tools/dependency-roadmap-tool/validate_dependency_update.py \
  --roadmap-json .dependency-roadmap/artifacts/dependency-roadmap.json \
  --project Demo.App \
  --project-dir C:/Users/developer/work/Demo.App \
  --target-mode yellow
```

Проверка ловит прямые зависимости ниже target, prerelease вместо stable target, версии выше target без объяснения и отсутствие lockfile-entry для target. Если есть baseline `package.json` до миграции, добавьте `--baseline-package-json <path>` — тогда отчёт также покажет direct-изменения вне выбранного scope.

## Работа с baseline проекта

Для миграции одного проекта удобно сначала зафиксировать состояние до изменений:

```bash
./scripts/generate.sh --only-project Demo.App --capture-baseline --baseline-label deps-start
```

После работы агента и мерджа групп запустите генерацию без `--capture-baseline`:

```bash
./scripts/generate.sh --only-project Demo.App
```

Отчёт подтянет последний snapshot из `.dependency-roadmap/history/baselines/<project>/` и покажет transition вроде `red -> yellow` или `red -> green`, дельту lag/C/H и количество direct dependency changes. Для следующего проекта создайте отдельный baseline, например `--only-project Admin.App --capture-baseline`.

Lag-границы также фиксируются в baseline. При его создании remediation target получает однократный запас 3 месяца: `12→9`, `9→6`, `6→3`, `3→3`. Обычный запуск без `--capture-baseline` использует те же сохранённые границы и не пересчитывает запас от нового registry `latest`, поэтому target не «уплывает» между генерациями.

## Регрессионные тесты в агентском промпте

Scope manifest содержит immutable `action`, `testPolicy`, `testReason`, `lagPolicyTarget`, `targetReason`, registry artifact evidence и compatibility cohort. Новые выгрузки используют `scopeHashVersion: 5`: дополнительно hash-защищены `scopeExcluded`, причина и источник исключения. Старые manifest остаются валидными по своей версии. Если строгая policy требует concrete target, но строка почему-то экспортируется как deferred, dashboard/validator останавливаются с `ROADMAP_TARGET_DESYNC`. Явно распознанный deprecated `@types/*` stub получает `action=remove`, а не бессмысленную установку stub-версии.

Для required-зависимости агент обязан либо доказать релевантность существующего теста, либо создать долговечный dependency-regression gate до обновления и прогнать его на baseline и после миграции. Для generated gate обязателен безопасный failure/mutation probe с восстановлением исходного состояния; validator также отклоняет skip/only, tautological assertions, отсутствующий package usage и тесты, которые отдельная команда фактически не собирала.

Новые проверки складываются отдельно от обычного тестового прогона: предпочтительно в `regressionTests/dependencyRegression` или проектный аналог. Generated test/gate files делятся по пакетам: один package-specific файл или подпапка на пакет, без `group-*`/umbrella-файлов и без совместного использования одного generated-файла несколькими пакетами. Для них нужен отдельный npm/yarn script, например `test:dependency-regression`; каталог должен быть явно исключён из ordinary `test`.

Попутный рефакторинг запрещён: допускаются только минимальные compatibility changes, непосредственно необходимые выбранному пакету; более широкая работа останавливается как `REFACTOR_REQUIRED`. Evidence обязан содержать `changePolicy=minimal-compatibility-only` и `refactoringPerformed=false`.

Фразы «`yarn test` зелёный», «сборка прошла», e2e/manual smoke или обещание тестировщика не закрывают required test policy. Run обязан сохранить evidence JSON по шаблону `templates/dependency-regression-evidence.example.json` и пройти:

```bash
python validate_dependency_regression.py \
  --project-dir <project> \
  --scope-manifest <run_scope.json> \
  --evidence <dependency-regression-evidence.json>
```

Отдельные suites самой тулзы:

```bash
python run_tool_tests.py --suite unit
python run_tool_tests.py --suite regression
```

Unit-команда намеренно не собирает `tests/regression/`.

После каждого merge prompt требует cumulative reconciliation (`MERGE_TARGET_REGRESSION`), а финальный validator сравнивает исходный scope с фактом и со свежим roadmap. Если целевой статус не достигнут или появились новые action rows, run обязан продолжиться closure-веткой.

## Автономный цикл проекта

В настройках проекта можно задать Git-контракт, который попадёт в выгруженный промпт:

```json
{
  "name": "Demo.App",
  "path": "Demo.App",
  "git": {
    "remote": "origin",
    "sourceBranch": "master",
    "baseBranch": "libs",
    "branchPrefix": "libs",
    "mergedBranch": "libs-merged",
    "releaseBranch": "libs-release",
    "push": false
  }
}
```

Промпт содержит точный branch plan: создаёт ветки подгрупп/групп от базовой ветки, проверяет каждую, последовательно мержит их в `mergedBranch`, разрешает конфликты семантически и повторяет проверки после merge. Все intermediate commit/merge/internal push выполняются через `git_hook_policy.py --mode skip`. Затем `dependency_release_branch.py` создаёт `releaseBranch` от verified source commit, squash-ит integration tree, удаляет audit workspace, запускает final gates и делает единственный обычный commit с hooks. Push запрещён, пока `push` не установлен в `true`.

В промпте также есть полный scope-manifest с `update` и `deferred`, количеством строк и deterministic hash. Его нужно сохранить как run-артефакт и передать валидатору:

```bash
python validate_dependency_update.py \
  --roadmap-json .dependency-roadmap/artifacts/dependency-roadmap.json \
  --scope-manifest .dependency-roadmap/history/runs/<run>_scope.json \
  --project Demo.App \
  --project-dir C:/work/Demo.App \
  --target-mode yellow \
  --final-roadmap-json .dependency-roadmap/artifacts/dependency-roadmap.final.json \
  --require-final-status \
  --strict-above-target
```

Валидатор различает `dependencies`, `devDependencies`, `optionalDependencies` и `peerDependencies`, поэтому одинаковый пакет в разных секциях не схлопывается молча.

## Состояние дашборда, подгруппы и custom lag

В HTML можно изменить группу, подгруппу, комментарий и lag-policy строки (3/6/9/12 месяцев). Кнопка **Сохранить настройки dashboard** сохраняет/скачивает state по пути из настройки:

```json
{
  "dashboardState": ".dependency-roadmap/state/dashboard-state.json"
}
```

Закоммитьте этот файл. При следующей генерации он накладывается поверх `groups.override.json`, а встроенная копия state сразу загружается обратно в браузер. Ключи section-aware (`runtime:react`, `peer:react` и т. п.), поэтому объявления одного пакета не конфликтуют.

Компактные tracked snapshots лежат в `history/snapshots/`. В браузере истории можно явно выбрать **До** и **После** и увидеть статус, C/H, lag и изменения direct dependencies.

## Анализ release notes и breaking changes

Для каждого обновления `current → target` генератор пытается собрать и проанализировать:

- GitHub releases в точном диапазоне версий;
- файлы `CHANGELOG`, `HISTORY`, `MIGRATION`, `UPGRADING` из репозитория;
- такие же документы внутри npm-тарбола target-версии;
- npm-метаданные о deprecation.

Дашборд показывает `breaking-confirmed`, `breaking-likely`, `coverage-incomplete`, `no-breaking-found` или `unavailable`. Анализ хранится отдельно для каждого уникального target, поэтому переключение «по умолчанию / до жёлтого / до зелёного» меняет и target, и соответствующие release notes. В модалке видны строки-доказательства, migration notes, deprecations, требования к Node/peer/browser, покрытие и ссылки. `no-breaking-found` используется только когда источники реально прочитаны и диапазон промежуточных stable-версий покрыт; иначе будет `coverage-incomplete` или `unavailable`.

Это best-effort разведка, а не замена инженерной проверке. Настройки:

```json
{
  "releaseIntelEnabled": true,
  "releaseIntelMaxPackages": 0
}
```

`0` означает без лимита. Для быстрого отчёта без загрузки release notes используйте `--skip-release-intel`.

## Ручная независимая сверка

`manual_dependency_audit.py` — независимая проверка. Для npm/pnpm она использует родной механизм аудита. Для Yarn Classic режим `auto` создаёт изолированный `package-lock.json` и запускает обычный `npm audit` через явно настроенный реестр. Файл и безопасная команда `reproduce-npm-audit.cmd` сохраняются в служебной папке аудита. Она использует текущий реестр npm; другой адрес можно передать параметром `--registry`. Исходный проект не получает `package-lock.json`. Возможное расхождение графов Yarn и npm записывается в отчёт как ограничение точности, но не делает успешно полученный ответ npm ошибкой. Строгие режимы `yarn-native` и `yarn-inventory` остаются доступны для диагностики. Затем через `npm view ... time dist-tags` проверяются прямые зависимости с политикой отставания 3/6/9/12 месяцев.

```bash
python manual_dependency_audit.py \
  --project-dir C:/work/Demo.App \
  --project-name Demo.App \
  --registry https://registry.example/repository/npm-group \
  --dashboard-state .dependency-roadmap/state/dashboard-state.json \
  --yarn-audit-engine auto
```

Проверка выводит общий `COMPLETE`/`INCOMPLETE` и отдельные статусы vulnerability/lag. Ненулевой код audit из-за найденных уязвимостей допустим, если JSON разобран. Отчёт не смешивает affected node count, vulnerable package count и unique advisory count; для npm v2 также показывает exact node paths/versions. Недоступный package manager, неразобранный audit, bridge drift, неизвестные lag-даты или декларация, из которой нельзя извлечь semver, дают `INCOMPLETE` и exit code `2`.

### Исключения из текущего scope

Разовый CLI override:

```bash
python dependency_live_roadmap_generator.py \
  --project-settings .dependency-roadmap/settings.project.json \
  --exclude-dependency 'Demo.App|runtime|oidc-client|backend migration is blocked'
```

Аргумент repeatable и поддерживает `package|reason`, `project|package|reason` и `project|kind|package|reason`. В HTML то же решение задаётся через настройки строки и сохраняется в `dashboard-state.json`. Исключение требует причины, остаётся visible/auditable и защищается `scopeHashVersion: 5`.

## Source checkout guard

Настройка `sourceCheckoutGuard=true` включает preflight до любого анализа и записи артефактов. Для проекта используются `git.remote` (по умолчанию `origin`) и обязательный `git.sourceBranch`. Чистый checkout переключается и fast-forward обновляется до fetched remote commit; грязный, ahead или diverged checkout отклоняется. Временное отключение: `--skip-source-checkout-guard`.


## Guarded generation без automatic audit bootstrap

`sourceCheckoutGuard=true` применяется только при `--capture-baseline`: generator проверяет чистый fetched `remote/sourceBranch@commit` и сохраняет baseline.

Обычный `generate` не создаёт audit branch, не запускает `npm audit` и не строит npm `package-lock.json` для Yarn. Старое `auditBootstrap` принимается для совместимости, но игнорируется с warning. Ручной audit запускается явно через Desktop или bundled `manual_dependency_audit.py`.

## Git hook policy и release finalizer

```json
{
  "gitHooks": {
    "intermediateCommits": "skip",
    "intermediateMerges": "skip",
    "intermediatePushes": "skip",
    "releaseCommit": "run",
    "releasePush": "run"
  },
  "migration": {
    "verificationCommands": ["yarn lint:types", "yarn lint:styles", "yarn build"],
    "integrationVerificationCommands": ["yarn lint:types", "yarn lint:styles", "yarn build"]
  },
  "release": {
    "strategy": "squash",
    "cleanupAuditWorkspace": true,
    "commitMessage": "chore(deps): update dependencies",
    "finalGateCommands": []
  }
}
```

`migration.verificationCommands` запускаются самим Desktop после работы агента **до merge группы**. `migration.integrationVerificationCommands` запускаются на накопительном `mergedBranch` после каждого merge и перед выходом из migration-stage. Если секция `migration` не задана, Desktop переиспользует `release.finalGateCommands`; если нет и их — выбирает только уже существующие package scripts (`lint:types`/`typecheck`, специализированные lint, test, build). Новая baseline→post регрессия возвращается агенту как следующая задача; красная ветка не считается готовой по одному текстовому `ready-to-commit`.

`git_hook_policy.py` использует временный пустой `core.hooksPath` на одну команду и не меняет repository/global config. Source checkout/fast-forward в generator использует ту же политику, поэтому post-checkout/post-merge hooks тоже откладываются до release. `dependency_release_branch.py` проверяет source/merged branches, при необходимости сохраняет dirty worktree в `dependency-flow-before-release-*`, создаёт clean release branch, выполняет squash без intermediate hooks, удаляет legacy tool-managed audit workspace, если он существует, собирает шардированные `docs/<name>/<branch>.md` в плоские `docs/<name>.md` в natural-порядке веток (см. ниже), запускает повторяемые `--gate-command`, запрещает оставленный ими dirty tree (`RELEASE_FINAL_GATE_DIRTY`) и делает normal release commit. В stdout печатаются commit safety stash и команда восстановления. При падении gate или hook staged squash остаётся для безопасного повтора; отсутствие audit workspace не считается ошибкой.

Три отслеживаемых migration-документа (`docs/dependency-upgrades.md`, `docs/dependency-update-summary.md`, `docs/dependency-update-review-notes.md`) агент пишет не напрямую, а по одному шарду на work branch: `docs/dependency-upgrades/<branch>.md` и так далее. Каждая sibling-ветка создаёт файл с нуля от общего base, поэтому запись в общий плоский файл гарантированно давала `add/add` конфликт при merge любых двух веток. Шардирование по уникальному имени ветки убирает конфликт по построению; `dependency_release_branch.py` собирает шарды в плоские файлы один раз, детерминированно, непосредственно перед release commit — после того как все ветки уже влиты и новые шарды появиться не могут.

Desktop state-коммит сначала обновляет уже отслеживаемые файлы, затем добавляет только разрешённые новые пути: настройки, history, dashboard state, `flow-state.json` и knowledge. Игнорируемые downloads/cache не форсируются. Если staged diff пуст, этап считается уже актуальным и завершается без пустого коммита.


### TTL кеша publish metadata

Даты публикации переиспользуются **24 часа** по умолчанию. Для разового запуска интервал можно переопределить переменной `ROADMAP_AUDIT_METADATA_CACHE_HOURS`; значение `0` принудительно отключает кеш. Переиспользование `package-lock` и audit evidence по-прежнему определяется хешем входных файлов и не зависит от этого TTL.

## Current checkout и lockfile одного package manager

`package.json` хранит direct declarations/ranges. Exact current versions берутся из lockfile того же package manager:

- Yarn → `yarn.lock`;
- npm → `package-lock.json`/`npm-shrinkwrap.json`;
- pnpm → `pnpm-lock.yaml`.

`--capture-baseline` validates and records the fetched source branch. Every ordinary run analyzes the current checkout, refreshes a stale **canonical project lockfile** by default and optionally deduplicates Yarn when tooling exists. Mixed root lockfiles are rejected.

Generator не выполняет проверку уязвимостей. Для ручной сверки `manual_dependency_audit.py` использует родной аудит npm/pnpm, а для Yarn по умолчанию — изолированный `package-lock.json` и `npm audit`. Сопоставление с `yarn.lock` остаётся в артефактах как оценка точности; строгие канонические режимы включаются явно.

Defaults require no `lockfileSync` block:

```json
{
  "baselineMode": "validate",
  "currentMode": "update",
  "allowExtraLockfiles": false,
  "yarnDeduplicate": "auto"
}
```

`--post-update` remains a compatibility alias. Use `--lockfile-mode validate` in a final staged-tree gate when mutation must be forbidden.
