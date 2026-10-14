import assert from 'node:assert/strict';
import { appendFileSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { emptyJournalView, applyJournalPage, clearJournalView } from '../dist-electron/project-journal-view.js';
import { startAttempt, recordAttemptLog } from '../dist-electron/iterative-attempt.js';
import { spawnIterativeStreamed } from '../dist-electron/iterative-stream.js';
import { appendJournalRecord, readJournalPage, JOURNAL_FILENAME } from '../dist-electron/project-journal.js';

const root = mkdtempSync(join(tmpdir(), 'deploom-journal-'));
const context = { workspaceId: 'fixture-workspace', projectName: 'Demo', runId: 'run-one', attemptId: 'attempt-one' };
const record = text => ({ ...context, stream: 'stdout', source: 'materialize', text, at: 123 });
try {
  const empty = readJournalPage(root);
  assert.deepEqual(empty.events, []);
  appendJournalRecord(root, record('same output'));
  appendJournalRecord(root, record('same output'));
  const initial = readJournalPage(root);
  assert.equal(initial.events.length, 2);
  assert.notEqual(initial.events[0].id, initial.events[1].id, 'real repetitions are not text-deduplicated');
  assert.deepEqual(initial.events.map(event => event.attemptId), ['attempt-one', 'attempt-one']);
  assert.equal(readJournalPage(root, initial.cursor).events.length, 0, 'replay/live invalidation overlap adds nothing twice');

  const huge = ('line \uD83D\uDE80 \u0416\n').repeat(50000);
  appendJournalRecord(root, { ...record(huge), stream: 'stderr', attemptId: 'attempt-two' });
  let cursor = initial.cursor;
  const recovered = [];
  for (let reads = 0; reads < 100; reads++) {
    const page = readJournalPage(root, cursor);
    assert.equal(page.reset, false);
    assert.ok(page.events.length <= 800);
    if (!page.events.length) break;
    assert.ok(page.events.every(event => event.stream === 'stderr' && event.attemptId === 'attempt-two'));
    assert.equal(new Set(page.events.map(event => event.id)).size, page.events.length);
    recovered.push(...page.events.map(event => event.text));
    cursor = page.cursor;
  }
  assert.equal(recovered.join(''), huge, 'large Unicode chunks remain lossless across bounded pages');
  const tail = readJournalPage(root);
  assert.equal(tail.truncated, true);
  assert.ok(tail.events.length < recovered.length, 'cold history reads only a bounded tail');
  assert.equal(readFileSync(join(root, JOURNAL_FILENAME), 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line).text).join(''), 'same outputsame output' + huge, 'full durable export is not a tail');

  const partial = JSON.stringify(record('partial append'));
  appendFileSync(join(root, JOURNAL_FILENAME), partial.slice(0, 20));
  const waiting = readJournalPage(root, cursor);
  assert.equal(waiting.events.length, 0);
  assert.equal(waiting.cursor.offset, cursor.offset, 'incomplete append must not advance cursor');
  appendFileSync(join(root, JOURNAL_FILENAME), partial.slice(20) + '\n');
  const completed = readJournalPage(root, waiting.cursor);
  assert.deepEqual(completed.events.map(event => event.text), ['partial append']);

  const old = completed.cursor;
  renameSync(join(root, JOURNAL_FILENAME), join(root, 'old.ndjson'));
  writeFileSync(join(root, JOURNAL_FILENAME), '');
  appendJournalRecord(root, { ...record('new run'), runId: 'run-two' });
  const replaced = readJournalPage(root, old);
  assert.equal(replaced.reset, true);
  assert.notEqual(replaced.cursor.epoch, old.epoch);
  assert.equal(replaced.events[0].runId, 'run-two');
  appendFileSync(join(root, JOURNAL_FILENAME), '{broken}\n');
  appendJournalRecord(root, record('after malformed record'));
  const malformed = readJournalPage(root, replaced.cursor);
  assert.deepEqual(malformed.events.map(event => event.text), ['after malformed record']);
  // Exercise the registered production handlers with real scoped filesystem data.
  const { mkdirSync, symlinkSync } = await import('node:fs');
  const { registerProjectJournalHandlers } = await import('../dist-electron/project-journal-ipc.js');
  const { listJournalSources } = await import('../dist-electron/project-journal-catalog.js');
  const ws = join(root, 'workspace');
  const iteration = join(ws, '.dependency-roadmap', 'iterative', 'demo');
  mkdirSync(iteration, { recursive: true });
  writeFileSync(join(iteration, 'run.json'), JSON.stringify({ runId: context.runId, projectName: context.projectName }));
  writeFileSync(join(iteration, 'attempt.json'), JSON.stringify({ ...context, status: 'done', startedAt: 100 }));
  writeFileSync(join(iteration, 'run.log'), 'historical raw output\n');
  appendJournalRecord(iteration, record('scoped event'));
  appendJournalRecord(iteration, { ...record('foreign event'), workspaceId: 'other-workspace' });
  const runs = join(ws, '.dependency-roadmap', 'artifacts', 'runs');
  const flowDir = join(runs, 'flow-one');
  mkdirSync(flowDir, { recursive: true });
  writeFileSync(join(flowDir, 'run.json'), JSON.stringify({ workspaceId: context.workspaceId, projectName: context.projectName, jobId: 'flow-one', action: 'draft' }));
  writeFileSync(join(flowDir, 'activity.log'), '[2026-01-01T00:00:00Z] [stderr] same output\n[2026-01-01T00:00:00Z] [stderr] same output\n');
  const foreignDir = join(runs, 'foreign');
  mkdirSync(foreignDir);
  writeFileSync(join(foreignDir, 'run.json'), JSON.stringify({ workspaceId: 'other-workspace', projectName: context.projectName, jobId: 'foreign' }));
  writeFileSync(join(foreignDir, 'activity.log'), 'must not leak');
  const workspaceEvent = join(runs, 'workspace-event');
  mkdirSync(workspaceEvent);
  writeFileSync(join(workspaceEvent, 'run.json'), JSON.stringify({ workspaceId: context.workspaceId, jobId: 'workspace-event' }));
  writeFileSync(join(workspaceEvent, 'activity.log'), 'workspace event');
  symlinkSync(foreignDir, join(runs, 'linked'), process.platform === 'win32' ? 'junction' : 'dir');
  const scope = { ...context, workspacePath: ws, iterativeDirectory: iteration };
  const handlers = new Map();
  let exportPath = join(root, 'full-export.ndjson');
  registerProjectJournalHandlers({ handle: (channel, handler) => handlers.set(channel, handler) }, {
    scope: input => { assert.equal(input.projectName, context.projectName); return scope; },
    savePath: async () => exportPath,
  });
  const catalog = handlers.get('flow:journal:list')(undefined, context).sources;
  assert.ok(catalog.every(source => !('directory' in source)), 'internal lookup paths are not a renderer selector');
  assert.ok(catalog.every(source => !['foreign', 'workspace-event'].includes(source.runId)));
  const structured = catalog.find(source => source.kind === 'iterative' && source.format === 'events');
  const input = { ...context, sourceId: structured.id };
  const page = handlers.get('flow:journal:read')(undefined, input);
  assert.deepEqual(page.events.map(event => event.text), ['scoped event']);
  assert.throws(() => handlers.get('flow:journal:read')(undefined, { ...input, sourceId: '../foreign' }), /NOT_OWNED/);
  const flowSource = catalog.find(source => source.runId === 'flow-one');
  const legacy = handlers.get('flow:journal:read')(undefined, { ...input, sourceId: flowSource.id });
  assert.equal(legacy.events.length, 2);
  assert.ok(legacy.events.every(event => event.stream === 'stderr'));
  assert.notEqual(legacy.events[0].id, legacy.events[1].id);
  appendJournalRecord(iteration, record('full export '.repeat(50000)));
  const exported = await handlers.get('flow:journal:export')(undefined, input);
  assert.equal(exported.canceled, false);
  assert.deepEqual(readFileSync(exportPath), readFileSync(join(iteration, JOURNAL_FILENAME)), 'export copies the entire durable artifact');
  exportPath = undefined;
  assert.equal((await handlers.get('flow:journal:export')(undefined, input)).canceled, true);
  assert.equal(listJournalSources({ ...scope, workspaceId: 'other-workspace' }).some(source => source.runId === 'flow-one'), false);
  // Exercise the real child-to-attempt-to-canonical-file boundary, then a
  // second attempt in the same directory. No text-based deduplication.
  const childDir = join(ws, '.dependency-roadmap', 'iterative', 'child');
  const firstAttempt = startAttempt(childDir, context.projectName, 'none', undefined, 0, context.workspaceId);
  const childResult = await spawnIterativeStreamed(childDir, process.execPath, ['-e', "process.stdout.write('repeated\\nrepeated\\n');process.stderr.write('diagnostic\\n')"], root, 10000, {
    cancelRequested: () => false, recordLine: recordAttemptLog, trimLog: () => {},
  }, {
    processTreeDetached: () => false, commandEnvironment: env => env,
    resolveSpawnInvocation: (command, args) => ({ command, args }),
    killProcessTree: child => child.kill(), decodeChunk: chunk => chunk.toString('utf8'),
  });
  assert.equal(childResult.code, 0);
  const secondAttempt = startAttempt(childDir, context.projectName, 'none', undefined, 0, context.workspaceId);
  recordAttemptLog(childDir, 'next attempt\n', 'stdout');
  const attemptScope = { ...scope, iterativeDirectory: childDir };
  const attemptSources = listJournalSources(attemptScope);
  const firstSource = attemptSources.find(source => source.startOffset !== undefined && source.attemptId === firstAttempt.attemptId);
  const secondSource = attemptSources.find(source => source.startOffset !== undefined && source.attemptId === secondAttempt.attemptId);
  assert.ok(firstSource && secondSource);
  const firstPage = readJournalPage(childDir, undefined, firstSource);
  assert.equal(firstPage.events.filter(event => event.stream === 'stdout').map(event => event.text).join('').split('\n').filter(line => line === 'repeated').length, 2, 'chunk batching preserves both identical real lines');
  assert.ok(firstPage.events.some(event => event.stream === 'stderr'));
  assert.ok(firstPage.events.every(event => event.attemptId === firstAttempt.attemptId));
  assert.equal(readJournalPage(childDir, undefined, secondSource).events[0].text, 'next attempt\n');
  const attemptHandlers = new Map();
  const attemptExport = join(root, 'selected-attempt.ndjson');
  registerProjectJournalHandlers({handle:(channel,handler)=>attemptHandlers.set(channel,handler)}, {scope:()=>attemptScope,savePath:async()=>attemptExport});
  await attemptHandlers.get('flow:journal:export')(undefined, {...context,sourceId:firstSource.id});
  assert.deepEqual(readFileSync(attemptExport),readFileSync(firstSource.artifactPath).subarray(firstSource.startOffset,firstSource.endOffset),'selected attempt export covers exactly its owned bytes');
  let view = emptyJournalView(context.workspaceId, context.projectName, firstSource.id, 7);
  const reply = { ...firstPage, sourceId: firstSource.id };
  view = applyJournalPage(view, { sourceId:firstSource.id, generation:7, sequence:1 }, reply);
  assert.equal(applyJournalPage(view, { sourceId:firstSource.id, generation:6, sequence:2 }, reply), view, 'late scope page ignored');
  const replayed = applyJournalPage(view, { sourceId:firstSource.id, generation:7, sequence:2 }, { ...reply, reset:false });
  assert.equal(replayed.events.length, view.events.length, 'overlap uses event identity');
  const cleared = clearJournalView(replayed);
  assert.equal(cleared.events.length, 0); assert.deepEqual(cleared.cursor, replayed.cursor);
  assert.equal(readJournalPage(childDir, undefined, firstSource).events.length, firstPage.events.length, 'clear view preserves disk');
  console.log('Project journal: real FS, stable offset identities, replay overlap, repeated text, Unicode fragments, bounded reads, full persistence and file replacement PASS');
} finally {
  assert.equal(dirname(root), resolve(tmpdir()));
  assert.ok(root.startsWith(join(resolve(tmpdir()), 'deploom-journal-')));
  rmSync(root, { recursive: true, force: true });
}
