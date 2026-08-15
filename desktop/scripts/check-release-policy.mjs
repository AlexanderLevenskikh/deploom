import { releaseGateCommands, releasePolicyForProject } from '../dist-electron/release-policy.js'

const settings = {
  release: {
    commitMessage: 'global release',
    finalGateCommands: ['yarn lint', 'yarn build'],
  },
  projects: [
    { name: 'A', release: { finalGateCommands: ['yarn test'], commitMessage: 'project release' } },
    { name: 'B', git: { release: { finalGateCommands: ['npm test'] } } },
    { name: 'C' },
    { name: 'D', release: {}, git: { release: { finalGateCommands: ['must-not-win'] } } },
  ],
}

const a = releasePolicyForProject(settings, 'A')
if (a.commitMessage !== 'project release') throw new Error('Project release commit message did not override global')
if (JSON.stringify(a.finalGateCommands) !== JSON.stringify(['yarn test'])) throw new Error('Project final gates did not override global')

const b = releasePolicyForProject(settings, 'B')
if (b.commitMessage !== 'global release') throw new Error('Global release commit message was lost')
if (JSON.stringify(b.finalGateCommands) !== JSON.stringify(['npm test'])) throw new Error('git.release override was not read')

const c = releasePolicyForProject(settings, 'C')
if (JSON.stringify(c.finalGateCommands) !== JSON.stringify(['yarn lint', 'yarn build'])) throw new Error('Global final gates were not inherited')

const d = releasePolicyForProject(settings, 'D')
if (JSON.stringify(d.finalGateCommands) !== JSON.stringify(['yarn lint', 'yarn build'])) throw new Error('Explicit empty project release must ignore git.release and inherit global values')

const gates = releaseGateCommands(settings, 'C', 'yarn build')
if (JSON.stringify(gates) !== JSON.stringify(['yarn lint', 'yarn build'])) throw new Error('Release gates were not de-duplicated')

console.log('Release policy resolution OK')
