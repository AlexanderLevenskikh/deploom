#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '.idea', '.vs', 'node_modules', 'dist', 'dist-electron', 'release', '__pycache__', '.cache', '.npm'}
TEXT_EXT = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.json', '.jsonc', '.md', '.yml', '.yaml', '.ps1', '.sh', '.txt', '.toml', '.ini', '.cfg', '.html', '.css', '.xml', '.properties', ''}

# Opaque hashes of organization/project identifiers removed during the private -> public
# transition. The original strings are intentionally not stored in the public source tree.
DENY_SHA256 = {
    'cd88aa214b330d20198b006f68532e47262787868f5b57d184427e86f9021fc2',
    '4942d535f324ea9cbab4d1481e4788e43c59ee439f5ae5f01ba985bad58e292f',
    'd7fb23aff5f916e35f0dbaab14e0251fa08538c47968a86e2ce7b9b94d801c1a',
    '74164c43053f785b248d505f351c75c284794f4ce4b795dea191481855c7185f',
    '36ee76aaf7b538468f37d5d5b50c6fed3b23286eae2002bc7d9f2b068870e30d',
    '43bdec6f9cebb7c3cd2bce9be2d4d4de0d4212ede4d40c2931fe35de3c8763f5',
    'ca8f0554eac8b57585bd5719ecc5d4dfea1325faa71bfba667959fe41e7f6ec4',
    '310e2f32cc3aa644d68f736814105f3c5aae52f2269d75f6bd1a079400b16935',
    '8f927eef3ed83062e2f2dd5ba4ebeb0a8707d4d6f8d7116d64ec27a03444ffda',
    '72e1c698b3cfaf2d4e652e1d69bbbaa2f71447b324698d00041acb3c59449d71',
    'a44a488cb92ed80d96cf52bc07bd9bf7f0bfd2b2ff5b080b485c2e1b61175ffb',
    '893cbfb1bb02586fe747074a4455c8b8448d8160e54245e1c2720de70fbc2cd1',
    '4c0bcdb9c7ee070dcbc056a4d85b07129d617fb20d0a4e2e4401557382a6813b',
    'd758fa728449dc909d19eff32776747bcf6c9989998cede55828ac2526a490f3',
    '02305bc630dcbe27f264e765a7a9f7c7d8f1b7981efe2d115f50a49c7a1e21c6',
    '66e13e9cc787f529166dd1a1e3d59127b1e3128f601ff7ce2f17479ea26ca40a',
    '1f854f5d68565ce99ba9b9e2303164afa4d13ae40c5c227ca4f4ef0b97ec90f1',
    'b72e4af8a791c178d4b245b2e2de8f47f5cdde03ba56eee405f71553c5877e7a',
    'ab016216ea9e747eacc3cab6b1fc7fa406ec148a9f008b013c54af3af6c74939',
    'acd964180f23a1c1bfa326b18f1016a740fa63aa9613d89dfbbe823530712877',
    '6ae41a5406c94d9f24e3dcd65141cb239884c9a4edf600253ef0758b73e85b8e',
    'b491752f5845a57bf0e22bc1d57f32b60116d12bed2456e59190cbad0f607e09',
}

SECRETS = [
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('gitlab-token', re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}\b')),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('url-credentials', re.compile(r'https?://[^\s/:]+:[^\s/@]+@')),
    ('npm-auth-token', re.compile(r'(?i)(?:_authToken|npmAuthToken)\s*[=:]\s*[^$\s{][^\s]{7,}')),
]
FORBIDDEN_NAMES = {'.npmrc', '.netrc', '.yarnrc', 'id_rsa', 'id_ed25519'}
FORBIDDEN_SUFFIX = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'}
TOKEN_RE = re.compile(r'@[A-Za-z0-9._-]+/|[A-Za-z0-9_@.-]+(?:/[A-Za-z0-9_@.-]+)*')
PRIVATE_IP_RE = re.compile(r'\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b')
TICKET_RE = re.compile(r'\b[A-Z]{2,5}-\d{4,}\b')


def digest(value: str) -> str:
    return hashlib.sha256(value.lower().encode('utf-8')).hexdigest()


def candidate_identifiers(text: str):
    for match in TOKEN_RE.finditer(text):
        token = match.group(0).lower().strip('./')
        if not token:
            continue
        yield token
        if token.startswith('@') and '/' in token:
            yield token.split('/', 1)[0] + '/'
            yield token.split('/', 1)[0][1:]
        if '.' in token:
            parts = token.split('.')
            for idx in range(len(parts)):
                yield '.'.join(parts[idx:])
        if '/' in token:
            parts = token.split('/')
            for part in parts:
                if part:
                    yield part


def files():
    # Scan exactly the Git-tracked public release surface.
    # Local agent worktrees and downloaded patchers are not release inputs.
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot enumerate tracked public release surface: {exc}") from exc

    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("tracked path is not valid UTF-8") from exc
        path = ROOT / relative
        if not path.is_file():
            continue
        yield path


def forbidden_credential_file(path: Path) -> bool:
    name = path.name.lower()
    return name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIX or (name.startswith('.env') and name != '.env.example')


def main() -> int:
    errors: list[str] = []
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        if forbidden_credential_file(path):
            errors.append(f'credential-file: {rel}')
            continue
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = path.read_text('utf-8')
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRETS:
            match = pattern.search(text)
            if match:
                errors.append(f'{label}: {rel}: {match.group(0)[:120]!r}')
        private_ip = PRIVATE_IP_RE.search(text)
        if private_ip:
            errors.append(f'private-network-address: {rel}: {private_ip.group(0)}')
        ticket = TICKET_RE.search(text)
        if ticket:
            errors.append(f'private-looking-ticket-id: {rel}: {ticket.group(0)}')
        for candidate in candidate_identifiers(text):
            if digest(candidate) in DENY_SHA256:
                errors.append(f'opaque-private-identifier: {rel}')
                break

    lock = ROOT / 'desktop/package-lock.json'
    if lock.exists():
        data = json.loads(lock.read_text('utf-8'))
        for meta in (data.get('packages') or {}).values():
            if not isinstance(meta, dict):
                continue
            resolved = meta.get('resolved')
            if isinstance(resolved, str) and resolved.startswith(('http://', 'https://')):
                host = (urlparse(resolved).hostname or '').lower()
                if host not in {'registry.npmjs.org'}:
                    errors.append(f'non-public-lockfile-host: {host}')

    allow_path = ROOT / 'scripts/public-binary-allowlist.json'
    allow = json.loads(allow_path.read_text('utf-8')) if allow_path.exists() else {}
    actual: dict[str, str] = {}
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.pdf', '.exe', '.dll', '.zip'}:
            actual[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    for rel, file_digest in actual.items():
        if rel not in allow:
            errors.append(f'unreviewed-binary: {rel}')
        elif allow[rel] != file_digest:
            errors.append(f'changed-reviewed-binary: {rel}')
    for rel in allow:
        if rel not in actual:
            errors.append(f'allowlisted-binary-missing: {rel}')

    if errors:
        print('PUBLIC SANITIZATION FAILED')
        for error in errors:
            print(' -', error)
        return 1
    print('Public sanitization OK: no known private identifiers/secrets; reviewed binaries unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
