import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const hook = fs.readFileSync(path.join(root, 'src', 'hooks', 'useDependencyFlow.ts'), 'utf8')
const policy = fs.readFileSync(path.join(root, 'src', 'autopilot-policy.ts'), 'utf8')
const workspace = fs.readFileSync(path.join(root, 'src', 'components', 'FlowWorkspace.tsx'), 'utf8')
const main = fs.readFileSync(path.join(root, 'electron', 'main.ts'), 'utf8')
const promptHarness = fs.readFileSync(path.join(root, 'electron', 'prompt-harness.ts'), 'utf8')
const githubCi = fs.readFileSync(path.resolve(root, '..', '.github', 'workflows', 'ci.yml'), 'utf8')

const mustContain = (text, needle, label) => { if (!text.includes(needle)) throw new Error(`${label}: missing ${JSON.stringify(needle)}`) }
const mustNotContain = (text, needle, label) => { if (text.includes(needle)) throw new Error(`${label}: forbidden ${JSON.stringify(needle)}`) }

mustContain(policy, "export const AUTOPILOT_ORDER: FlowAction[] = ['preflight', 'baseline', 'agent', 'generate', 'audit', 'release', 'commit-state', 'push-workspace']", 'stage order')
mustContain(policy, "if (!verdict || verdict.status === 'UNKNOWN') return undefined", 'unknown acceptance is fail closed')
mustContain(policy, 'if (verdict.accepted)', 'accepted result proceeds')
mustContain(policy, "return 'agent'", 'hard acceptance failure reopens migration')
mustContain(policy, 'cumulative merged commit', 'progress authority')
mustNotContain(policy, 'lagOkPct', 'freshness is not autopilot progress authority')

mustContain(hook, 'publish: Boolean(project.git?.push)', 'publication remains opt-in')
mustContain(hook, "if (recovery?.kind === 'agent')", 'recoverable failures are intercepted')
mustContain(hook, "if (recovery?.kind === 'infrastructure')", 'bounded infrastructure recovery remains')
mustContain(hook, 'MAX_AUTOPILOT_INFRA_RETRIES = 3', 'infrastructure retry bound')
mustContain(hook, 'MAX_AUTOPILOT_RECOVERY_CYCLES = 8', 'recovery budget')
mustContain(hook, 'goalSeekingStopReason', 'semantic no-progress guard remains')

for (const sentinel of [
  'runAutonomousMigrationStage(job)',
  'runReleaseRecoveryAgent(job, issue)',
  'AGENT_BRANCH_SCOPE_VIOLATION',
  'clearPlannerDeferrals(workspace, project.name)',
  'cleanupSupersededMigrationAfterBaseline',
  'ACCEPTANCE_REMEDIATION_REQUIRED',
  'ACCEPTANCE_NOT_SATISFIED',
  'ACCEPTANCE_AUTHORITY_INVALIDATED_DURING_RELEASE',
  'Acceptance remediation имеет приоритет над freshness residual',
]) mustContain(main, sentinel, 'backend autonomy/safety contract')
mustNotContain(main, 'Supervisor goal-seeking', 'Yellow goal-seeking removed')
mustNotContain(main, 'GOAL_CLOSURE_PROPOSAL_INSUFFICIENT', 'full-Yellow correction removed')

mustContain(workspace, "text('Acceptance', 'Acceptance')", 'acceptance UX')
mustContain(workspace, "text('Freshness', 'Freshness')", 'freshness is separate')
mustNotContain(workspace, 'target-field', 'Yellow/Green selector removed')
mustContain(workspace, 'AUTOPILOT_HELP[language]', 'autopilot explanation')
mustContain(promptHarness, 'PROMPT_HARNESS_TIMEOUT', 'prompt export timeout remains')
mustContain(githubCi, 'npm run check:autopilot-scenarios', 'scenario matrix remains mandatory in CI')
mustContain(githubCi, 'npm run check:acceptance-policy', 'acceptance policy remains mandatory in CI')
mustContain(githubCi, 'npm run check:progressive-contract', 'progressive source contract remains mandatory in CI')

console.log('Progressive Autopilot contract OK')
