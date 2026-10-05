// Dev-only: renders the real panel with a synthetic read-only status.
import { createRoot } from 'react-dom/client'
import { IterativeTaskPanel } from '../components/IterativeTaskPanel'
import { LanguageProvider, useLanguage } from '../i18n'
import '../index.css'
import '../App.css'
const action = async () => ({ ok: true })
const openPath = async (path?: string) => { document.getElementById('opened-path')!.textContent = path ?? '' }
const status = async () => ({ok:true,present:true,stale:false,phase:'READY',workingCheckout:{path:'C:/demo/.dependency-roadmap/iterative/demo/trial/workspace',kind:'trial' as const,checkpointId:'C6'},decision:{step:'plan-next' as const,phase:'READY',reason:'Other cohorts continue'},scopeExpansionIssues:[{packages:['@testing-library/react'],proposals:['@testing-library/dom = ^10.0.0'],reason:'SCOPE_EXPANSION_NEEDS_EXACT_VERSION',nextAction:'Provide an exact registry version.'}]})
export function Harness() {
 const {setLanguage}=useLanguage()
 return <main style={{padding:24,maxWidth:960,margin:'auto'}}>
  <output id="opened-path" /><button onClick={()=>setLanguage('ru')}>RU</button><button onClick={()=>setLanguage('en')}>EN</button>
  <IterativeTaskPanel projectName="Synthetic demo" onGet={async()=>({present:false,missing:[],stale:false})}
   onStatus={status} onAttempt={async()=>({ok:true,present:false})}
   onExport={action} onExportLegacy={action} onCopy={action} onSave={action}
   onDrive={async()=>({ok:true,steps:[],stopped:'finished'})} onBegin={async()=>({ok:true})}
   onAgent={action} onCancel={action} onOpenPath={openPath} />
 </main>
}
const root = import.meta.hot?.data.root ?? createRoot(document.getElementById('root')!)
if (import.meta.hot) import.meta.hot.data.root = root
root.render(<LanguageProvider><Harness /></LanguageProvider>)
