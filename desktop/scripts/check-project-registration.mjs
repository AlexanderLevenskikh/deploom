import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const code = ts.transpileModule(readFileSync(new URL('../src/components/project-registration-batch.ts', import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const exports={}
vm.runInNewContext(code,{exports,Set,Promise})
const {registerProjectBatch}=exports
const entries=[{path:'a'},{path:'b',packagePath:'b/member'}]
const completed=new Set(),calls=[],snapshots=[]
let fail=true
const submit=async entry=>{calls.push(entry.path);if(entry.path==='b'&&fail){fail=false;throw Error('second failed')}}
await assert.rejects(registerProjectBatch(entries,submit,completed,keys=>snapshots.push(keys.join(','))),/second failed/)
assert.deepEqual([...completed],['a'])
await registerProjectBatch(entries,submit,completed,keys=>snapshots.push(keys.join(',')))
assert.deepEqual(calls,['a','b','b'])
assert.deepEqual([...completed],['a','b/member'])
assert.deepEqual(snapshots,['a','a,b/member'])
const callbackFailure=new Set()
await assert.rejects(registerProjectBatch([{path:'x'}],async()=>{},callbackFailure,()=>{throw Error('UI callback failed')}),/UI callback/)
assert.deepEqual([...callbackFailure],['x'],'a fulfilled registration remains recorded even when presentation fails')
await registerProjectBatch([{path:'x'}],async()=>assert.fail('duplicate registration'),callbackFailure,()=>{})
console.log('Project registration batch: partial success, retry remainder, selected package key and callback failure OK')
