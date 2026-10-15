"""Exercise the real PowerShell SemVer policy without Git/network side effects."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]

class ReleaseVersionPolicyTests(unittest.TestCase):
    def test_real_powershell_suggestions_overrides_and_strict_semver(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell unavailable on this platform')
        policy = str(ROOT / 'scripts' / 'release-version.ps1').replace("'", "''")
        command = ". '" + policy + "'; " + r'''
$ErrorActionPreference = 'Stop'
$cases = @{'1.0.0-preview.2'='v1.0.0-preview.3'; '1.0.0-preview.9+build.42'='v1.0.0-preview.10'; 'v2.3.4-beta'='v2.3.4-beta.1'; '1.0.0'='v1.0.1'; '0.3.7'='v0.3.8'}
foreach($version in $cases.Keys) { if((Get-NextReleaseTag $version) -ne $cases[$version]) { throw "Bad suggestion: $version" } }
$default = Resolve-ReleaseTag -CurrentVersion '1.0.0-preview.2' -ReadTag { param($suggested) '' }
$manual = Resolve-ReleaseTag -CurrentVersion '1.0.0-preview.2' -ReadTag { param($suggested) 'v1.2.0-rc.1+build.9' }
$explicit = Resolve-ReleaseTag -CurrentVersion '1.0.0-preview.2' -Tag 'v2.0.0-preview.1' -ReadTag { throw 'Explicit tag must not prompt' }
foreach($invalid in @('01.0.0','1.0.0-preview.01','1.0.0-','1.0.0-preview..1','1.0.0+','1.0.0.1','1.0.0+meta+extra')) { $rejected=$false;try{ConvertTo-ReleaseVersion $invalid | Out-Null}catch{$rejected=$true};if(-not $rejected){throw "Accepted invalid SemVer: $invalid"} }
@{default=$default.Tag;manual=$manual.Tag;explicit=$explicit.Tag} | ConvertTo-Json -Compress
'''
        result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        values = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual('v1.0.0-preview.3', values['default'])
        self.assertEqual('v1.2.0-rc.1+build.9', values['manual'])
        self.assertEqual('v2.0.0-preview.1', values['explicit'])
