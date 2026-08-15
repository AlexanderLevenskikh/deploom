import { restoreVerifiedAgentCompletion, updateFlowProgress } from "../dist-electron/flow-state.js";

const auditPosition = {
  lastAction: "generate",
  status: "passed",
  target: "yellow",
  completedActions: ["preflight", "baseline", "agent", "generate"],
};

const checking = updateFlowProgress(auditPosition, "preflight", "running");
if (checking.completedActions.join(",") !== auditPosition.completedActions.join(",")) throw new Error(`Preflight erased progress while running: ${JSON.stringify(checking)}`);
if (checking.lastAction !== "generate" || checking.status !== "passed" || checking.target !== "yellow") throw new Error(`Preflight moved the workflow marker: ${JSON.stringify(checking)}`);

const checked = updateFlowProgress(checking, "preflight", "passed");
if (checked.completedActions.join(",") !== auditPosition.completedActions.join(",")) throw new Error(`Completed preflight erased progress: ${JSON.stringify(checked)}`);
if (checked.lastAction !== "generate" || checked.status !== "passed") throw new Error(`Completed preflight moved the workflow marker: ${JSON.stringify(checked)}`);

const initialCheck = updateFlowProgress(undefined, "preflight", "running");
if (initialCheck.completedActions.length !== 0 || initialCheck.lastAction !== "preflight" || initialCheck.status !== "running") throw new Error(`Initial preflight state is wrong: ${JSON.stringify(initialCheck)}`);
const initialPassed = updateFlowProgress(initialCheck, "preflight", "passed");
if (initialPassed.completedActions.join(",") !== "preflight") throw new Error(`Initial preflight was not completed: ${JSON.stringify(initialPassed)}`);

const baselineRerun = updateFlowProgress(auditPosition, "baseline", "running");
if (baselineRerun.completedActions.join(",") !== "preflight") throw new Error(`Baseline rerun did not invalidate downstream work: ${JSON.stringify(baselineRerun)}`);

const auditedGoalMiss = { ...auditPosition, lastAction: "audit", completedActions: [...auditPosition.completedActions, "audit"] };
const agentEpochRunning = updateFlowProgress(auditedGoalMiss, "agent", "running");
if (agentEpochRunning.completedActions.join(",") !== "preflight,baseline") throw new Error(`Goal-seeking agent rerun retained stale generate/audit/release: ${JSON.stringify(agentEpochRunning)}`);
const agentEpochPassed = updateFlowProgress(agentEpochRunning, "agent", "passed");
if (agentEpochPassed.completedActions.join(",") !== "preflight,baseline,agent") throw new Error(`Goal-seeking agent epoch did not restart normal stage order: ${JSON.stringify(agentEpochPassed)}`);
const failedAgent = updateFlowProgress(auditedGoalMiss, "agent", "failed");
if (failedAgent.completedActions.join(",") !== "preflight,baseline") throw new Error(`Failed agent retained invalid downstream work: ${JSON.stringify(failedAgent)}`);

const FLOW_ACTIONS_BEFORE_PUBLICATION = ["preflight", "baseline", "agent", "generate", "audit", "release", "commit-state"];
const failedPublication = updateFlowProgress({ ...auditPosition, lastAction: "push-workspace", completedActions: [...FLOW_ACTIONS_BEFORE_PUBLICATION, "push-workspace"] }, "push-workspace", "failed");
if (failedPublication.completedActions.join(",") !== FLOW_ACTIONS_BEFORE_PUBLICATION.join(",")) throw new Error(`Failed publication retained a false success marker: ${JSON.stringify(failedPublication)}`);

const failedRelease = {
  lastAction: "release",
  status: "failed",
  target: "yellow",
  completedActions: ["preflight", "baseline", "agent", "generate", "audit"],
};
const recoveryRunning = updateFlowProgress(failedRelease, "recover", "running", "yellow");
if (recoveryRunning.lastAction !== "release" || recoveryRunning.status !== "failed" || recoveryRunning.completedActions.join(",") !== failedRelease.completedActions.join(",")) throw new Error(`Recovery must not move/reset FLOW stage markers: ${JSON.stringify(recoveryRunning)}`);
const releasePassed = { ...failedRelease, status: "passed", completedActions: [...failedRelease.completedActions, "release"] };
const recoveryPassed = updateFlowProgress(releasePassed, "recover", "passed", "yellow");
if (recoveryPassed.lastAction !== "release" || recoveryPassed.status !== "passed" || !recoveryPassed.completedActions.includes("release") || recoveryPassed.completedActions.includes("recover")) throw new Error(`Successful recovery changed the visible FLOW position: ${JSON.stringify(recoveryPassed)}`);

const legacyFalseFailure = restoreVerifiedAgentCompletion({
  lastAction: "agent",
  status: "failed",
  target: "yellow",
  completedActions: ["preflight", "baseline"],
});
if (legacyFalseFailure.status !== "passed" || !legacyFalseFailure.completedActions.includes("agent")) throw new Error(`Verified Git completion must repair the old dirty-release false failure: ${JSON.stringify(legacyFalseFailure)}`);
const releaseFailurePosition = restoreVerifiedAgentCompletion({
  lastAction: "release",
  status: "failed",
  target: "yellow",
  completedActions: ["preflight", "baseline", "generate", "audit"],
});
if (releaseFailurePosition.lastAction !== "release" || releaseFailurePosition.status !== "failed" || !releaseFailurePosition.completedActions.includes("agent")) throw new Error(`Restoring migration completion must not hide the real release failure: ${JSON.stringify(releaseFailurePosition)}`);

console.log("Flow state transitions OK");
