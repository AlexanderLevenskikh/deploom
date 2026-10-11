#!/usr/bin/env node
// C3 acceptance: an idle poll must not re-run the expensive ref-containment and
// per-branch package-facts grid, and a moved ref target (same name, new object
// id) must invalidate that grid instead of riding out the TTL window.
//
// readMigrationProgress lives inside main.ts and cannot be imported headlessly,
// so this check replicates its exact git query profile (cheap reads -> cheap
// fingerprint gate -> expensive per-ref `for-each-ref --contains` + `git show`
// grid) against a REAL git repository and measures the actual subprocess call
// counts across polls. It proves:
//   1. GIT_FACTS_CACHE_TTL_MS covers the poll intervals (3s active, 10s idle).
//   2. A second poll with unchanged Git state issues ZERO expensive calls.
//   3. Advancing a ref's object id re-issues the expensive grid on the next
//      poll even though the ref NAME is unchanged (OID invalidation).
//   4. After the TTL expires the grid re-runs even on an unchanged fingerprint.
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync, readFileSync, existsSync, statSync } from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
import * as migration from '../dist-electron/migration-progress.js';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { GIT_FACTS_CACHE_TTL_MS, liveGitWorktreeRecords, migrationFactsFingerprint, relevantGitStatusLines } from '../dist-electron/migration-progress.js';

// Ref names whose containment/package-facts grid readMigrationProgress computes.
const PLANNED_REFS = ['deps-group-1', 'libs-merged', 'main'];
const MANIFEST_PATHS = ['package.json'];

// --- instrumented git runner: counts every subprocess call, and separately ---
// --- the expensive containment/show calls that the cache must suppress      ---
function makeGit(cwd, counts) {
  return (args) => {
    counts.total += 1;
    const isExpensive = args[0] === 'show'
      || (args[0] === 'for-each-ref' && args.some((a) => a.startsWith('--contains=')));
    if (isExpensive) counts.expensive += 1;
    const result = spawnSync('git', ['-C', cwd, ...args], { encoding: 'utf8' });
    return { code: result.status, stdout: result.stdout ?? '', stderr: result.stderr ?? '' };
  };
}

function gitOk(result, what) {
  if (result.code !== 0) throw new Error(`git ${what} failed: ${result.stderr || result.stdout}`);
  return result;
}

// Mirrors the cheap-read + fingerprint-gate + expensive-grid shape of
// readMigrationProgress in main.ts (prompt parsing is irrelevant here).
async function pollFacts({ cwd, git, cache, ttl }) {
  const [refsResult, branchResult, statusResult, worktreeResult, headResult] = await Promise.all([
    git(['for-each-ref', '--format=%(refname:short)%09%(objectname)', 'refs/heads', 'refs/remotes/origin']),
    git(['branch', '--show-current']),
    git(['status', '--porcelain']),
    git(['worktree', 'list', '--porcelain']),
    git(['rev-parse', 'HEAD']),
  ]);
  const refRows = refsResult.stdout.split(/\r?\n/).map((row) => row.trim()).filter(Boolean);
  const refs = refRows.map((row) => row.split('\t')[0]);
  const refOids = refRows.map((row) => row.split('\t')[1] ?? '');
  const fingerprint = migrationFactsFingerprint({
    projectPath: cwd,
    head: headResult.stdout.trim(),
    branch: branchResult.stdout.trim(),
    refs,
    refOids,
    statusLines: relevantGitStatusLines(statusResult.stdout),
    worktrees: worktreeResult.stdout,
    promptMtimeMs: 123,
    promptSize: 456,
  });
  const cached = cache.get(cwd);
  if (cached && cached.fingerprint === fingerprint && Date.now() - cached.takenAt < ttl) {
    return { fromCache: true, fingerprint, refs };
  }
  // Expensive grid: the per-ref containment and manifest probes that the cache
  // must suppress on unchanged cheap state (mirrors readMigrationProgress).
  let probes = 0;
  for (const planRef of PLANNED_REFS) {
    for (const manifestPath of MANIFEST_PATHS) {
      const containing = gitOk(await git(['for-each-ref', `--contains=${planRef}`, '--format=%(objectname) %(refname:short)', 'refs/heads', 'refs/remotes/origin']), 'for-each-ref --contains');
      probes += containing.stdout.split(/\r?\n/).map((r) => r.trim()).filter(Boolean).length;
      const manifest = await git(['show', `refs/heads/${planRef}:${manifestPath}`]);
      if (manifest.code !== 0) {
        const records = liveGitWorktreeRecords(worktreeResult.stdout);
        if (records.length) probes += 1;
      } else {
        probes += 1;
      }
    }
  }
  cache.set(cwd, { fingerprint, takenAt: Date.now() });
  return { fromCache: false, fingerprint, refs, probes };
}

