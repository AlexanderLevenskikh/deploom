// Dev-only full production renderer with synthetic IPC. Never migration evidence.
import { createRoot } from 'react-dom/client'
import App from '../App'
import { LanguageProvider } from '../i18n'
import type { BootstrapPayload, DependencyFlowApi, IterativeStatusOutcome } from '../types'
import type { ProjectAuditView } from '../../electron/project-audit-types'
import '../index.css'
const query = new URLSearchParams(location.search)
const scenario = query.get('case') || 'ready'
const workspace = { id:'qa', name:'QA workspace · synthetic IPC', path:'C:/qa/workspace', templateRemote:'', toolRemote:'', settingsPath:'.dependency-roadmap/settings.json', agent:'codex' as const, selectedProject:'Demo.Web' }
const projects = (query.get('projects') === 'many' ? ['Demo.Web','Demo.Api','Demo.Tools', ...Array.from({length:21}, (_,i) => 'Demo.VeryLongProjectNameForEllipsis.' + (i + 1))] : ['Demo.Web','Demo.Api','Demo.Tools']).map(name => ({name,path:'C:/qa/'+name,nodeVersion:'22',git:{sourceBranch:'master',baseBranch:'libs'}}))
const details = { workspace, projects, settingsExists:true, dashboardExists:false, promptStale:false, git:{branch:'master',dirty:false,summary:[]}, projectLevels:{}, teamState:{schemaVersion:1 as const,updatedAt:'',projects:{}} }
const payload: BootstrapPayload = { appVersion:'0.3.7 · QA', state:{schemaVersion:1,workspaces:[workspace],selectedWorkspaceId:'qa'},details,environment:{git:{available:true,version:'Git · fixture'},python:{available:true,version:'Python · fixture'},codex:{available:true,version:'Codex · fixture'},opencode:{available:true,version:'OpenCode · fixture'}},defaults:{templateRemote:'',toolRemote:''},notificationsEnabled:false,themePreference:query.get('theme')==='dark'?'dark':'light',updateStatus:{state:query.get('update')==='error'?'error':'downloading',percent:47,version:'QA-next'} }
let audit: ProjectAuditView = { lifecycle:{phase:scenario==='audit-error'?'error':'missing',error:scenario==='audit-error'?'REGISTRY_UNAVAILABLE: synthetic fixture':undefined,previousPresent:false} }
let auditStarts=0
const observed = (key:string,value:unknown) => { document.documentElement.dataset[key]=JSON.stringify(value) }
const status = (): IterativeStatusOutcome => ({ok:true,present:false,stale:false,checked:scenario==='unchecked'?undefined:{ok:true},inFlight:false,validationProfile:{commands:['npm run typecheck','npm test'],suggestedUnitCommand:'npm test'}})
const bridge = {
 bootstrap:async()=>payload, refreshWorkspace:async()=>({state:payload.state,details}),
 selectProject:async(input:{projectName:string})=>{workspace.selectedProject=input.projectName;return {state:payload.state,details}},
 selectWorkspace:async()=>({state:payload.state,details}),
 setThemePreference:async(preference: 'light'|'dark'|'system')=>({preference}),
 setNotificationsEnabled:async(enabled:boolean)=>({enabled}),
 onJobOutput:()=>()=>{}, onJobFinished:()=>()=>{}, onMigrationProgressChanged:()=>()=>{}, onDownloadSaved:()=>()=>{},onUpdateStatus:()=>()=>{},onIterativeAttempt:()=>()=>{},onStorageStatus:()=>()=>{},onStorageProgress:()=>()=>{},
 storageStatus:async()=>({phase:'idle'}),
 getIterativeTask:async()=>({present:false,missing:['run.json'],stale:false}),iterativeStatus:async()=>status(),iterativeAttempt:async()=>({ok:true,present:false}),
 listProjectBranches:async()=>['master','libs'], listNodeVersions:async()=>['22','24'],listAgentModels:async()=>['default'],
 projectAuditStatus:async()=>audit,
 projectAuditStart:async()=>{ auditStarts++;observed('auditStarts',auditStarts);audit={lifecycle:{phase:'running',operationId:'audit-fixture',previousPresent:false}};setTimeout(()=>{audit={lifecycle:{phase:'done',operationId:'audit-fixture',previousPresent:false},audit:{status:'UNKNOWN',auditComplete:false,securityComplete:true,lagComplete:false,generatedAt:new Date().toISOString(),packageTotals:{critical:0,high:2},lagUnknown:1,unknownPackages:[{package:'example-private',reason:'Synthetic registry data unavailable'}],policy:{minLagOkPct:80,maxKnownHigh:1}}}},1500);return audit},
 journalSources:async(input:{projectName:string})=>({sources:[{id:'fixture-log',kind:'flow',format:'events',runId:'qa-check-01',action:'preflight',current:true,artifactPath:'C:/qa/activity.log',startedAt:Date.now(),projectName:input.projectName}]}),
 journalRead:async(input:{projectName:string;cursor?:{offset:number}})=>({sourceId:'fixture-log',events:input.cursor?[]:Array.from({length:12},(_,index)=>({id:'fixture:'+index,offset:index,endOffset:index+1,workspaceId:'qa',projectName:input.projectName,runId:'qa-check-01',source:'system',stream:'stdout',text:index===10?'npm test: synthetic completed check':'Synthetic check output '+index,at:Date.now()})),cursor:{epoch:'fixture',offset:12},reset:!input.cursor,truncated:false,bytes:12}),
 journalExport:async()=>{observed('exported',true);return {canceled:false,path:'C:/qa/export.ndjson'}},
 openPath:async(path:string)=>{observed('openedPath',path)},
 getBaselineIntentPlan:async()=>({candidates:[],intent:{schemaVersion:2,policies:{},controlMode:'AUTONOMOUS',targetLevel:'yellow'}}),
 getDependencyGraphSnapshot:async(projectName:string)=>({schemaVersion:1,project:projectName,capturedAt:new Date().toISOString(),packages:[{name:'react',kind:'runtime',requestedSpec:'^18.0.0',currentVersion:'18.3.1',installedVersion:'18.3.1',policy:'auto',manifestObserved:true}],edges:[],intent:{policies:{},deferredCohorts:[]},manifestCoverage:{observed:1,total:1,missing:[]},authorityBoundary:{packageList:'PROJECT_MANIFEST',relations:'OBSERVED_LOCAL_MANIFEST',cohorts:'DIAGNOSTIC_HINT_OR_USER_POLICY',proofAuthority:false}}),
 checkForUpdates:async()=>({currentVersion:payload.appVersion,development:true}),installUpdate:async()=>{observed('installed',true)},
 iterativeBegin:async(input:unknown)=>{observed('begin',input);return {ok:true,checked:{ok:true}}},iterativeDrive:async(input:unknown)=>{observed('drive',input);return {ok:true,steps:[],stopped:'finished'}},iterativeCancel:async()=>({ok:true}),
 updateWorkspace:async(input:{selectedProject?:string})=>{if(input.selectedProject !== undefined)workspace.selectedProject=input.selectedProject; const next=structuredClone({state:payload.state,details}); if(query.get('switchDelay'))await new Promise(resolve=>setTimeout(resolve,Number(query.get('switchDelay')))); if(query.get('switchError') && input.selectedProject === 'Demo.Api')throw Error('QA: project state unavailable'); return next},updateProjectBranches:async()=>({state:payload.state,details}),updateProjectNode:async()=>({state:payload.state,details}),
}
window.dependencyFlow = bridge as unknown as DependencyFlowApi
const root = createRoot(document.getElementById('root')!)
root.render(<LanguageProvider><App /></LanguageProvider>)
if(import.meta.hot)import.meta.hot.dispose(()=>root.unmount())
