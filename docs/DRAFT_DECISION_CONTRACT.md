# Draft decision contract and semantic inputs

Draft planning can run through the Python CLI without Electron. A Draft remains
`PLANNING_ONLY`, `NOT_VERIFIED`, with physical compatibility `UNKNOWN`. The
versioned contract below describes proposed decisions and allowed candidate
selection. It does not authorize a migration or turn solver success into passing
project checks.

## Decision contract v1

Pass the existing `DEPLOOM_BASELINE_INTENT_JSON` environment variable to the CLI.
Other existing intent fields continue to work. For example:

```json
{
  "schemaVersion": 1,
  "targetMode": "advisory",
  "objective": "latest-compatible-in-approved-lines",
  "mutablePackages": ["example-library", "example-adapter"],
  "approvedVersionLines": {
    "example-library": [2],
    "example-adapter": [1, 2]
  }
}
```

The Draft manifest and `plan.json` echo `decisionContract` with `version: 1` and
the four normalized fields. Consumers that request these features must require a
matching echo; an older Core cannot be assumed to honor new intent fields.
Malformed JSON and invalid bounds fail with `DECISION_CONTRACT_INVALID`.

| Field | Default | Meaning |
| --- | --- | --- |
| `targetMode` | `pinned` | Exact proposed targets need an explicit replacement assignment and replanning if they change. `advisory` permits evidence-backed alternatives inside existing user authority, with a recorded decision diff and replanning of affected constraints. |
| `objective` | `minimum-change-to-policy` | Preserve the existing health-policy planner. `latest-compatible-in-approved-lines` first selects the newest observed installable registry candidate in the allowed majors, then uses the existing peer solver for compatible fallbacks. |
| `mutablePackages` | `null` | All managed packages may be planned. An explicit list freezes every other package at current; `[]` freezes all packages. Frozen packages remain in peer constraints and the health denominator. |
| `approvedVersionLines` | `{}` | Map package names to allowed nonnegative integer major numbers. An empty list permits no new versions. Under the latest objective an absent entry means current major only; under the default objective an absent entry adds no major bound. |

The current version remains a fallback even when its major is outside the allowed
update lines. These fields do not authorize downgrades, adding new package names,
changing excluded scope, or ignoring peer requirements. The existing
`policies: {"example-library": "required"}` means an update from current, not an
exact target; `required` outside an explicit mutable list is rejected.

Both Russian and English generated prompts use the same authority rules. User
requirements and previously granted authorization define the work. Changing scope
requires an explicit scope update; a prompt must not ask again for authority the
user already granted. Missing evidence is not a compatibility conflict. Independent
components may proceed only when their independence is supported. Changed inputs,
policy or assignment invalidate the previous plan and require fresh checks.

The user's task determines `PLAN_ONLY` versus `EXECUTE`. A planning request does
not authorize install, project checks, source changes or migration documents
inside the target project. Execution/audit/checkpoint and developer-guide/report
instructions in the generated prompt apply only to user-authorized execution;
planning-only output describes those as future steps. Replanning means updating
the selected assignment, affected constraints and evidence, and does not require
an identical MCP retry. Policy satisfaction or a planner defer recommendation is
not a user pin; explicit user exclusions, keep-current and mutable bounds remain
user requirements.

## Health and freshness are separate

`POLICY_SATISFIED` answers the configured lag/security goal. A healthy project can
still have newer versions available. The latest objective changes candidate
selection even for such packages and constrains solver domains to allowed majors.
The existing solver retains its security, stability and target-preservation
priorities; this is not a new global freshness optimizer.
The candidate universe is stable registry versions, matching the existing static
solver domains. The CLI's broader metadata `includePrerelease` option does not
make this objective or its solver select prerelease targets; the manifest echoes
`candidateUniverse: STABLE_REGISTRY_VERSIONS`.

`objectiveCandidate` records the initial freshness candidate per row;
`objectiveNote` states remaining uncertainty. It is distinct from the final target
chosen after constraints. `objectiveEvidence.freshnessOptimality` is
`NOT_PROVEN`, and physical compatibility remains `UNKNOWN`. If latest selection
was not established for a mutable managed row, the objective status is `UNKNOWN`.
Selecting a version does not manufacture OSV evidence: missing exact-version
security evidence remains unknown in the projected health result.

