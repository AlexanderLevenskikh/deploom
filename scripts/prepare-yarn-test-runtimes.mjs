// Pinned modern Yarn runtimes for physical acceptance, isolated from Classic.
import { mkdirSync, mkdtempSync, appendFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
const root = process.env.DEPLOOM_TEST_YARN_ROOT || mkdtempSync(join(tmpdir(), 'deploom-yarn-acceptance-'));
for (const version of ['2.4.3', '3.8.7', '4.0.0']) {
  const prefix = join(root, version);
  mkdirSync(prefix, { recursive: true });
  if (version.startsWith('2.')) {
    // Yarn 2 predates the standalone cli-dist npm publication.
    const response = await fetch(`https://repo.yarnpkg.com/${version}/packages/yarnpkg-cli/bin/yarn.js`);
    if (!response.ok) throw new Error(`Yarn ${version} download failed: ${response.status}`);
    const target = join(prefix, 'node_modules', '@yarnpkg', 'cli-dist', 'bin');
    mkdirSync(target, { recursive: true });
    writeFileSync(join(target, 'yarn.js'), Buffer.from(await response.arrayBuffer()));
    continue;
  }
  const args = ['install', '--prefix', prefix, '--ignore-scripts', '--no-audit', '--no-fund', '--package-lock=false', `@yarnpkg/cli-dist@${version}`];
  const executable = process.platform === 'win32' ? process.env.COMSPEC || 'cmd.exe' : 'npm';
  const argv = process.platform === 'win32' ? ['/d', '/s', '/c', 'npm', ...args] : args;
  const result = spawnSync(executable, argv, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status || 1);
}
if (process.env.GITHUB_ENV) appendFileSync(process.env.GITHUB_ENV, `DEPLOOM_TEST_YARN_ROOT=${resolve(root)}\n`);
console.log(`DEPLOOM_TEST_YARN_ROOT=${resolve(root)}`);
