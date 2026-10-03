# Adaptive cohorts, storage maintenance and staged review

## Cohort scheduling

Iterative migration starts with a soft batch size of eight actionable updates. Verified cumulative acceptance grows the batch toward the default cap of 24. The CLI accepts `begin --cohort-max-packages 1..32`; an explicit cap remains authoritative. Related package families, installed dependency metadata, priorities and validated companion proposals guide grouping. These are scheduling hints: resolver, source checks and authoritative verification still decide acceptance.

An exactly rejected assignment can be split into smaller candidates. Infrastructure failures do not establish incompatibility. Unavailable targets and deferred updates remain visible. Existing runs without an explicit cohort cap adopt the new scheduling default on their next planning step; their in-flight candidate and verified checkpoint remain unchanged.

The physical 24-package demo accepted 22 updates in three cumulative cohorts (8, 12, 2), deferred two intentionally unavailable upgrades, rejected an invalid repair and resumed the same candidate after stopping at C1. This demonstrates the production CLI with Git, npm and project checks; it does not establish acceptance for every real project or a particular agent provider.

## Storage cleanup

The desktop header contains Storage cleanup. Inspect first, then clean detached verification garbage older than one day. The selected storage root comes from the backend, never an arbitrary renderer path. Cleanup claims a directory by atomic rename, rejects junctions, symbolic links and hard-linked trees, and reports removal failures separately. It preserves active runs, cumulative checkpoints, project checkouts, run history and package caches.

New private verification trials record their owning process. Automatic orphan cleanup requires evidence that the owner exited. Legacy unmarked directories remain protected: age alone does not prove that a long verification finished. Failed normal cleanup retires a private trial into the recognized garbage namespace for later retry. ReFS and NTFS have the same lifecycle rules. Logical file size does not equal physically reclaimable space on ReFS.

Restarting migration archives durable run state. It is not a request to erase all snapshots or Git worktrees. Existing Git cleanup removes only recognized tool-owned worktrees at the appropriate publication boundary; unknown or dirty worktrees need separate review.

## Review and partial acceptance

Keep the existing publication mode. A cumulative checkpoint is an internal verified snapshot, not automatically a Git commit, GitLab diff version or tag. Reviewers can compare commits already published in the migration branch. Recommended future commit metadata is a stable checkpoint ID, parent checkpoint, accepted packages and versions, verification commands and evidence references. Do not automatically publish one tag or remote branch per cohort.

A future Accept current result action should pause scheduling, select the last verified checkpoint, show its exact diff and unresolved work, and produce a review branch from the pinned source base. Applying it to a developer checkout requires source-HEAD and dirty-worktree guards, fresh applicable audit evidence and explicit approval of the concrete result. Preserve both sides on conflicts and clean tool-owned worktrees only after publication is confirmed. This change does not implement that transfer action or checkpoint-to-Git commit publication.
