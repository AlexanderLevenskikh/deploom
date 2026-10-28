# Extraction boundaries

**Current phase: staging branch.** The host GitHub connector can create Git
objects and branches, but cannot provision a new repository. This branch is a
self-contained, minimal-tree bootstrap intended for import into `DepLoom/core`.

Source: `b7f13c401e0e77906307eb2f7bd4dc61c57e2068`; imported runtime: 133 files from the pinned MCP
Core release inventory. Added: lightweight Python package metadata, focused
solver/intent/input-identity tests and a small CI matrix.

The change to `peer_solver_transition.py` distinguishes a final-only
`USER_BASELINE_REQUIRED_UPDATE` intent constraint from intermediate
transition invariants. The real final Z3 model **retains** the requirement;
peer and keep-current constraints continue to be checked. Regression tests
cover an allowed required upgrade, mandatory peer cohort, invalid final peer
tuple, and contradictory required/keep-current intent.

Follow-up before experiment:
1. Import the prepared headless tree into a new repository, retaining its
   commit hash and source bytes.
2. Repoint the MCP pinned Core submodule and verify the exact 133-file inventory,
   bundle wheel, stdio startup and tool response.
3. Confirm real end-to-end planning with `required` returns a DRAFT_READY
   blueprint (or an honest typed blocker) rather than INVALID_CURRENT_ENDPOINT.
4. Freeze versions, registry evidence, prompts, and acceptance gates for a
   harder **agent + MCP vs bare agent** experiment, with **no Skill** assigned
   to either arm. Require equal task and security checks.

The full old repository remains available as historical Desktop/execution
source. This extraction is not itself proof of migration correctness, global
freshness optimality or security of an installed dependency tree.