const repo = mkdtempSync(join(tmpdir(), 'deploom-gitfacts-'));
const git = (args, cwd = repo) => {
  const r = spawnSync('git', ['-C', cwd, ...args], { encoding: 'utf8' });
  if (r.error) throw r.error;
  return { code: r.status, stdout: r.stdout ?? '', stderr: r.stderr ?? '' };
};
const ok = (r, what) => { if (r.code !== 0) throw new Error(`git ${what}: ${r.stderr || r.stdout}`); return r };

try {
  // Build a real repository whose refs make the containment grid meaningful:
  // main, plus deps-group-1 merged into libs-merged.
  ok(git(['init', '-b', 'main']), 'init');
  ok(git(['config', 'user.email', 'c3-test@example.com']), 'config user.email');
  ok(git(['config', 'user.name', 'C3 Test']), 'config user.name');
  writeFileSync(join(repo, 'package.json'), '{"name":"fixture","dependencies":{}}\n');
  writeFileSync(join(repo, 'README.md'), 'seed\n');
  ok(git(['add', '-A']), 'add seed');
  ok(git(['commit', '-m', 'c1']), 'commit c1');
  ok(git(['branch', 'deps-group-1']), 'branch deps-group-1');
  ok(git(['checkout', '-q', 'deps-group-1']), 'checkout deps-group-1');
  writeFileSync(join(repo, 'package.json'), '{"name":"fixture","dependencies":{"dep":"2.0.0"}}\n');
  ok(git(['add', '-A']), 'add deps');
  ok(git(['commit', '-m', 'c2 deps']), 'commit c2');
  ok(git(['checkout', '-q', 'main']), 'checkout main');
  ok(git(['checkout', '-q', '-b', 'libs-merged']), 'checkout -b libs-merged');
  ok(git(['merge', '-q', 'deps-group-1', '-m', 'm1']), 'merge deps-group-1');
  ok(git(['checkout', '-q', 'main']), 'checkout main');
  writeFileSync(join(repo, 'README.md'), 'seed\nmain ahead\n');
  ok(git(['add', '-A']), 'add main ahead');
  ok(git(['commit', '-m', 'c3 main ahead']), 'commit c3');

  // Assertion 1: the TTL constant must cover both poll intervals.
  if (GIT_FACTS_CACHE_TTL_MS < 10_000) {
    throw new Error(`GIT_FACTS_CACHE_TTL_MS=${GIT_FACTS_CACHE_TTL_MS} must be >= 10000 idle poll interval`);
  }

  const counts = { total: 0, expensive: 0 };
  const runGit = makeGit(repo, counts);
  const cache = new Map();

  // Phase 1: cold poll runs the full grid.
  const first = await pollFacts({ cwd: repo, git: runGit, cache, ttl: GIT_FACTS_CACHE_TTL_MS });
  if (first.fromCache) throw new Error('First poll must compute the grid');
  if (counts.expensive === 0) throw new Error('First poll must issue expensive containment/show calls');
  const expensiveAfterFirst = counts.expensive;
  const totalAfterFirst = counts.total;

  // Phase 2: unchanged Git state within TTL -> cheap reads only, zero expensive calls.
  const second = await pollFacts({ cwd: repo, git: runGit, cache, ttl: GIT_FACTS_CACHE_TTL_MS });
  if (!second.fromCache) throw new Error('Second identical poll must hit the facts cache');
  if (second.fingerprint !== first.fingerprint) throw new Error('Identical polls must share one fingerprint');
  if (counts.expensive !== expensiveAfterFirst) {
    throw new Error(`Idle poll re-ran the expensive grid: expensive went ${expensiveAfterFirst} -> ${counts.expensive}`);
  }
  if (counts.total === totalAfterFirst) throw new Error('Cheap reads must still run on a cached poll to build the fingerprint');
  const expensiveAfterSecond = counts.expensive;

  // Phase 3: advance a ref target (same NAME, new OID) -> fingerprint changes,
  // the very next poll re-runs the grid even inside the TTL window.
  ok(git(['checkout', '-q', 'deps-group-1']), 'checkout deps-group-1');
  writeFileSync(join(repo, 'package.json'), '{"name":"fixture","dependencies":{"dep":"2.1.0"}}\n');
  ok(git(['add', '-A']), 'add dep bump');
  ok(git(['commit', '-m', 'c4 move deps-group-1 target']), 'commit c4');
  ok(git(['checkout', '-q', 'main']), 'checkout main');
  const third = await pollFacts({ cwd: repo, git: runGit, cache, ttl: GIT_FACTS_CACHE_TTL_MS });
  if (third.fromCache) throw new Error('Moved ref OID must invalidate the cache');
  if (third.fingerprint === second.fingerprint) throw new Error('Moved ref OID must change the fingerprint');
  if (counts.expensive <= expensiveAfterSecond) {
    throw new Error(`Moved ref OID must re-run the grid (expensive stayed ${expensiveAfterSecond})`);
  }

  // Phase 4: TTL expiry re-runs the grid even on an unchanged fingerprint.
  const shortCounts = { total: 0, expensive: 0 };
  const runGitShort = makeGit(repo, shortCounts);
  const shortCache = new Map();
  const ttlShort = 60;
  const pre = await pollFacts({ cwd: repo, git: runGitShort, cache: shortCache, ttl: ttlShort });
  const expensivePre = shortCounts.expensive;
  const shortGap = await new Promise((resolve) => setTimeout(resolve, ttlShort + 40));
  const post = await pollFacts({ cwd: repo, git: runGitShort, cache: shortCache, ttl: ttlShort });
  if (post.fingerprint !== pre.fingerprint) throw new Error('Phase 4 fixture changed unexpectedly');
  if (shortGap < ttlShort) throw new Error('Phase 4 wait did not outlive TTL');
  if (post.fromCache) throw new Error('An expired-cache poll must recompute');
  if (shortCounts.expensive <= expensivePre) throw new Error('Expired TTL must re-run the grid');

  // Exercise the actual production reader, with a real Git repo and real
  // worktree files. Electron-only persistence/runtime services are injected.
  ok(git(['update-ref', 'refs/heads/libs-merged', 'HEAD']), 'reset merged fixture');
  const worker = join(repo, 'worker');
  writeFileSync(join(repo, '.git', 'info', 'exclude'), '/worker/\n');
  ok(git(['worktree', 'add', worker, 'deps-group-1']), 'add worker');
  const source = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8');
  const begin = source.indexOf('async function readMigrationProgress(');
  const end = source.indexOf('\nasync function mergeInProgress(', begin);
  assert.ok(begin >= 0 && end > begin);
  const readerCode = ts.transpileModule(source.slice(begin, end), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  let completed = [];
  let phase = 'agent';
  let failStatus = false;
  let moveDuringProbe = false;
  const realCounts = { total: 0, expensive: 0 };
  const realGit = makeGit(repo, realCounts);
  const prodPlan = { project: 'fixture', baseBranch: 'main', mergedBranch: 'libs-merged', branches: [{ branch: 'deps-group-1', label: 'dep', packages: ['dep'] }] };
  const context = {
    ...migration, Map, Set, Date, JSON, Object, Boolean, Promise, createHash,
    GIT_QUERY_TIMEOUT_MS: 1000,
    migrationFactsCache: new Map(),
    promptPathForProject: () => join(repo, 'README.md'),
    existsSync, readFileSync, statSync, join,
    migrationPlanFromPrompt: () => prodPlan,
    spawnCapture: async (_command, args, cwd) => {
      if (failStatus && args.includes('status')) return { code: 1, stdout: '', timedOut: false };
      if (cwd === repo) {
        if (moveDuringProbe && args[2] === 'show') {
          moveDuringProbe = false;
          ok(git(['update-ref', 'refs/heads/main', 'refs/heads/deps-group-1']), 'race fixture ref move');
        }
        return realGit(args.slice(2));
      }
      return git(args.slice(2), cwd);
    },
    resolveProjectGitLayout: async () => ({ packageRelativePath: '' }),
    projectTreePath: (_relative, path) => path,
    projectPathInWorktree: (path, relative) => join(path, relative),
    portablePathKey: (path) => resolve(path).toLowerCase(),
    satisfiedScopePackagesFromPrompt: (_prompt, _name, pkg) => new Set(Object.keys(pkg?.dependencies ?? {})),
    readTeamState: () => ({ projects: { fixture: { executionContractVersion: 2, completedMigrationBranches: completed } } }),
    migrationRuntime: () => ({ 'deps-group-1': { phase, updatedAt: 'now' } }),
  };
  vm.createContext(context);
  vm.runInContext(readerCode, context);
  const project = { name: 'fixture', path: repo };
  const readPoll = () => context.readMigrationProgress({}, project, { cacheImmutableFacts: true });
  const initial = await readPoll();
  assert.equal(initial.branches[0].status, 'partial');
  const coldProbes = realCounts.expensive;
  completed = ['deps-group-1'];
  phase = 'ready';
  const live = await readPoll();
  assert.equal(live.branches[0].status, 'ready');
  assert.equal(live.branches[0].runtime.phase, 'ready');
  assert.equal(realCounts.expensive, coldProbes, 'UI poll must reuse only immutable facts');
  writeFileSync(join(worker, 'package.json'), '{"name":"fixture","dependencies":{"dep":"2.2.0"}}\n');
  const dirtyWorker = await readPoll();
  assert.ok(dirtyWorker.branches[0].worktreeDirtyChanges > 0, 'secondary worktree dirty state must stay fresh on a cache hit');
  assert.equal(realCounts.expensive, coldProbes);
  failStatus = true;
  const failedRead = await readPoll();
  assert.equal(failedRead.trustworthy, false, 'failed cheap evidence cannot return a trustworthy cached progress');
  failStatus = false;
  const probesBeforeGuard = realCounts.expensive;
  await context.readMigrationProgress({}, project);
  assert.ok(realCounts.expensive > probesBeforeGuard, 'authoritative guard must bypass the UI cache');
  moveDuringProbe = true;
  const raced = await context.readMigrationProgress({}, project);
  assert.equal(raced.trustworthy, false, 'moving refs during a grid must reject mixed evidence');
  console.log('Production Git reader: fresh completion/runtime/worktree/failure evidence, authoritative cache bypass and ref-race guard OK');

  console.log(`Git facts cache OK: TTL=${GIT_FACTS_CACHE_TTL_MS}ms; cold=${expensiveAfterFirst} expensive calls, ` +
    `unchanged poll added ${counts.total - totalAfterFirst} cheap calls and 0 expensive, ` +
    `ref OID move re-ran ${counts.expensive - expensiveAfterSecond} expensive, expiry recomputed ${shortCounts.expensive}.`);
} finally {
  if (repo.startsWith(tmpdir())) rmSync(repo, { recursive: true, force: true });
}
