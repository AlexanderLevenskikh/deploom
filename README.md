# DepLoom 0.2.225 — Logan composite lint optimization

This patch is for the supplied `deploom-master(4).zip` source snapshot.
The Marketer local link change is **already present in this source** and is NOT applied twice.

From the DepLoom repository root (PowerShell):

```powershell
python .\apply_logan_fix.py
python -m unittest discover -s tests -p "test_discovery_no_duplicate_composite.py" -v
python -m unittest discover -s tests -p "test_workspace_glob.py" -v
```

The test belongs to `tests/test_discovery_no_duplicate_composite.py`. Do not pass `tests/test_...` as a module name to `python -m unittest`.

Effect: when `lint` consists *exactly* of the already selected lint stage names joined with `&&`, prefer the one original `lint` script over redundant separate invocations. Preserves declared script sequence and full coverage; extra stages or shell syntax prevent reduction. Checks still fail when ESLint fails. No changes to source identity verification, Yarn install or its network retry behavior.

Actual marketer/logan Windows acceptance was not run here. Not a complete resolution of FIXED_SOURCE_IDENTITY_DRIFT_DURING_RESOLVER.
