import type { TranslationKey } from '../i18n'
import type { FlowAction } from '../types'

export type FlowStage = {
  id: number
  action?: FlowAction
  titleKey: TranslationKey
  descriptionKey: TranslationKey
  buttonKey: TranslationKey
  confirmationKey?: TranslationKey
}

export const FLOW_STAGES: FlowStage[] = [
  { id: 1, action: 'preflight', titleKey: 'flow.stage.preflight.title', descriptionKey: 'flow.stage.preflight.description', buttonKey: 'flow.stage.preflight.button' },
  { id: 2, action: 'baseline', titleKey: 'flow.stage.baseline.title', descriptionKey: 'flow.stage.baseline.description', buttonKey: 'flow.stage.baseline.button', confirmationKey: 'flow.stage.baseline.confirmation' },
  { id: 3, titleKey: 'flow.stage.plan.title', descriptionKey: 'flow.stage.plan.description', buttonKey: 'flow.stage.plan.button' },
  { id: 4, action: 'agent', titleKey: 'flow.stage.agent.title', descriptionKey: 'flow.stage.agent.description', buttonKey: 'flow.stage.agent.button' },
  { id: 5, action: 'generate', titleKey: 'flow.stage.verify.title', descriptionKey: 'flow.stage.verify.description', buttonKey: 'flow.stage.verify.button' },
  { id: 6, action: 'audit', titleKey: 'flow.stage.audit.title', descriptionKey: 'flow.stage.audit.description', buttonKey: 'flow.stage.audit.button' },
  { id: 7, action: 'release', titleKey: 'flow.stage.release.title', descriptionKey: 'flow.stage.release.description', buttonKey: 'flow.stage.release.button', confirmationKey: 'flow.stage.release.confirmation' },
  { id: 8, action: 'commit-state', titleKey: 'flow.stage.state.title', descriptionKey: 'flow.stage.state.description', buttonKey: 'flow.stage.state.button', confirmationKey: 'flow.stage.state.confirmation' },
  { id: 9, action: 'push-workspace', titleKey: 'flow.stage.publish.title', descriptionKey: 'flow.stage.publish.description', buttonKey: 'flow.stage.publish.button', confirmationKey: 'flow.stage.publish.confirmation' },
]

export const ACTION_ORDER = FLOW_STAGES.flatMap((stage) => stage.action ? [stage.action] : [])
