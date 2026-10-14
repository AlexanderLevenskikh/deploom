import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
type SurfaceName = 'dependencies' | 'report' | 'journal'
type SurfaceTargets = Partial<Record<SurfaceName, HTMLDivElement | null>>
const ProjectSurfaceContext = createContext<SurfaceTargets>({})
export const ProjectSurfaceProvider = ProjectSurfaceContext.Provider
export function useProjectSurface() {
  const targets = useContext(ProjectSurfaceContext)
  return (name: SurfaceName, children: ReactNode) => targets[name] ? createPortal(children, targets[name]!) : children
}
export function useProjectSurfaceHosts() {
  const [dependencies, setDependencies] = useState<HTMLDivElement | null>(null)
  const [report, setReport] = useState<HTMLDivElement | null>(null)
  const [journal, setJournal] = useState<HTMLDivElement | null>(null)
  const bindDependencies = useCallback((element: HTMLDivElement | null) => setDependencies(element), [])
  const bindReport = useCallback((element: HTMLDivElement | null) => setReport(element), [])
  const bindJournal = useCallback((element: HTMLDivElement | null) => setJournal(element), [])
  return { targets: { dependencies, report, journal }, bindDependencies, bindReport, bindJournal }
}
