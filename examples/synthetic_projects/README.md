# Small DepLoom synthetic projects

These are deliberately tiny **real package-manager projects**, unlike
`examples/synthetic_baseline_scenarios.py` which tests only the in-memory model.

Materialize them into independent Git repositories:

```powershell
python examples/synthetic_projects/materialize.py --out C:\Temp\deploom-synthetic
```

Then add any generated repository to DepLoom Desktop.

Recommended order:

1. `tiny-basic` — tiny ordinary npm project.
2. `tiny-types-stub` — `uuid` + old `@types/uuid`; exercises type-stub removal.
3. `tiny-vite-vitest` — small compatibility pair around the Vite/Vitest boundary.
4. `nested-single-package` — select the **repository root**, not `frontend/`;
   this is the regression fixture for package.json below the Git root.

The materializer runs `npm install --package-lock-only --ignore-scripts`, then
initializes and commits a local Git repository. Use `--skip-lockfile` only for
layout inspection; a real Baseline normally needs the generated lockfile.
