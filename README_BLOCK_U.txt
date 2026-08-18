DepLoom Block U overlay v3 (Windows-lock-safe)
===============================================

Install from the extracted directory:

  py .\install_block_u.py --root C:\path\to\deploom

or from the repository root:

  py .\path\to\install_block_u.py --root .

What changed in v3 installer:
- no Git/blob SHA checks;
- builds and syntax-checks the whole patch before touching target files;
- never uses py_compile/__pycache__ in the target repo;
- Windows file writes retry WinError 5/32/33 for up to 30 seconds;
- writes support modules first and verifier/generator last;
- keeps a recovery backup under .dependency-roadmap/block-u-backup-*;
- rollback reports the exact file if Windows still refuses access;
- missing BLOCK_U2_GRANULAR_FALLBACK_V1 is informational, not an error.

Proof model:
- platform workspace acceleration is not authority;
- successful candidate still needs the full configured ProjectProof;
- adaptive early screen only rejects after introduced structural evidence is
  confirmed against current-baseline control;
- infrastructure/unknown remains fail-closed.
