from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ProjectIsolationRegressionTests(unittest.TestCase):
    def test_prompt_and_dashboard_readiness_are_scoped_to_selected_project(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        flow = (ROOT / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("promptPathForProject(workspace, project.name)", main)
        self.assertIn("dashboardHasProject(workspace, project.name)", main)
        self.assertIn("projectPromptPath: project ? promptPathForProject", main)
        self.assertIn("details.projectPromptPath", flow)
        self.assertNotIn("details.workspace.latestPromptPath", flow)

    def test_download_completion_never_rewrites_stale_selected_project(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("const downloadProjectName = workspace.selectedProject", main)
        self.assertIn("const latestState = loadState()", main)
        self.assertIn("rememberScopedPromptPath(stored, downloadProjectName, savePath)", main)
        # Old behaviour saved the DesktopState captured at will-download time,
        # which could restore a project the user had already left.
        prompt_block = main.split("if ((/prompt|task|agent/i.test(suggested)", 1)[1].split("const recalculate", 1)[0]
        self.assertNotIn("saveState(state)", prompt_block)

    def test_renderer_rejects_stale_async_view_results_and_isolates_chat(self) -> None:
        hook = (ROOT / "desktop" / "src" / "hooks" / "useDependencyFlow.ts").read_text(encoding="utf-8")
        self.assertIn("viewEpochRef", hook)
        self.assertIn("const epoch = ++viewEpochRef.current", hook)
        self.assertIn("if (viewEpochRef.current === epoch)", hook)
        self.assertIn("entry.workspaceId === selectedWorkspaceId && entry.projectName === selectedProject?.name", hook)
        self.assertIn("const [activeRuns, setActiveRuns]", hook)

    def test_autopilot_finish_error_keeps_project_context_after_ref_is_cleared(self) -> None:
        hook = (ROOT / "desktop" / "src" / "hooks" / "useDependencyFlow.ts").read_text(encoding="utf-8")
        finish_block = hook.split("})().catch((finishError) => {", 1)[1].split("})\n    })", 1)[0]
        self.assertIn("const failedWorkspaceId = event.workspaceId", finish_block)
        self.assertIn("const failedProjectName = event.projectName", finish_block)
        self.assertIn("autopilotRef.current = undefined", finish_block)
        self.assertIn("setContextError(failedWorkspaceId, failedProjectName", finish_block)
        self.assertNotIn("autopilotRef.current?.", finish_block)

    def test_project_background_actions_can_overlap_across_projects_while_workspace_globals_stay_locked(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")

        self.assertIn(
            "const PROJECT_BACKGROUND_ACTIONS = new Set<FlowAction>(['preflight', 'baseline'])",
            main,
        )
        self.assertIn(
            "const WORKSPACE_GLOBAL_ACTIONS = new Set<FlowAction>(['sync-tool', 'generate-all', 'commit-state', 'push-workspace'])",
            main,
        )
        self.assertIn(
            "if (existing.projectName === project.name) return true",
            main,
        )
        self.assertIn(
            "if (WORKSPACE_GLOBAL_ACTIONS.has(action) || WORKSPACE_GLOBAL_ACTIONS.has(existing.action)) return true",
            main,
        )
        self.assertIn(
            "if (PROJECT_BACKGROUND_ACTIONS.has(action) || PROJECT_BACKGROUND_ACTIONS.has(existing.action)) return false",
            main,
        )
        self.assertIn(
            "projectRunConflicts(existing, workspace, project, input.action)",
            main,
        )
        self.assertIn(
            "const runningJob = [...jobs.values()].find",
            main,
        )


    def test_project_generator_outputs_are_snapshotted_per_project(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("function projectArtifactCacheDir", main)
        self.assertIn("app.getPath('userData'), 'project-artifacts'", main)
        self.assertIn("snapshotProjectArtifacts(job.workspace, job.projectName)", main)
        self.assertIn("const roadmapPath = projectRoadmapPath(job.workspace, project.name)", main)
        self.assertIn("const dashboardPath = projectDashboardPath(job.workspace, project.name)", main)
        self.assertIn("const dashboardPath = project ? projectDashboardPath(workspace, project.name)", main)

    def test_project_switch_clears_visible_error_and_rail_tracks_workspace_busy(self) -> None:
        hook = (ROOT / "desktop" / "src" / "hooks" / "useDependencyFlow.ts").read_text(encoding="utf-8")
        app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        select_project = hook.split("const selectProject = useCallback", 1)[1].split("const updateWorkspace", 1)[0]
        self.assertIn("setError(undefined)", select_project)
        self.assertIn("workspaceBusy: anyActiveJob", hook)
        self.assertIn("active={flow.workspaceBusy}", app)


    def test_fresh_baseline_ends_old_execution_epoch_and_forgets_prompt(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        context = (ROOT / "desktop" / "electron" / "project-context.ts").read_text(encoding="utf-8")
        flow = (ROOT / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("cleanupSupersededMigrationAfterBaseline", main)
        self.assertIn("baselineStartCommands(input, project)", main)
        self.assertIn("SOURCE_SNAPSHOT_BASELINE_LIVE_CHECKOUT", main)
        self.assertNotIn("BASELINE_SOURCE_DIRTY", main)
        baseline_guard = main.split("async function baselineStartCommands", 1)[1].split("async function cleanAgentStartCommands", 1)[0]
        self.assertNotIn("'status'", baseline_guard)
        self.assertNotIn("'switch'", baseline_guard)
        self.assertIn("if (job.action === 'baseline' && job.projectName)", main)
        self.assertIn("forgetProjectPromptState(job.workspace, project.name, oldPromptPath)", main)
        self.assertIn("const freshBaseline = job.action === 'baseline' && status === 'passed'", main)
        self.assertIn("forgetScopedPromptPath", context)
        self.assertIn("План предыдущего цикла забыт", main)
        # With the previous prompt forgotten, planReady is false even though the
        # new dashboard exists, so the non-action Step 3 stays current/un-checked.
        self.assertIn("const planReady = details.dashboardExists && promptReady", flow)

    def test_planner_is_rendered_as_one_global_supervisor_gate(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        flow = (ROOT / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertNotIn("Planner расширяет план", flow)
        ru = (ROOT / "desktop" / "src" / "i18n" / "locales" / "ru.ts").read_text(encoding="utf-8")
        en = (ROOT / "desktop" / "src" / "i18n" / "locales" / "en.ts").read_text(encoding="utf-8")
        self.assertIn("planning: t('flow.runtime.planning')", flow)
        self.assertIn('"flow.runtime.planning": "Ожидает новый план"', ru)
        self.assertIn('"flow.runtime.planning": "Waiting for a new plan"', en)
        self.assertIn("t('flow.supervisorReplans')", flow)
        self.assertIn('"flow.supervisorReplans": "Supervisor пересчитывает общий план"', ru)
        self.assertIn('"flow.supervisorReplans": "Supervisor is recalculating the overall plan"', en)
        self.assertIn("!['planning', 'queued', 'failed', 'ready'].includes", flow)
        planner_block = main.split("async function runIndependentPlannerWithProgress", 1)[1].split("async function runIndependentPlanner(", 1)[0]
        self.assertIn("setBranchRuntime(job, branch, 'planning')", planner_block)
        self.assertNotIn("Независимый Planner расширяет executable plan", planner_block)

    def test_manual_agent_launch_builds_fresh_prompt_after_baseline_boundary(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        agent_case = main.split("case 'agent': {", 1)[1].split("case 'release':", 1)[0]
        self.assertIn("if (input.promptPath)", agent_case)
        self.assertIn("ensureCurrentAgentPrompt()", agent_case)
        self.assertNotIn("Сначала выгрузите prompt", agent_case)
        self.assertIn("await ensureCurrentAgentPrompt(job.workspace, project", main)

    def test_dashboard_opens_filtered_to_current_project(self) -> None:
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("new URLSearchParams(window.location.search).get('project')", generator)
        self.assertIn("project=${encodeURIComponent(project?.name || '')}", main)


if __name__ == "__main__":
    unittest.main()