`planningOptions.draftMaxCandidates` reports the effective evidence-search cap
(including configuration/default precedence). Zero means unbounded; null means
that the publishing API was not given an effective cap. The cap limits candidate
security evidence search. It does not cap the complete static solver domain, and
increasing only the deadline does not widen it.

## Semantic input identity v1

Legacy `inputHashes` and `inputFilesByProject` retain their Desktop byte contract.
New consumers should use the additive `semanticInputHash`,
`semanticInputsByProject` and `inputCoverage` fields when comparing local planning
inputs across runs or relocated checkouts.

The CLI captures these inputs during local inventory, before network enrichment:

- The selected manifest, the package-manager root manifest, the canonical parent
  lockfile and discovered workspace member manifests.
- Root and selected-package package-manager/runtime configuration, including
  `.npmrc`, `.yarnrc`, `.yarnrc.yml`, `pnpm-workspace.yaml`, `package-manager.json`,
  `.nvmrc` and `.node-version`.
- Actually loaded settings sources, group overrides, effective dependency
  overrides and effective registry/candidate-selection options.

Each record carries a logical name, read status and content/semantic digest. File
paths are kept for evidence but do not enter the semantic hash. Loaded settings
use canonical JSON with known output/cache locations removed and workspace/source
prefixes normalized. Effective dependency overrides exclude unrelated UI state
and timestamps. Changing the invocation output directory or generated state file
path alone does not change this identity. Changing their effective overrides does.
Manifest, lock and package-manager configuration bytes remain conservative hash
inputs, so a formatting-only edit can change their identity.

Missing or unreadable inputs are explicit. Failed topology resolution is
`UNKNOWN`; it is not silently reclassified as a standalone project. External
manifest paths, if supplied by an unsupported topology, are read at their actual
paths and keep coverage unknown.

`inputCoverage` describes **manifest inventory**. It lists discovered, evaluated
and unevaluated manifests. Hashing a sibling manifest does not mean that its
dependencies were jointly solved. A root-only or child-only workspace plan is
`PARTIAL` when other manifests remain unevaluated. `jointSolver` is always false
and `dependencyGraph` is `SELECTED_MANIFESTS_ONLY` in this contract. `COMPLETE`
means the discovered manifest inventory was evaluated in the selected scope;
it is not proof of the installed dependency graph or project behavior.

This identity is not a complete reproducibility certificate. It does not bind all
source code, external environment variables, registry/OSV snapshots, cache state,
Core/solver version or package-manager execution. Persist those separately when
comparing decisions or reproducing a migration.

## Storybook release cohorts

The exact-version Storybook cohort is an explicit, versioned membership table in
`storybook_release_policy.py`. The `@storybook/` namespace alone is not cohort
membership. Independently versioned compiler addons, including
`@storybook/addon-webpack5-compiler-babel`, retain their own release lines. Their
peer requirements still participate in normal solving, so removing the false
exact-version constraint does not suppress a genuine peer conflict. Unknown
addons are independent unless explicitly added to the release policy.

The membership table is a conservative product policy, not metadata proof that
every package name shares every historical or future release. Retired packages
can lack a later shared version and still require explicit migration/removal or
policy maintenance. The compiler fix does not claim that all such release-train
edge cases are solved.

Existing common-installable-version checks, current-version floors, registry
availability and ESLint plugin handling remain. Mutable-package and approved-major
bounds also apply to release-cohort candidate selection.

## Focused validation

The executable synthetic fixtures are `tests/test_draft_decision_contract.py` and
`tests/test_draft_input_identity.py`. They cover healthy-project freshness intent,
mutable peer context, approved-major bounds, independent compiler releases with a
real Z3 conflict fallback, RU/EN prompt contracts, frozen publication identity,
parent lock/root/sibling/config changes, relocation, generated state paths and
unknown input coverage. They make no claim about real-project migration runtime.
