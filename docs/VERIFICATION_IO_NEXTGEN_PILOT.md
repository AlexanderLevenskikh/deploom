# Verification I/O reduction: experimental delivery and evidence policy

This document is deliberately **not** a claim that the three-stage redesign is shipped.

## SourceSnapshot co-location (implemented)

The temporary sealed SourceSnapshot is placed in `<verification-root>/source-snapshots/` when that location is available and outside the authoritative source. The same verification-root configuration, including opt-in ReFS discovery, is honored. Invalid/unsafe destinations fall back to the normal verification cache or OS TEMP. Orphan cleanup scans both locations. The sealed/live manifests, source identity, and private-materialization integrity checks remain unchanged.

This removes **one cross-volume transition** from subsequent materialization when verification storage is on another volume. It does not eliminate the initial source copy, repeated hashing, or per-command private trials. It does not prove that `robocopy` uses ReFS block cloning; benchmark actual written bytes on Windows before claiming zero-copy.

## Differencing VHDX (isolated Windows PoC only)

Run in *elevated* Windows PowerShell with the Hyper-V PowerShell module installed. Create a disposable scratch directory outside your project, then:

```powershell
New-Item -ItemType Directory -Force 'D:\DepLoom\vhdx-poc' | Out-Null
.\scripts\benchmark-vhdx-cow.ps1 -ScratchRoot 'D:\DepLoom\vhdx-poc' -Files 1000 -FileKB 64 -Trials 3
```

The script generates a synthetic base, verifies that changes in each differencing child do not appear in later children, checks the base VHDX hash, measures creation, attach, write and detach, and saves a JSON report. It attempts cleanup even on failure. `-KeepArtifacts` retains its disposable files. No real user repository is touched. Its limitations are explicit: requires elevation and Hyper-V cmdlets; measures synthetic NTFS, not Yarn/Nx; a VHDX isolates a *volume*, not an arbitrary process's ability to write outside it. Child isolation is not process sandboxing.

**Never silently enable VHDX in production:** first confirm mount rights, handling of stable paths, cross-volume dependencies, background processes, symlink/junction behavior, crash recovery, parent immutability, COW efficiency, and cold + warm end-to-end trials. A registered backend with safe fallback and a Windows physical CI gate is required before integration.

## Fast-first (existing groundwork; new policy requires separate implementation)

Existing baseline screening, solver and exact verification already have different costs and proof meanings. The proposed next step is a *separate opt-in orchestration policy*:

- Explore: metadata/lockfile and solver screening; emits only diagnostic candidate hints.
- Validate: bounded physical attempts on selected candidates; any weakened independent-trial guarantees must remain `PRELIMINARY`.
- Certify: exact proof-preserving verification of a selected incumbent, the only path to `VERIFIED`.

Do **not** reuse `FAST` execution mode as synonymous with `PRELIMINARY`, and never weaken begin/C0 control validation. In particular, Yarn `--mode=update-lockfile` does not prove successful install/build. A full staged implementation also needs persistent state transitions, budget accounting, Desktop status, retries, and negative tests for false authority promotion; none are supplied by the VHDX benchmark or Snapshot relocation.

## Capture closure and progress follow-up (2026-10-13)

Repository scope now excludes `.claude` and `.codex` agent state by default. Git metadata and source dotfiles remain covered. Declared local dependencies into excluded paths fail closed; a diagnosed missing excluded input is infrastructure evidence, not a solver incompatibility. This policy does not exclude arbitrary hidden directories or backend outputs in repository mode.

Initial C0 capture emits serialized progress and heartbeats. Capture retries, durable copy and validation share a 1,800-second preparation budget. A filesystem operation and required cleanup may outlive this cooperative deadline; it is not a hard process watchdog. C0 control reuses the durable snapshot with original project/Git-root mapping, retaining live-source continuity guards instead of capturing the live tree again. Orphan cleanup checks age before traversing a retired tree for links.

A Windows synthetic capture A/B on identical input (1,000 source files at 4 KiB plus 3,000 agent-state files at 16 KiB) measured 22.013 / 7.280 seconds before versus 2.100 / 1.923 seconds after. Captured files fell from 4,001 to 1,001; logical bytes from 53,248,016 to 4,096,016. Runs were sequential on a warm host, unrelated orphan maintenance was excluded, and the first run includes cold-process/cache costs. These are not real-project measurements or an end-to-end product speedup claim.

A separate Windows private-copy fixture (1,000 files at 64 KiB) measured 0.581 / 0.567 / 0.628 seconds using robocopy. It excludes install/check/mount costs and cannot be compared directly with the VHDX PoC. The supplied Linux measurement used the portable fallback, so it provides no measured reflink improvement. Real-project acceptance and equivalent VHDX/private-clone A/B remain outstanding.

The first remote validation also exercised two previously uncovered boundaries: Windows watcher drain barriers left a newly created empty `.dependency-roadmap` directory in sealed C0 (preventing exact-membership cold compaction), and forbidden snapshot allocation propagated the wrong exception type. The barrier now removes only its own empty directory, retaining pre-existing/concurrent files; unsafe allocation raises `SourceCaptureError` immediately. Cold compaction remains strict. Regression coverage includes exact C0 membership, barrier cleanup and preservation, and the original injected-alias rejection.
