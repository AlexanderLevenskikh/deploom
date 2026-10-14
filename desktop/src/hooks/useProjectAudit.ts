import { useCallback, useEffect, useRef, useState } from 'react'
import type { DependencyFlowApi } from '../types'
import type { ProjectAuditView } from '../../electron/project-audit-types'

/** Observing a project never starts an audit. An epoch owns each async reply. */
export function useProjectAudit(api: DependencyFlowApi | undefined, workspaceId?: string, projectName?: string) {
  const [view, setView] = useState<ProjectAuditView>()
  const [error, setError] = useState<string>()
  const [starting, setStarting] = useState(false)
  const epoch = useRef(0)
  const inFlight = useRef(false)
  useEffect(() => {
    const generation = ++epoch.current
    setView(undefined); setError(undefined); setStarting(false); inFlight.current = false
    if (!api?.projectAuditStatus || !projectName) return
    let stopped = false, timer: ReturnType<typeof setTimeout>
    const read = async () => {
      try {
        const result = await api.projectAuditStatus({ workspaceId, projectName })
        if (!stopped && generation === epoch.current) { setView(result); setError(undefined) }
      } catch (reason) { if (!stopped && generation === epoch.current) setError(String(reason)) }
      finally { if (!stopped) timer = setTimeout(() => void read(), 2000) }
    }
    void read()
    return () => { stopped = true; clearTimeout(timer); ++epoch.current }
  }, [api, workspaceId, projectName])
  const start = useCallback(async () => {
    if (!api?.projectAuditStart || !projectName || inFlight.current) return
    const generation = epoch.current
    inFlight.current = true; setStarting(true); setError(undefined)
    try {
      const result = await api.projectAuditStart({ workspaceId, projectName })
      if (generation === epoch.current) setView(result)
    } catch (reason) { if (generation === epoch.current) setError(String(reason)) }
    finally { if (generation === epoch.current) { inFlight.current = false; setStarting(false) } }
  }, [api, workspaceId, projectName])
  return { view, error, starting, start }
}
