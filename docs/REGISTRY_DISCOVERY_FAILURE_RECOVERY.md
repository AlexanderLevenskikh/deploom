# Registry discovery after a successful project check

DepLoom's initial project check and target discovery are separate operations.
A successful build/test is not retroactively a failure when registry discovery
cannot establish upgrade targets. `DISCOVERY_UNSETTLED` means the migration has
**not started**; it does not mean the current dependency tree failed build/test.

## Yarn Berry / workspace scope

Yarn Berry can define npm registries and authentication by scope in the manager
root's `.yarnrc.yml`. `npm view` does not automatically inherit these settings.
Discovery now queries `yarn npm info` from the canonical **package-manager
root**, with the project's selected Node environment, whenever the canonical
lockfile identifies a modern Yarn project. npm-based projects continue to use
`npm view` from the canonical lockfile root; no external registry or token is
invented. Yarn Classic behavior is unchanged.

The registry metadata response must carry an exact, published version and a
matching `dist-tags.latest`. Alternatives are checked against the installed
lockfile version and the selected Node engine requirement as before. Unknown
metadata remains an abstention, not a learned incompatibility or success.

Errors are classified locally (`authentication`, `not-found`, `network-or-tls`,
`timeout`, `metadata-invalid`, `command-failed`, `yarn-cli-unavailable`, etc.).
Raw stderr, URLs containing credentials, tokens and secret environment values
are never forwarded as failure diagnostics.

## One cheap check before retrying the migration

In the original project's **package-manager root** (not a temporary DepLoom
trial), run a read-only command with the SAME Node/Yarn environment as DepLoom:

```powershell
Set-Location 'C:\path\to\package-manager-root'
yarn npm info react --fields version,dist-tags,versions,engines --json
```

If it fails, inspect local network/proxy/registry settings and Yarn's auth.
Do not paste private `.yarnrc.yml`, `.npmrc`, tokens or detailed registry
responses into tickets or chats. When sharing logs, redact credentials.

The command **does not install dependencies or mutate the project**. A passing
probe indicates the selected registry client can read metadata; it does not
certify the full migration or guarantee every package is available.

## Retrying preparation without losing discovery progress

Desktop preserves the structured `DISCOVERY_UNSETTLED` failure even after a
long stream of progress. The retry action repeats discovery rather than the
separate successful project check. A new migration still performs its own
source-bound C0 control; the earlier project check is not C0 proof.

`preparation-discovery.json` under the run directory atomically saves each
completed package probe. A repeated begin reuses settled planning rows for up
to 24 hours and probes the remaining packages. The original probe timestamp
is carried in `discoveryCheckpoint.savedAt`; registry tags may have advanced
since that probe, so this is dated planning evidence. Unavailable registry responses,
budget skips and metadata errors are retried. Completed probes with registry
errors are also saved as diagnostics, independently of the Baseline result;
an error row never skips a new request. Replaying a row does not extend
its lifetime. The `begin.discovery-resumed` event reports how many rows were
reused.

Reuse is bound to the project and manager manifests, canonical lockfile,
inherited registry configuration, environment, selected runtime, intent and
tool implementation. Configuration and environment values are hashed; the
journal does not contain authentication values. Changed inputs or a corrupt
journal trigger fresh discovery. Source capture and C0 verification still run
with their ordinary guards; planning progress cannot grant a verified result.

## Tests

```powershell
python -m unittest tests.regression.test_iterative_yarn_registry_discovery -v
python -m unittest tests.regression.test_iterative_registry_metadata -v
python -m unittest tests.regression.test_iterative_target_discovery -v
```

On Windows with a configured private Yarn Berry registry, the final acceptance
requires a real discovery attempt after a successful read-only probe. CI unit
fixtures deliberately avoid calls to a corporate registry.
