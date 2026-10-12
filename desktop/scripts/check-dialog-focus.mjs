import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const effects=[],listeners=new Map()
const document={activeElement:null,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)}
class Element {
  constructor(name){this.name=name;this.isConnected=true}
  focus(){document.activeElement=this}
  getClientRects(){return [{}]}
  hasAttribute(){return false}
}
const trigger=new Element('trigger'),close=new Element('close'),input=new Element('input'),cancel=new Element('cancel')
const root={querySelectorAll:()=>[close,input,cancel],contains:element=>[close,input,cancel].includes(element)}
document.activeElement=trigger
const exports={}
const code=ts.transpileModule(readFileSync(new URL('../src/hooks/useDialogFocus.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
vm.runInNewContext(code,{exports,document,HTMLElement:Element,require:()=>({useRef:value=>({current:value}),useEffect:effect=>effects.push(effect)})})
let closed=0
exports.useDialogFocus({current:root},{onClose:()=>closed++})
input.focus() // React autoFocus commit happens before the effect.
const cleanup=effects[0]()
assert.equal(document.activeElement,input,'respect the autofocus already inside the modal')
const key=(value,shiftKey=false,defaultPrevented=false)=>({key:value,shiftKey,defaultPrevented,preventDefault(){this.defaultPrevented=true}})
cancel.focus();listeners.get('keydown')(key('Tab'));assert.equal(document.activeElement,close)
listeners.get('keydown')(key('Tab',true));assert.equal(document.activeElement,cancel)
listeners.get('keydown')(key('Escape',false,true));assert.equal(closed,0,'nested listbox handles Escape first')
listeners.get('keydown')(key('Escape'));assert.equal(closed,1)
cleanup();assert.equal(document.activeElement,trigger,'restore the pre-commit trigger, not an unmounted input')
assert.equal(listeners.size,0)
console.log('Dialog focus lifecycle: pre-commit trigger, autofocus, Tab boundaries, nested Escape, restore and cleanup OK')
