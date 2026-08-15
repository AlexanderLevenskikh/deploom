# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for suspected credentials, private-data exposure, or a security vulnerability. Use GitHub private vulnerability reporting when it is enabled for the repository, or contact the maintainer privately.

## Public-release hygiene

This repository includes `scripts/check-public-sanitization.py`, which blocks known private identifiers, common credential formats, credential files, non-public lockfile registry hosts, and unreviewed binary/image changes.

The check is defense in depth, not a guarantee that arbitrary confidential information can never be committed. Review changes before publication and enable GitHub secret scanning/push protection where available.
