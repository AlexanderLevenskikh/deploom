# Iterative FLOW and automatic continuation

The visible Run settings card contains the agent, model, project/CI Node version,
branch and push preference. Agent/model and Node edits are persisted before
starting check, update or repair. Settings are disabled while the current
operation runs. Old acceptance/freshness metrics are explicitly labelled as
results of a separate audit in Technical details; they cannot describe the
current iterative attempt.

New and newly connected workspaces require an explicit agent selection. There
is no selected default in the creation screens. Existing saved workspaces retain
their provider. The model can use the selected provider's default or an explicit
selection. An agent invocation can consume tokens.

The project-check success journal marks the Check stage immediately, including
while the final IPC/status read is returning. Selecting versions and sealing
C0 are separate operations: C0 verification after selection stays at Plan and
is labelled as preparation of the verified starting state. A fresh explicit
check still runs at Check; a prior verdict cannot mark that running check done.
The discovery counter is scoped to one search, does not reset on begin.capture,
and retains its final count. Budget-skipped packages remain visible. Completion
of this counter means the search ended, not that skipped packages were queried
or that all proposed versions passed project verification.

## Autopilot

Enable the checkbox before starting, while a manual step is running, or at a
repair gate. The active operation is adopted without launching a duplicate.
Disabling it lets the current step finish and stops automatic transitions.
Electron owns the sequence:
project check -> begin with the configured scope or discovery -> durable drive
-> agent repair when requested -> durable drive again. The same underlying
handlers, exact-request verifier, audit, checkpoint guards and budgets apply.
A failed or inconclusive initial check stops for review; it is not retried or
silently treated as green. Error, cancellation, time/iteration budget, missing
targets and an identical repeated repair gate stop the sequence. Autopilot does
not add a release/push stage to the iterative workflow.

The coordinator stays active across renderer navigation/remounts and rejects a
second begin/drive/agent for the same workspace/project. Stop cancels the
coordinator as well as the current child, including between stage invocations.
Other projects have separate ownership. Closing/restarting the desktop does
not grant permission to launch another agent automatically: use Continue to
resume from the durable state. The autopilot checkbox is opt-in, not persisted
as an instruction to run unattended after a restart.

The obsolete Previous FLOW/history stage panel is removed. Current Technical
details and Run logs use the current attempt; previous logs remain accessible
as saved artifacts, with explicit provenance.
