# Dependency Graph — explanatory projection

The Desktop `Graph` tab visualizes the current project dependency surface without changing DepLoom proof authority.

## Views

- **Packages** — direct dependencies, current/planned versions, user policy, and locally observed direct-to-direct manifest relations.
- **Groups** — active/deferred compatibility neighborhoods plus clearly labelled ecosystem priors. Groups may overlap and expose bridge packages.
- **Verified Scope** — projects the latest Flow/Baseline state, required/deferred scope, migration integration facts, and current health.

## Authority boundary

The graph is read-only.

- Direct package membership comes from the project manifest / existing Baseline intent plan.
- `dependency`, `peer`, and `optional` edges are shown only when the installed direct package manifest is locally observable under `node_modules`. Missing manifests produce missing coverage, not guessed edges.
- Suggested/inferred groups are `DIAGNOSTIC_HINT`.
- A deferred group reflects `USER_POLICY` only for the actual `keep-current` scope decision; confidence/reasons/group shape remain explanatory metadata.
- Verified scope reflects existing Flow/Baseline state. It does not create package-level proof.
- The graph never creates Solver clauses, compatibility facts, cache identities, or verification results.

## Current blockage

When a Baseline decision contains a suggested cohort, the Graph tab can focus on that neighborhood and shows predicate, suggested group/confidence, packages, and bridge/boundary packages. The actual defer/revisit action remains in the existing FLOW UX.

## Scale

The graph intentionally visualizes direct packages and bounded explanatory groups rather than the full transitive dependency universe. This keeps projects with roughly 70–150 direct dependencies readable and avoids turning transitive metadata into a false proof surface.
