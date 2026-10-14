# Desktop project surfaces

One scenario owner remains mounted for the selected workspace/project across tab navigation. Actions still come from the existing runtime scenario; navigation never starts work or grants proof authority.

| Surface | Capabilities |
| --- | --- |
| Overview | One main scenario action, current activity, pipeline, explicit current-source audit; secondary Draft/artifact actions. |
| Dependencies | Scope/Draft, required/deferred policies, agent/model/Node/branches, validation commands and nonregression scope, graph/fullscreen. |
| Report | Preserved checkout, delivery/commits, document cleanup, task preview/copy/save/rebuild and dashboard. |
| Journal | Durable FLOW/iterative/audit sources, run/attempt history, activity/raw, source filters, search/follow, visible copy, complete export, diagnostics and live-group messaging. |
| Workspace/interface | Project/workspace management, per-project audit, storage, RU/EN, light/dark/system, notifications and settings. |

## Journal ownership

New output uses journal.ndjson. Byte offsets plus file epochs identify records; equal text is not deduplicated. Live IPC wakes scoped disk replay instead of being concatenated with it. journal-attempts.ndjson records attempt boundaries without copying output. Export streams the entire selected artifact/range. The renderer retains at most 2,000 events and windows mounted rows. Clear view preserves disk and cursor. Legacy raw artifacts remain separate sources.

## Audit lifecycle

Status observation is read-only. Explicit audit actions collect current-source evidence; existing explicit planning and mandatory cohort/final audits remain. Durable operation IDs bind evidence to dependency inputs including shared manager inputs, policy, Node/runtime and an opaque registry/authentication hash. Changed context is distinct from saved measurements. Missing/partial evidence never means zero. Lost owners become interrupted, duplicate starts join one collection and previous complete evidence is retained separately.

## Storage and updater

Cleanup disables migration launch/retry with a visible reason. Pause is cooperative and remains pending until the worker acknowledges a safe boundary. Paused cleanup still blocks migration. Resume keeps the same operation; cancel unwinds safely and releases ownership only after termination. Canceled results are partial, completed deletions are not undone, and another cleanup requires fresh inspection. The owned mailbox cannot control another operation.

DepLoom exposes checking, available, downloading, current, ready and error states. Unknown progress is indeterminate. Restart is explicit; backend ownership checks cover FLOW, iterative/Autopilot, audit and maintenance both before download and immediately before restart.

## Validation boundary

Dev-only renderer harnesses use synthetic IPC and do not establish project migration acceptance. Filesystem/subprocess gates cover real journal ingestion and attempt export, audit operation ownership and Node-to-Python storage controls. Normal public release validation remains mandatory.
