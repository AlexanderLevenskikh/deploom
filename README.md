# DepLoom Core — headless extraction bootstrap

Independent Python source distribution for deterministic **planning-only**
dependency updates. No Electron, GUI, Desktop build, physical migration runner
or historical monorepo test suite is required to develop and test the Draft
decision engine.

This bootstrap preserves the **133-file headless runtime inventory** shipped by
DepLoom MCP 0.2.0, then applies one focused bug fix. It intentionally does not
claim that every retained Python module is on the minimal import path. Prune
the runtime only with import/behavior evidence in later PRs.

## Local development

Requires Python 3.12 or 3.13.

```sh
python -m venv .venv
python -m pip install -e .
python -m unittest discover -s tests -p "test_peer_solver_transition.py" -v
python -m unittest discover -s tests -p "test_required_transition.py" -v
python -m unittest discover -s tests -p "test_draft_decision_contract.py" -v
python -m unittest discover -s tests -p "test_draft_input_identity.py" -v
```

CI runs these focused synthetic fixtures on Linux and Windows. It does **not**
run Desktop, source migration, npm installs, broad historical integration,
or a benchmark. The headless planner remains `PLANNING_ONLY`/`NOT_VERIFIED`.

## Provenance and migration

The initial inventory is pinned to
`b7f13c401e0e77906307eb2f7bd4dc61c57e2068` from
[AlexanderLevenskikh/deploom](https://github.com/AlexanderLevenskikh/deploom).
The corrected module is `peer_solver_transition.py`. Source filenames are
preserved so existing imports and the MCP content inventory can be repointed
without a simultaneous rearchitecture.

The original repository is unchanged on its default branch by this bootstrap.
Import this branch into a new `DepLoom/core` repository, then update MCP's
submodule URL, revision and release SHA-256 inventory in one reviewed change.
Do not switch MCP to a new source without regenerating the delivery manifest and
running installed-wheel smoke.

See [extraction status](docs/EXTRACTION_STATUS.md).
