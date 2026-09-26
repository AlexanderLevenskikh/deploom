#!/usr/bin/env node
// A3 contract: baselineFailureMessage may promise a "partially ready Draft
// plan" ONLY when a fresh accepted manifest exists (draftPublishable), never
// from the error envelope alone. Fast/Deep/Verified runs get an honest "no new
// verified result" message; budget is checked before unknown so a budget
// envelope is not swallowed by the code-only unknown branch.
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../electron/baseline-failure.ts', import.meta.url), 'utf8')
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

const module_ = { exports: {} }
new Function('module', 'exports', 'require', output)(module_, module_.exports, () => ({}))
const { baselineFailureMessage } = module_.exports

const envelope = (code, category, summary = '') => JSON.stringify({
  schemaVersion: 'DEPLOOM_FAILURE_V2',
  code,
  category,
  summary,
  diagnosticArtifact: '',
})

const result = (code, category, summary) => ({
  code: 3,
  stderr: `[error] baseline stopped\n${envelope(code, category, summary)}\n`,
  stdout: '',
})

let failed = 0
const check = (label, cond) => {
  if (!cond) { console.error(`check-baseline-failure-message: FAIL ${label}`); failed += 1 }
}

// Fast/Deep/Verified: unknown must never promise a Draft plan.
for (const mode of ['FAST', 'DEEP', 'EXHAUSTIVE']) {
  const msg = baselineFailureMessage(result('EXACT_SOLVER_UNKNOWN', 'SOLVER_UNKNOWN', 'component=a unknown'), { mode })
  check(`unknown ${mode} does not promise a plan`, !msg.toLowerCase().includes('черновой план готов частично'))
  check(`unknown ${mode} states no new result`, msg.includes('Новый проверенный результат не получен'))
}
// Same without options at all (legacy call shape).
const bare = baselineFailureMessage(result('EXACT_SOLVER_UNKNOWN', 'SOLVER_UNKNOWN'))
check('unknown defaults to no-plan wording', !bare.toLowerCase().includes('черновой план готов частично') && bare.toLowerCase().includes('новый проверенный результат не получен'))

// Budget in Fast/Deep: no plan promise, states no new result, honest budget.
const budget = baselineFailureMessage(result('EXACT_SOLVER_BUDGET_EXHAUSTED', 'SOLVER_BUDGET_EXHAUSTED', 'registry refinement budget exhausted'))
check('budget fast/deep no plan promise', !budget.toLowerCase().includes('черновой план готов частично'))
check('budget fast/deep no new result', budget.toLowerCase().includes('новый проверенный результат не получен'))
check('budget text kept', budget.includes('бюджет'))

// Budget envelope with code=EXACT_SOLVER_UNKNOWN (category priority) reaches the
// budget branch, not the unknown one.
const budgetByCategory = baselineFailureMessage(result('EXACT_SOLVER_UNKNOWN', 'SOLVER_BUDGET_EXHAUSTED', 'component=timeout'))
check('budget category wins over unknown code', budgetByCategory.includes('Не удалось завершить') && !budgetByCategory.toLowerCase().includes('черновой план готов частично'))

// Draft with a genuinely published fresh manifest may say "ready partially".
const draftReady = baselineFailureMessage(result('EXACT_SOLVER_UNKNOWN', 'SOLVER_UNKNOWN'), { mode: 'DRAFT', draftPublishable: true })
check('draft publishable says plan ready', draftReady.toLowerCase().includes('черновой план готов частично'))
const draftReadyBudget = baselineFailureMessage(result('EXACT_SOLVER_BUDGET_EXHAUSTED', 'SOLVER_BUDGET_EXHAUSTED'), { mode: 'DRAFT', draftPublishable: true })
check('draft publishable budget keeps plan ready', draftReadyBudget.toLowerCase().includes('черновой план готов частично'))

// Draft error BEFORE publication: no plan promise even in DRAFT mode.
const draftBeforePublish = baselineFailureMessage(result('EXACT_SOLVER_UNKNOWN', 'SOLVER_UNKNOWN', 'no artifact yet'), { mode: 'DRAFT', draftPublishable: false })
check('draft before publish no plan promise', !draftBeforePublish.toLowerCase().includes('черновой план готов частично') && draftBeforePublish.toLowerCase().includes('новый проверенный результат не получен'))

// Stale/old artifact (older runId) must not surface as the new error result:
// draftPublishable=false text must not reference an old prompt/draft path.
check('stale artifact never presented as result', !draftBeforePublish.includes('prompt.md') && !draftBeforePublish.includes('Откройте план'))

// Unchanged branches still work.
const unsat = baselineFailureMessage(result('EXACT_SOLVER_UNSAT_PROVEN', 'EXACT_UNSAT_PROVEN'))
check('unsat branch intact', unsat.includes('доказал'))
check('exit 0 returns undefined', baselineFailureMessage({ code: 0, stderr: '', stdout: '' }) === undefined)

if (failed > 0) {
  console.error(`check-baseline-failure-message: ${failed} assertion(s) failed`)
  process.exit(1)
}
console.log('check-baseline-failure-message: OK (A3 draft-ready claims gated on accepted manifest)')
