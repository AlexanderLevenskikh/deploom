import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/data/logPresentation.ts", import.meta.url), "utf8");
const panelSource = await readFile(new URL("../src/components/LogPanel.tsx", import.meta.url), "utf8");
const monitorSource = await readFile(new URL("../src/data/processMonitor.ts", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const flowSource = await readFile(new URL("../src/components/FlowWorkspace.tsx", import.meta.url), "utf8");
const failureModalSource = await readFile(new URL("../src/components/BranchFailureModal.tsx", import.meta.url), "utf8");
const enLocaleSource = await readFile(new URL("../src/i18n/locales/en.ts", import.meta.url), "utf8");
const ruLocaleSource = await readFile(new URL("../src/i18n/locales/ru.ts", import.meta.url), "utf8");
const mainSource = await readFile(new URL("../electron/main.ts", import.meta.url), "utf8");
if (!mainSource.includes("stallWarningMs: 2 * 60_000") || !mainSource.includes("stallAbortMs: 15 * 60_000")) throw new Error("Baseline/generate commands must expose silence watchdog thresholds");
if (!mainSource.includes("HARD_STALL") || !mainSource.includes("метка возможного зависания")) throw new Error("Long deterministic jobs must surface visible stall markers");
if (!mainSource.includes("deterministicWatchdogFailure") || !mainSource.includes("полный Baseline автоматически повторно не запускаю")) throw new Error("A watchdog stop must not trigger three identical full Baseline retries");
if (!appSource.includes("for (const branch of details.migrationProgress?.branches ?? []) sessionBranches.add(branch.branch)")) throw new Error("Every planned migration group must appear before its first log/session");
const failedGroupI18nContract = [
  [flowSource, "migration-error-indicator"],
  [flowSource, "t('flow.runtime.failed')"],
  [failureModalSource, "t('branchFailure.autopilot')"],
  [failureModalSource, "t('branchFailure.userRequired')"],
  [enLocaleSource, '"flow.runtime.failed": "Waiting for Supervisor"'],
  [ruLocaleSource, '"flow.runtime.failed": "Ожидает Supervisor"'],
  [enLocaleSource, '"branchFailure.autopilot": "Autopilot will handle it"'],
  [ruLocaleSource, '"branchFailure.autopilot": "Автопилот исправит"'],
  [enLocaleSource, '"branchFailure.userRequired": "Action required"'],
  [ruLocaleSource, '"branchFailure.userRequired": "Требуется вмешательство"'],
];
for (const [contractSource, marker] of failedGroupI18nContract) {
  if (!contractSource.includes(marker)) throw new Error(`Failed-group i18n contract missing: ${marker}`);
}
if (!panelSource.includes("followLogRef.current") || !panelSource.includes("scrollHeight - element.scrollTop - element.clientHeight <= 48")) throw new Error("Log panel must preserve manual scroll position while new entries arrive");
if (panelSource.includes("scrollIntoView({ block: 'end' })")) throw new Error("Unconditional log autoscroll must not return");
if (!panelSource.includes("<RunMonitor") || !appSource.includes("language-switch")) throw new Error("Run monitor / language switch is missing");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const module = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const sources = module.mergeLogSources([], [{ kind: "group", id: "queued-1", label: "queued" }]);
if (sources.length !== 1 || sources[0][0] !== "group:queued-1") throw new Error(`Known runtime session was absent before its first log: ${JSON.stringify(sources)}`);
const sample = [
  { jobId: "agent", stream: "stdout", line: '{"type":"step_start","sessionID":"secret"}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"tool_use","part":{"type":"tool","tool":"glob","state":{"status":"completed","input":{"pattern":"docs/**"},"output":"No files found"}}}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"text","part":{"type":"text","text":"Проверяю документацию."}}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":19856,"input":19793,"output":63,"reasoning":0},"cost":0.12}}\n' },
];
const presented = module.presentLogs(sample);
if (presented.length !== 2) throw new Error(`Expected two human-friendly entries, got ${presented.length}`);
if (presented[0].title !== "Ищу файлы" || presented[0].body !== "docs/**" || presented[0].detail !== "Ничего не найдено") throw new Error("Tool event was not summarized");
if (presented[1].title !== "Агент" || presented[1].body !== "Проверяю документацию.") throw new Error("Agent text was not presented as a message");
if (JSON.stringify(presented).includes("sessionID") || JSON.stringify(presented).includes("19856")) throw new Error("Service telemetry leaked into activity view");
const severity = module.presentLogs([
  { jobId: "generator", stream: "stderr", line: "[info] [1/1] checkout-form: loading registry metadata\n" },
  { jobId: "generator", stream: "stderr", line: "[warn] auditBootstrap is deprecated\n" },
  { jobId: "generator", stream: "stderr", line: "[error] registry unavailable\n" },
]);
if (severity.map((entry) => entry.kind).join(",") !== "raw,warning,error") throw new Error(`Generator stderr severity is wrong: ${JSON.stringify(severity)}`);
if (severity[0].title !== "Ход выполнения" || severity[1].title !== "Предупреждение") throw new Error("Generator severity titles are wrong");

const compactActivity = module.presentLogs([
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow heartbeat; elapsedSeconds=3667.6, checksStarted=10, maxChecks=24, currentUnits=2, wave=subsets/2, active=2, completed=0" },
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline yellow iteration 1 assignment abc: resolver-install: package-manager resolver install: running; elapsed=270s; hardTimeout=600s; pid=22120" },
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow shrink; elapsedSeconds=3592.5, checksStarted=8, maxChecks=24, currentUnits=2, reason=failing-subset, units=2, packages=46" },
  { jobId: "baseline", stream: "system", line: "demo-app: exact z3 yellow SUMMARY; components=26/26, changed=53, constraints=7190, refinements=0, elapsedMs=7488" },
]);
if (compactActivity.length !== 2) throw new Error(`Transient progress leaked into activity view: ${JSON.stringify(compactActivity)}`);
if (!compactActivity.some((entry) => entry.body.includes("shrink")) || !compactActivity.some((entry) => entry.body.includes("SUMMARY"))) throw new Error("Valuable stage/status events were filtered out");

const monitorCompiled = ts.transpileModule(monitorSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const monitorModule = await import(`data:text/javascript;base64,${Buffer.from(monitorCompiled).toString("base64")}`);
const monitor = monitorModule.deriveRunMonitor([
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow started; units=19, maxChecks=24, parallelism=4, attemptHardTimeout=3600s, localizationHardTimeout=7200s" },
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow shrink; elapsedSeconds=1019.3, checksStarted=2, maxChecks=24, currentUnits=10, reason=failing-subset, units=10, packages=64" },
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow shrink; elapsedSeconds=1820.0, checksStarted=4, maxChecks=24, currentUnits=5, reason=failing-subset, units=5, packages=58" },
  { jobId: "baseline", stream: "system", line: "demo-app: Baseline localization yellow heartbeat; elapsedSeconds=2060.3, checksStarted=6, maxChecks=24, currentUnits=5, wave=subsets/2, active=2, completed=0" },
], true, "baseline");
if (monitor.phase !== "localizing" || monitor.localization?.currentUnits !== 5 || monitor.localization?.checksStarted !== 6) throw new Error(`Localization monitor is wrong: ${JSON.stringify(monitor)}`);
if (monitor.localization?.activeChecks !== 2 || monitor.localization?.completedChecks !== 0) throw new Error(`Localization worker counters are wrong: ${JSON.stringify(monitor.localization)}`);
if (monitor.localization?.shrinkHistory.join(",") !== "19,10,5") throw new Error(`Localization shrink history is wrong: ${JSON.stringify(monitor.localization)}`);

const conversation = module.presentLogs([
  { jobId: "agent", stream: "system", line: "Вы → агент: Проверь ещё Storybook" },
  { jobId: "agent", stream: "stdout", line: '{"type":"text","part":{"type":"text","text":"Проверяю Storybook."}}\n' },
]);
if (conversation.length !== 2 || conversation[0].kind !== "user" || conversation[0].title !== "Вы" || conversation[0].body !== "Проверь ещё Storybook") throw new Error(`User chat message was not presented separately: ${JSON.stringify(conversation)}`);
if (conversation[1].kind !== "message" || conversation[1].title !== "Агент") throw new Error(`Agent reply did not remain a chat message: ${JSON.stringify(conversation)}`);
const groupSource = { kind: 'group', id: 'libs-group-2', label: 'Группа 2' };
const sourced = module.presentLogs([{ jobId: 'worker::group::libs-group-2', stream: 'stdout', source: groupSource, line: '{"type":"text","part":{"type":"text","text":"Работаю."}}\n' }]);
if (sourced[0]?.source?.id !== groupSource.id) throw new Error(`Group source metadata was lost in presentation: ${JSON.stringify(sourced)}`);
for (const required of ["t('log.allMessages')", "t('log.systemOrchestrator')", 'onSendAgentNote(message, addressedGroup.id)', 'sourceLabel(entry.source)']) {
  if (!panelSource.includes(required)) throw new Error(`Session-filtered chat contract missing: ${required}`);
}
for (const [localeSource, marker] of [
  [enLocaleSource, '"log.allMessages": "All messages"'],
  [ruLocaleSource, '"log.allMessages": "Все сообщения"'],
  [enLocaleSource, '"log.systemOrchestrator": "System / orchestrator"'],
  [ruLocaleSource, '"log.systemOrchestrator": "Система / оркестратор"'],
]) {
  if (!localeSource.includes(marker)) throw new Error(`Session-filter locale contract missing: ${marker}`);
}

const usage = module.summarizeTokenUsage(sample);
if (usage.total !== 19856 || usage.input !== 19793 || usage.output !== 63 || usage.cost !== 0.12) throw new Error(`Token usage was not summarized: ${JSON.stringify(usage)}`);

// Verified against a real OpenCode session's own sqlite-tracked totals: a
// step-finish's `tokens.total` already folds in cache reads/writes, but the
// old code only summed input/output/reasoning into the breakdown, so on a
// cache-heavy run the tooltip (input+output+reasoning) fell dramatically
// short of the headline total -- input 7.78M vs a 29.8M total in one real
// session, which read as broken math rather than legitimate cache reuse.
const cacheSample = [
  { jobId: "agent", stream: "stdout", line: '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":64450,"input":654,"output":308,"reasoning":0,"cache":{"write":0,"read":63488}},"cost":0}}\n' },
];
const cacheUsage = module.summarizeTokenUsage(cacheSample);
if (cacheUsage.cacheRead !== 63488) throw new Error(`Cache-read tokens were dropped: ${JSON.stringify(cacheUsage)}`);
if (cacheUsage.total !== cacheUsage.input + cacheUsage.output + cacheUsage.reasoning + cacheUsage.cacheRead + cacheUsage.cacheWrite) {
  throw new Error(`Headline total must equal the sum of its own tracked parts: ${JSON.stringify(cacheUsage)}`);
}
if (cacheUsage.total !== 64450) throw new Error(`Total must match the provider's own per-step total when cache is included: ${JSON.stringify(cacheUsage)}`);

const latest = module.latestJobId([
  { jobId: "system", stream: "system", line: "Запуск: agent" },
  { jobId: "job-1", stream: "stdout", line: "..." },
  { jobId: "download", stream: "system", line: "Сохранено: ..." },
  { jobId: "job-2", stream: "stdout", line: "..." },
]);
if (latest !== "job-2") throw new Error(`latestJobId must skip system/download markers and pick the real last job: ${latest}`);

const claudeSample = [
  { jobId: "agent", stream: "stdout", line: '{"type":"system","subtype":"init","session_id":"sid"}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"assistant","message":{"content":[{"type":"text","text":"Проверяю зависимости."}]}}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"npm test"}}]}}\n' },
  { jobId: "agent", stream: "stdout", line: '{"type":"result","subtype":"success","is_error":false,"result":"Готово.","usage":{"input_tokens":100,"output_tokens":40,"cache_read_input_tokens":500,"cache_creation_input_tokens":20},"total_cost_usd":0.05}\n' },
];
const claudePresented = module.presentLogs(claudeSample);
if (claudePresented.length !== 3) throw new Error(`Expected three human-friendly Claude entries, got ${claudePresented.length}`);
if (claudePresented[0].title !== "Агент" || claudePresented[0].body !== "Проверяю зависимости.") throw new Error("Claude assistant text was not presented as a message");
if (claudePresented[1].body !== "npm test") throw new Error("Claude tool_use was not summarized");
if (claudePresented[2].kind !== "message" || claudePresented[2].body !== "Готово.") throw new Error("Claude result event was not presented");
if (JSON.stringify(claudePresented).includes("sid")) throw new Error("Claude session id leaked into activity view");
const claudeUsage = module.summarizeTokenUsage(claudeSample);
if (claudeUsage.input !== 100 || claudeUsage.output !== 40 || claudeUsage.cost !== 0.05) throw new Error(`Claude token usage was not summarized: ${JSON.stringify(claudeUsage)}`);
if (claudeUsage.cacheRead !== 500 || claudeUsage.cacheWrite !== 20) throw new Error(`Claude cache tokens were dropped: ${JSON.stringify(claudeUsage)}`);
if (claudeUsage.total !== 660) throw new Error(`Claude total must include cache, not just input+output: ${JSON.stringify(claudeUsage)}`);

// A failed optional tool probe is still an activity, not proof that the job
// failed. The provider emits an explicit error/result event when the failure
// itself must be shown in red.
const failedReadActivity = module.presentLogs([
  { jobId: "planner", stream: "stdout", line: '{"type":"tool_use","part":{"type":"tool","tool":"read","state":{"status":"failed","input":{"path":"C:/Temp/.dependency-flow-planner-result.json"}}}}\n' },
]);
if (failedReadActivity.length !== 1 || failedReadActivity[0].kind !== "tool" || failedReadActivity[0].title !== "Читаю файл") throw new Error(`Informational read activity turned red: ${JSON.stringify(failedReadActivity)}`);

// Real, routine stderr noise from tools the orchestrator shells out to
// (git's own CRLF warning, a plain progress line) must not read as a failed
// step just because it landed on stderr and isn't one of our own
// [info]/[warn]/[error]-prefixed lines. Only text that actually reads like
// a problem should escalate.
const rawStderr = module.presentLogs([
  { jobId: "git", stream: "stderr", line: "warning: in the working copy of 'CHANGELOG.md', CRLF will be replaced by LF the next time Git touches it\n" },
  { jobId: "git", stream: "stderr", line: "Cloning into 'checkout-form'...\n" },
  { jobId: "git", stream: "stderr", line: "fatal: not a git repository\n" },
]);
if (rawStderr.map((entry) => entry.kind).join(",") !== "warning,raw,error") throw new Error(`Unprefixed stderr severity is wrong: ${JSON.stringify(rawStderr)}`);
console.log("OpenCode/Claude JSONL presentation OK");

