import { teamStatePaths, teamStatePathspecs } from "../dist-electron/state-commit.js";

const paths = teamStatePaths(".dependency-roadmap/settings.project.json", {
  groupsConfig: ".team/groups.json",
  historyDir: ".team/history",
  dashboardState: ".team/dashboard.json",
});
for (const expected of [".dependency-roadmap/desktop/flow-state.json", ".dependency-roadmap/settings.project.json", ".team/groups.json", ".team/history", ".team/dashboard.json", "knowledge"]) {
  if (!paths.includes(expected)) throw new Error(`Missing team state path: ${expected}`);
}
if (paths.some((value) => value.includes("desktop/downloads"))) throw new Error(`Local downloads leaked into state paths: ${paths}`);
if (paths.length !== new Set(paths).size) throw new Error(`Duplicate state paths: ${paths}`);
console.log("Team state path selection OK");
// A real Git index can already contain runtime data or unrelated user work.
// The production scoped add/commit must preserve it without publishing it.
const assert = (await import('node:assert/strict')).default;
const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = await import('node:fs');
const { tmpdir } = await import('node:os');
const { join } = await import('node:path');
const { execFileSync } = await import('node:child_process');
const root = mkdtempSync(join(tmpdir(), 'deploom-state-commit-'));
const git = (...args) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', env: { ...process.env, GIT_CONFIG_GLOBAL: process.platform === 'win32' ? 'NUL' : '/dev/null', GIT_CONFIG_NOSYSTEM: '1' } }).trim();
const write = (name, text) => { const target = join(root, name); mkdirSync(join(target, '..'), {recursive:true}); writeFileSync(target, text) };
try {
  git('init'); git('config','user.name','State test'); git('config','user.email','state@example.invalid');
  write('.dependency-roadmap/settings.project.json', '{}');
  write('.dependency-roadmap/history/report.md', 'before');
  write('.dependency-roadmap/history/cache/source.bin', 'tracked runtime before');
  write('.dependency-roadmap/iterative/demo/sources/C0/payload.bin', 'tracked snapshot before');
  write('personal.txt', 'before'); git('add','-A'); git('commit','-m','initial');
  write('.dependency-roadmap/history/report.md', 'after');
  write('.dependency-roadmap/history/cache/source.bin', 'tracked runtime after');
  write('.dependency-roadmap/iterative/demo/sources/C0/payload.bin', 'tracked snapshot after');
  write('.dependency-roadmap/history/snapshot-objects/aa/object.gz', 'new object');
  write('personal.txt','staged unrelated edit'); git('add','-A');
  const selected = teamStatePaths('.dependency-roadmap/settings.project.json', {historyDir:'.dependency-roadmap', groupsConfig: '../outside.json'}, root);
  assert.ok(!selected.includes('.dependency-roadmap') && !selected.includes('../outside.json'));
  assert.throws(() => teamStatePathspecs([]), /No safe/);
  const scope = teamStatePathspecs(selected.filter(value => ['.dependency-roadmap/settings.project.json','.dependency-roadmap/history'].includes(value)));
  git('add','-A','--',...scope); git('commit','-m','team state','--only','--',...scope);
  assert.equal(git('diff-tree','--no-commit-id','--name-only','-r','HEAD'), '.dependency-roadmap/history/report.md');
  const staged = git('diff','--cached','--name-only');
  assert.match(staged,/personal.txt/); assert.match(staged,/payload.bin/); assert.match(staged,/source.bin/); assert.match(staged,/object.gz/);
  assert.equal(git('diff','--cached','--name-only','--',...scope), '');
  console.log('Real Git state commit: runtime copies and unrelated staged files excluded, index preserved');
} finally { rmSync(root,{recursive:true,force:true}) }
