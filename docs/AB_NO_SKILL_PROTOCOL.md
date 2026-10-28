# DepLoom pilot vNext — value of Core/blueprint, without Skill

**Status: protocol design, not a run or a result.** Run only after
`DepLoom/core` exists, the MCP pin is updated and the real-Core `required`
planning gate passes end to end.

## Competing hypotheses

- H1: a planner-produced, evidence-bound assignment reduces invalid target
  choices, peer-install failures, wasted edits and repair attempts.
- H0: with the same accessible registry/OSV metadata and strong execution
  prompt, an agent reaches the same accepted migration without material benefit.
- Do not use total speed as the only outcome. A wrong/unknown migration is not
  faster success, and an incomplete security check is not policy closure.

## Treatments — one difference

- **A: bare Codex agent**. No MCP, no DepLoom blueprint, and no DepLoom Skill.
  Can read the project, the frozen registry/OSV metadata and package docs.
- **B: Codex + DepLoom MCP**. Same model, prompt, project and external
  evidence; one extra capability to obtain a live-to-fixture Core blueprint.
  **No DepLoom Skill**. The blueprint is advisory; the agent may submit a
  documented evidence-based decision diff inside approved user boundaries.

A and B receive exactly the same user objective and scope. They are not given
the same precomputed target assignments: selecting compatible versions is the
work being evaluated. Neither arm is given a migration guide produced by
DepLoom. The broker/tool list for A must not advertise inaccessible MCP tools.

## Fixture proposal (build and freeze before dispatch)

A **single-manifest** realistic React 18/TypeScript application with roughly
12–16 direct dependencies, two interacting peer cohorts and one independent
cohort; no unsupported joint workspace solver. Include:
- Router 5→6 API source adaptation with nested routes, redirect replace,
  parameters, browser Back and 404 behavior;
- a companion whose peer requires a bounded major and an immutable consumer
  retaining a previous major; require actual peer resolution, not guesses;
- an independently versioned Storybook compiler addon so namespace-wide exact
  cohort logic cannot reappear;
- a more recent candidate with incomplete exact-version security evidence and
  a verified finding elsewhere in the dependency graph;
- at least one feasible alternate assignment. Keep scope/approved majors explicit
  and make it possible to achieve a documented outcome without guessing safety.

Use real package tarballs held behind a frozen test registry when practical;
freeze metadata, exact lockfile, OSV evidence, Node/npm/Python/tool versions and
git commit of the fixture. Record all seed hashes. Do not introduce proprietary
dependencies or corporate source.

## Gates for both arms

Baseline clean install, typecheck, lint, build, unit and targeted runtime smoke
must run before dispatch. A version-only API change should fail a behavioral
gate: otherwise the fixture is too weak.

At the **final** source/config/manifest/lock bytes, run an independent clean
install and replay identical lint/type/build/test/runtime commands, npm ls and
a pinned installed-tree audit. Keep nested audit exit code separate from wrapper
exit code. Score final policy attainment, severity, unknown evidence, direct
and transitive changes, and unmet user requirements separately. A pass in
functional checks does not imply Yellow or safe dependency tree.

Add a blind reviewer for behavior and a separate reviewer for the validity of
target assignments/security claims. The reviewer must not see arm labels,
timings or tokens before recording the verdict.

## Measurement and isolation

Fresh Codex sessions with the same explicit model and effort, disjoint home,
workspace, git cache, Skill catalog, tool grants, and output directories.
No global DepLoom Skill for either arm: record inherited prompts and check
loaded content. Give each agent the same registry and policy access; allow B
only the extra MCP tool. Run sequentially or measure concurrency explicitly.
No cross-arm artifacts; any operator repair/approval becomes an intervention
and is retained.

Retain complete prompts, MCP requests and compact/full outputs, actual Core
and MCP revisions, tool spans, failed/avoided installs, commands, exit codes,
final evidence receipts, source hashes and usage per unique response ID.
Keep cached input as a subset of input, reasoning as subset of output.
Calculate wall clock, verified permission waits and infrastructure waits
separately. Do not infer active thinking or MCP overhead from a long tool call.

## Analysis gate

Primary: accepted migration with no critical behavioral regression and an
honest policy/security verdict. Secondary: invalid assignments prevented,
repair cycles, unexplained retries, independent replay outcome, participant
time, input/output/cache and review effort. Compare only equivalent work.
Mark unresolved and truncated evidence as uncertainty, not success.

Start with one matched pair as a **diagnostic**. If both passes and the
difference looks meaningful, repeat independently (e.g. 3–5 runs per arm,
balanced order) before making performance claims. Stop early if the product
fails a correctness gate; debug that concrete case before more benchmark runs.

Future Skill experiment is a **separate** contrast: hold the blueprint fixed
and compare minimal data-only guidance against current full Skill, focusing
on automatic acceptance evidence and reduced manual proof work.
