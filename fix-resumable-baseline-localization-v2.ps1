param(
    [string]$RepoRoot = "."
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path $RepoRoot).Path

Push-Location $RepoRoot
try {
    Write-Host "== Patch resumable Baseline localization v2 =="
    Write-Host "Partial v1 application is supported; existing Python changes will be kept."

    if (Test-Path ".\constraint_verify.py") {
        $constraintText = Get-Content ".\constraint_verify.py" -Raw -Encoding UTF8
        Write-Host ("ddmin resume API present: " + $constraintText.Contains("resume_state: Optional[Mapping[str, object]]"))
        Write-Host ("confirmation heartbeat present: " + $constraintText.Contains('"confirmation-heartbeat"'))
    }

    if (Test-Path ".\dependency_live_roadmap_generator.py") {
        $generatorText = Get-Content ".\dependency_live_roadmap_generator.py" -Raw -Encoding UTF8
        Write-Host ("checkpoint store present: " + $generatorText.Contains("class BaselineLocalizationCheckpointStore"))
    }

    @'
from pathlib import Path

root = Path(".")

# ---------------------------------------------------------------------------
# 1. constraint_verify.py
# ---------------------------------------------------------------------------
path = root / "constraint_verify.py"
text = path.read_text(encoding="utf-8")

if "CheckpointCallback = Callable[[Mapping[str, object]], None]" not in text:
    needle = "ProgressCallback = Callable[[str, Mapping[str, object]], None]\n"
    if needle not in text:
        raise SystemExit("constraint_verify.py: ProgressCallback anchor not found")
    text = text.replace(
        needle,
        needle + "CheckpointCallback = Callable[[Mapping[str, object]], None]\n",
        1,
    )

start = text.find("def parallel_ddmin(")
if start < 0:
    raise SystemExit("constraint_verify.py: parallel_ddmin not found")

prefix = text[:start]
new_function = r'''def parallel_ddmin(
    units: Sequence[VerificationUnit],
    fails: Callable[[Tuple[VerificationUnit, ...]], bool],
    *,
    parallelism: int = 4,
    max_checks: int = 24,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: float = 15.0,
    timeout_seconds: Optional[float] = None,
    confirm_failure: Optional[Callable[[Tuple[VerificationUnit, ...]], bool]] = None,
    resume_state: Optional[Mapping[str, object]] = None,
    checkpoint: Optional[CheckpointCallback] = None,
) -> Tuple[VerificationUnit, ...]:
    """Return a small failure-inducing subset using resumable parallel ddmin.

    Parallel FAIL is screening evidence only when ``confirm_failure`` is set.
    It may be cached across restarts, but it still cannot shrink the search
    until the same candidate fails again in an isolated serial confirmation.

    The checkpoint stores execution evidence, not solver authority.  A resumed
    invocation keeps current subset/cache/check budget, while wall-clock timeout
    starts fresh for the new process.
    """
    initial = tuple(units)
    if len(initial) <= 1 or max_checks <= 0:
        return initial

    unit_by_id = {item.id: item for item in initial}
    initial_ids = tuple(item.id for item in initial)
    if len(unit_by_id) != len(initial_ids):
        raise ValueError("VerificationUnit ids must be unique")

    current = initial
    cache: Dict[Tuple[str, ...], bool] = {}
    confirmed_failure_keys: set[Tuple[str, ...]] = set()
    checks = 0
    n = 2
    resumed_finished = False

    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    progress_interval_seconds = max(1.0, float(progress_interval_seconds or 15.0))

    def key(candidate: Sequence[VerificationUnit]) -> Tuple[str, ...]:
        return tuple(sorted(item.id for item in candidate))

    def package_count(candidate: Sequence[VerificationUnit]) -> int:
        return len({name for item in candidate for name in item.packages})

    def emit(event: str, **details: object) -> None:
        if progress is None:
            return
        progress(event, {
            "elapsedSeconds": round(time.monotonic() - started, 1),
            "checksStarted": checks,
            "maxChecks": max_checks,
            "currentUnits": len(current),
            **details,
        })

    def state_payload(reason: str, *, finished: bool = False) -> Dict[str, object]:
        entries = []
        for candidate_key in sorted(cache):
            entries.append({
                "unitIds": list(candidate_key),
                "failed": bool(cache[candidate_key]),
                "confirmedFailure": candidate_key in confirmed_failure_keys,
            })
        return {
            "schemaVersion": 1,
            "reason": reason,
            "initialUnitIds": list(initial_ids),
            "currentUnitIds": [item.id for item in current],
            "granularity": n,
            "checksStarted": checks,
            "cache": entries,
            "finished": finished,
        }

    def persist(reason: str, *, finished: bool = False) -> None:
        if checkpoint is None:
            return
        try:
            checkpoint(state_payload(reason, finished=finished))
        except Exception as exc:
            # Recovery cache failure must never change the solver result.
            emit("checkpoint-error", reason=reason, error=f"{type(exc).__name__}: {exc}")

    if resume_state is not None and int(resume_state.get("schemaVersion") or 0) == 1:
        raw_initial = resume_state.get("initialUnitIds")
        state_initial = tuple(str(item) for item in raw_initial) if isinstance(raw_initial, list) else ()
        if state_initial == initial_ids:
            raw_current = resume_state.get("currentUnitIds")
            current_ids = tuple(str(item) for item in raw_current) if isinstance(raw_current, list) else ()
            if (
                current_ids
                and len(set(current_ids)) == len(current_ids)
                and all(item_id in unit_by_id for item_id in current_ids)
            ):
                current = tuple(unit_by_id[item_id] for item_id in current_ids)
                try:
                    n = max(2, min(len(current), int(resume_state.get("granularity") or 2)))
                except (TypeError, ValueError):
                    n = 2
                try:
                    checks = max(0, min(max_checks, int(resume_state.get("checksStarted") or 0)))
                except (TypeError, ValueError):
                    checks = 0

                raw_cache = resume_state.get("cache")
                if isinstance(raw_cache, list):
                    for entry in raw_cache:
                        if not isinstance(entry, dict):
                            continue
                        raw_ids = entry.get("unitIds")
                        if not isinstance(raw_ids, list):
                            continue
                        ids = tuple(sorted(str(item) for item in raw_ids))
                        if not ids or any(item_id not in unit_by_id for item_id in ids):
                            continue
                        failed = bool(entry.get("failed"))
                        cache[ids] = failed
                        if failed and bool(entry.get("confirmedFailure")):
                            confirmed_failure_keys.add(ids)

                resumed_finished = bool(resume_state.get("finished"))
                emit(
                    "resume",
                    resumedUnits=len(current),
                    resumedPackages=package_count(current),
                    resumedChecks=checks,
                    cachedResults=len(cache),
                    confirmedFailures=len(confirmed_failure_keys),
                    finished=resumed_finished,
                )

    def evaluate_many(candidates: Sequence[Tuple[VerificationUnit, ...]], *, wave: str) -> List[bool | None]:
        nonlocal checks
        results: List[bool | None] = [None] * len(candidates)
        pending: List[Tuple[int, Tuple[VerificationUnit, ...], int]] = []
        for index, candidate in enumerate(candidates):
            candidate_key = key(candidate)
            if candidate_key in cache:
                results[index] = cache[candidate_key]
            elif checks < max_checks:
                checks += 1
                pending.append((index, candidate, checks))
        if not pending:
            return results

        persist("wave-start")
        workers = max(1, min(parallelism, len(pending)))
        emit("wave-start", wave=wave, candidates=len(pending), active=workers)
        pool = ThreadPoolExecutor(max_workers=workers)
        future_map = {}

        def run_candidate(candidate: Tuple[VerificationUnit, ...], check_number: int) -> bool:
            check_started = time.monotonic()
            emit(
                "check-start",
                wave=wave,
                check=check_number,
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )
            try:
                value = bool(fails(candidate))
                emit(
                    "check-finish",
                    wave=wave,
                    check=check_number,
                    failed=value,
                    checkElapsedSeconds=round(time.monotonic() - check_started, 1),
                    candidateUnits=len(candidate),
                    candidatePackages=package_count(candidate),
                )
                return value
            except Exception as exc:
                emit(
                    "check-error",
                    wave=wave,
                    check=check_number,
                    error=f"{type(exc).__name__}: {exc}",
                    checkElapsedSeconds=round(time.monotonic() - check_started, 1),
                )
                raise

        try:
            for index, candidate, check_number in pending:
                future = pool.submit(run_candidate, candidate, check_number)
                future_map[future] = (index, candidate, check_number)

            unfinished = set(future_map)
            last_heartbeat = time.monotonic()
            while unfinished:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    for future in unfinished:
                        future.cancel()
                    emit("timeout", wave=wave, active=len(unfinished), timeoutSeconds=timeout_seconds)
                    persist("timeout")
                    raise LocalizationTimeoutError(
                        f"localization exceeded {int(timeout_seconds or 0)}s with {len(unfinished)} check(s) still active"
                    )
                wait_for = progress_interval_seconds
                if deadline is not None:
                    wait_for = max(0.1, min(wait_for, deadline - now))
                done, unfinished = wait(unfinished, timeout=wait_for, return_when=FIRST_COMPLETED)
                if not done:
                    emit("heartbeat", wave=wave, active=len(unfinished), completed=len(future_map) - len(unfinished))
                    last_heartbeat = time.monotonic()
                    continue

                for future in sorted(done, key=lambda item: future_map[item][0]):
                    index, candidate, _check_number = future_map[future]
                    value = bool(future.result())
                    cache[key(candidate)] = value
                    results[index] = value
                    persist("check-finish")

                if time.monotonic() - last_heartbeat >= progress_interval_seconds:
                    emit("heartbeat", wave=wave, active=len(unfinished), completed=len(future_map) - len(unfinished))
                    last_heartbeat = time.monotonic()
        except Exception:
            for future in future_map:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
            emit("wave-finish", wave=wave, candidates=len(pending), active=0)
        return results

    def first_confirmed_failure(
        candidates: Sequence[Tuple[VerificationUnit, ...]],
        results: Sequence[bool | None],
        *,
        wave: str,
    ) -> Optional[Tuple[VerificationUnit, ...]]:
        for index, value in enumerate(results):
            if value is not True:
                continue
            candidate = candidates[index]
            candidate_key = key(candidate)
            if confirm_failure is None or candidate_key in confirmed_failure_keys:
                return candidate

            confirmation_started = time.monotonic()
            emit(
                "confirmation-start",
                wave=wave,
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )

            # Strictly one proof worker. Coordinator remains free to emit output.
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(confirm_failure, candidate)
            try:
                while not future.done():
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        future.cancel()
                        emit(
                            "timeout",
                            wave=wave,
                            phase="confirmation",
                            candidateUnits=len(candidate),
                            candidatePackages=package_count(candidate),
                            timeoutSeconds=timeout_seconds,
                        )
                        persist("confirmation-timeout")
                        raise LocalizationTimeoutError(
                            f"localization exceeded {int(timeout_seconds or 0)}s during serial confirmation"
                        )

                    wait_for = progress_interval_seconds
                    if deadline is not None:
                        wait_for = max(0.1, min(wait_for, deadline - now))
                    done, _ = wait({future}, timeout=wait_for, return_when=FIRST_COMPLETED)
                    if not done:
                        emit(
                            "confirmation-heartbeat",
                            wave=wave,
                            confirmationElapsedSeconds=round(time.monotonic() - confirmation_started, 1),
                            candidateUnits=len(candidate),
                            candidatePackages=package_count(candidate),
                        )

                confirmed = bool(future.result())
            except Exception:
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

            emit(
                "confirmation-finish",
                wave=wave,
                confirmed=confirmed,
                confirmationElapsedSeconds=round(time.monotonic() - confirmation_started, 1),
                candidateUnits=len(candidate),
                candidatePackages=package_count(candidate),
            )

            cache[candidate_key] = confirmed
            if confirmed:
                confirmed_failure_keys.add(candidate_key)
            else:
                confirmed_failure_keys.discard(candidate_key)
            persist("confirmation-finish")

            if confirmed:
                return candidate
        return None

    emit("start", units=len(current), packages=package_count(current), parallelism=parallelism)
    if resumed_finished:
        emit("finish", units=len(current), packages=package_count(current), resumed=True)
        return current

    while len(current) >= 2 and checks < max_checks:
        parts = [tuple(part) for part in _partitions(current, n)]
        subset_wave = f"subsets/{n}"
        subset_results = evaluate_many(parts, wave=subset_wave)
        failing_subset = first_confirmed_failure(parts, subset_results, wave=subset_wave)
        if failing_subset is not None:
            current = failing_subset
            n = max(2, n - 1)
            emit("shrink", reason="failing-subset", units=len(current), packages=package_count(current))
            persist("shrink")
            continue

        complements = [tuple(item for item in current if item not in part) for part in parts]
        complements = [candidate for candidate in complements if candidate]
        complement_wave = f"complements/{n}"
        complement_results = evaluate_many(complements, wave=complement_wave)
        failing_complement = first_confirmed_failure(complements, complement_results, wave=complement_wave)
        if failing_complement is not None:
            current = failing_complement
            n = max(2, n - 1)
            emit("shrink", reason="failing-complement", units=len(current), packages=package_count(current))
            persist("shrink")
            continue

        if n >= len(current):
            break
        n = min(len(current), n * 2)
        persist("granularity")

    emit("finish", units=len(current), packages=package_count(current))
    persist("finish", finished=True)
    return current
'''

path.write_text(prefix + new_function + "\n", encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------
# 2. tests/test_constraint_verify.py
# ---------------------------------------------------------------------------
test_path = root / "tests" / "test_constraint_verify.py"
tests = test_path.read_text(encoding="utf-8")
anchor = "    def test_parallel_ddmin_has_total_watchdog(self) -> None:\n"
if anchor not in tests:
    raise SystemExit("tests/test_constraint_verify.py: insertion anchor not found")

addition = r'''    def test_serial_confirmation_emits_heartbeat(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def slow_confirmation(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(1.15)
            return True

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=slow_confirmation,
            progress=lambda event, _details: events.append(event),
            progress_interval_seconds=1,
            timeout_seconds=5,
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        self.assertIn("confirmation-heartbeat", events)

    def test_resume_reuses_screening_but_still_confirms_fail(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        parallel_calls = 0
        confirmation_calls = 0

        def parallel_screen(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal parallel_calls
            parallel_calls += 1
            raise AssertionError("cached screening result should be reused")

        def confirm(candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal confirmation_calls
            confirmation_calls += 1
            return {item.id for item in candidate} == {"a"}

        state = {
            "schemaVersion": 1,
            "initialUnitIds": ["a", "b"],
            "currentUnitIds": ["a", "b"],
            "granularity": 2,
            "checksStarted": 2,
            "cache": [
                {"unitIds": ["a"], "failed": True, "confirmedFailure": False},
                {"unitIds": ["b"], "failed": False, "confirmedFailure": False},
            ],
            "finished": False,
        }

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=confirm,
            resume_state=state,
        )
        self.assertEqual(0, parallel_calls)
        self.assertEqual(1, confirmation_calls)
        self.assertEqual({"a"}, {item.id for item in culprit})

    def test_checkpoint_can_resume_finished_localization_without_rechecks(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        states: list[dict[str, object]] = []

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        culprit = parallel_ddmin(
            units,
            fails,
            parallelism=2,
            max_checks=8,
            confirm_failure=fails,
            checkpoint=lambda state: states.append(dict(state)),
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        final_state = states[-1]
        self.assertEqual(["a"], final_state["currentUnitIds"])
        self.assertTrue(final_state["finished"])

        calls = 0
        def should_not_run(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal calls
            calls += 1
            return True

        resumed = parallel_ddmin(
            units,
            should_not_run,
            parallelism=2,
            max_checks=8,
            confirm_failure=should_not_run,
            resume_state=final_state,
        )
        self.assertEqual(0, calls)
        self.assertEqual({"a"}, {item.id for item in resumed})

    def test_serial_confirmation_obeys_total_watchdog(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def hangs(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(0.5)
            return True

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units,
                parallel_screen,
                parallelism=2,
                max_checks=8,
                confirm_failure=hangs,
                progress=lambda event, _details: events.append(event),
                progress_interval_seconds=1,
                timeout_seconds=0.1,
            )
        self.assertIn("confirmation-heartbeat", events)
        self.assertIn("timeout", events)

'''
if "test_resume_reuses_screening_but_still_confirms_fail" not in tests:
    tests = tests.replace(anchor, addition + anchor, 1)
test_path.write_text(tests, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------
# 3. dependency_live_roadmap_generator.py
# ---------------------------------------------------------------------------
gen_path = root / "dependency_live_roadmap_generator.py"
gen = gen_path.read_text(encoding="utf-8")

checkpoint_class = r'''

class BaselineLocalizationCheckpointStore:
    """Durable ddmin recovery state guarded by an exact proof identity."""

    def __init__(self, progress_path: Optional[Path]) -> None:
        self.path = (
            progress_path.with_name("baseline-localization-checkpoint.json")
            if progress_path is not None
            else None
        )
        self._lock = threading.Lock()

    @staticmethod
    def _slot(project: str, mode: str) -> str:
        return hashlib.sha256(f"{project}\0{mode}".encode("utf-8")).hexdigest()[:24]

    def _read_locked(self) -> Dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {"schemaVersion": 1, "entries": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schemaVersion": 1, "entries": {}}
        if not isinstance(payload, dict) or int(payload.get("schemaVersion") or 0) != 1:
            return {"schemaVersion": 1, "entries": {}}
        if not isinstance(payload.get("entries"), dict):
            payload["entries"] = {}
        return payload

    def _write_locked(self, payload: Dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def load(self, project: str, mode: str, identity: str) -> Optional[Dict[str, object]]:
        if self.path is None:
            return None
        with self._lock:
            payload = self._read_locked()
            entry = payload.get("entries", {}).get(self._slot(project, mode))
            if not isinstance(entry, dict) or entry.get("identity") != identity:
                return None
            state = entry.get("state")
            return dict(state) if isinstance(state, dict) else None

    def save(self, project: str, mode: str, identity: str, state: Mapping[str, object]) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            entries = payload.setdefault("entries", {})
            entries[self._slot(project, mode)] = {
                "identity": identity,
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "state": dict(state),
            }
            self._write_locked(payload)

    def clear(self, project: str, mode: str) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return
            if entries.pop(self._slot(project, mode), None) is not None:
                self._write_locked(payload)
'''

resolve_anchor = "\n\ndef resolve_peer_compatibility_with_verification(\n"
if "class BaselineLocalizationCheckpointStore:" not in gen:
    if resolve_anchor not in gen:
        raise SystemExit("generator: resolve function anchor not found")
    gen = gen.replace(resolve_anchor, checkpoint_class + resolve_anchor, 1)

init_anchor = "    progress_reporter = BaselineProgressReporter(progress_path)\n"
if "localization_checkpoint_store = BaselineLocalizationCheckpointStore(progress_path)" not in gen:
    if init_anchor not in gen:
        raise SystemExit("generator: progress reporter anchor not found")
    gen = gen.replace(
        init_anchor,
        init_anchor + "    localization_checkpoint_store = BaselineLocalizationCheckpointStore(progress_path)\n",
        1,
    )

old_filter = '                    if event in {"start", "wave-start", "heartbeat", "check-finish", "confirmation-start", "confirmation-finish", "shrink", "timeout", "finish"}:\n'
new_filter = '                    if event in {"start", "resume", "wave-start", "heartbeat", "check-finish", "confirmation-start", "confirmation-heartbeat", "confirmation-finish", "shrink", "timeout", "checkpoint-error", "finish"}:\n'
if new_filter not in gen:
    if old_filter not in gen:
        raise SystemExit("generator: localization event filter anchor not found")
    gen = gen.replace(old_filter, new_filter, 1)

call_anchor = '''                try:\n                    culprit_units = parallel_ddmin(\n'''
resume_prelude = r'''                try:
                    source_head_result = subprocess.run(
                        ["git", "-C", str(spec.path), "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    source_head = source_head_result.stdout.strip() if source_head_result.returncode == 0 else ""
                except (OSError, subprocess.SubprocessError):
                    source_head = ""

                localization_identity_payload = {
                    "algorithm": "baseline-ddmin-resume-v1",
                    "sourceHead": source_head,
                    "environment": project_environment_fingerprint,
                    "mode": mode,
                    "assignment": sorted(assignment.items()),
                    "commands": list(config.commands),
                    "projectChecks": config.project_checks,
                    "units": [{"id": unit.id, "packages": list(unit.packages)} for unit in units],
                }
                localization_identity = hashlib.sha256(
                    json.dumps(
                        localization_identity_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                localization_resume_state = localization_checkpoint_store.load(
                    project, mode, localization_identity
                )
                if localization_resume_state is not None:
                    eprint(
                        f"[info] {project}: Baseline localization {mode} resume checkpoint found; "
                        f"currentUnits={len(localization_resume_state.get('currentUnitIds') or [])}, "
                        f"checksStarted={localization_resume_state.get('checksStarted', 0)}, "
                        f"reason={localization_resume_state.get('reason', 'unknown')}"
                    )

                try:
                    culprit_units = parallel_ddmin(
'''
if "localization_identity_payload = {" not in gen:
    if call_anchor not in gen:
        raise SystemExit("generator: parallel_ddmin call anchor not found")
    gen = gen.replace(call_anchor, resume_prelude, 1)

old_args = '''                        confirm_failure=subset_fails if learn_project_failure else None,\n                    )\n'''
new_args = '''                        confirm_failure=subset_fails if learn_project_failure else None,
                        resume_state=localization_resume_state,
                        checkpoint=lambda state: localization_checkpoint_store.save(
                            project, mode, localization_identity, state
                        ),
                    )
'''
if new_args not in gen:
    if old_args not in gen:
        raise SystemExit("generator: parallel_ddmin argument tail not found")
    gen = gen.replace(old_args, new_args, 1)

learn_anchor = '''                learned[project][mode].append(nogood)\n                detail = ", ".join(f"{name}@{version}" for name, version in sorted(nogood.items()))\n'''
learn_replacement = '''                learned[project][mode].append(nogood)
                # Keep the finished localization checkpoint through project
                # reproductions; clear only after the clause becomes solver authority.
                localization_checkpoint_store.clear(project, mode)
                detail = ", ".join(f"{name}@{version}" for name, version in sorted(nogood.items()))
'''
if "localization_checkpoint_store.clear(project, mode)" not in gen:
    if learn_anchor not in gen:
        raise SystemExit("generator: learned constraint anchor not found")
    gen = gen.replace(learn_anchor, learn_replacement, 1)

gen_path.write_text(gen, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------
# 4. Desktop watchdog
# ---------------------------------------------------------------------------
main_path = root / "desktop" / "electron" / "main.ts"
main = main_path.read_text(encoding="utf-8")

type_anchor = "  stallAbortMs?: number\n"
if "stallWarningSeverity?: 'info' | 'warn'" not in main:
    if type_anchor not in main:
        raise SystemExit("desktop main: stallAbortMs type anchor not found")
    main = main.replace(type_anchor, type_anchor + "  stallWarningSeverity?: 'info' | 'warn'\n", 1)

old_pair = "        stallWarningMs: 2 * 60_000,\n        stallAbortMs: 15 * 60_000,\n"
new_pair = "        stallWarningMs: 8 * 60_000,\n        stallAbortMs: 30 * 60_000,\n        stallWarningSeverity: 'info',\n"
generator_watchdog_already_patched = main.count(new_pair)
if generator_watchdog_already_patched < 3:
    missing = 3 - generator_watchdog_already_patched
    count = main.count(old_pair)
    if count < missing:
        raise SystemExit(
            f"desktop main: need {missing} more generator watchdog block(s), "
            f"but found only {count} legacy 2m/15m block(s)"
        )
    main = main.replace(old_pair, new_pair, missing)

new_warning_marker = "line: spec.stallWarningSeverity === 'info'"
if new_warning_marker not in main:
    lines = main.splitlines(keepends=True)
    warning_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "line:" in line
            and "[warn]" in line
            and "нет новых сообщений" in line
            and "${spec.label}" in line
        ),
        None,
    )
    if warning_index is None:
        raise SystemExit(
            "desktop main: could not locate generic stall-warning line structurally"
        )

    original_line = lines[warning_index]
    indent = original_line[: len(original_line) - len(original_line.lstrip())]
    newline = "\r\n" if original_line.endswith("\r\n") else "\n"
    lines[warning_index] = (
        indent + "line: spec.stallWarningSeverity === 'info'" + newline
        + indent + "  ? `[info] ⏳ ${spec.label}: длительная операция без нового вывода ${Math.max(1, Math.floor(silentMs / 60_000))} мин. Процесс ещё запущен; продолжаю ожидать внутренний heartbeat/progress.`" + newline
        + indent + "  : `[warn] ⚠ ${spec.label}: нет новых сообщений ${Math.max(1, Math.floor(silentMs / 60_000))} мин. Процесс ещё запущен; внутренний watchdog продолжает контролировать subprocess.`," + newline
    )
    main = "".join(lines)

main_path.write_text(main, encoding="utf-8", newline="\n")

print("Patched resumable Baseline localization + watchdog hardening.")
'@ | python -
    if ($LASTEXITCODE -ne 0) { throw "Patch application failed" }

    Write-Host ""
    Write-Host "== Constraint verifier regression tests =="
    python -m unittest tests.test_constraint_verify -v
    if ($LASTEXITCODE -ne 0) { throw "constraint_verify regression tests failed" }

    Write-Host ""
    Write-Host "== Full deterministic tool suite =="
    python .\run_tool_tests.py --suite all
    if ($LASTEXITCODE -ne 0) { throw "full tool suite failed" }

    Write-Host ""
    Write-Host "== Desktop build/contracts =="
    Push-Location ".\desktop"
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "desktop build failed" }

        npm run check:cross-platform
        if ($LASTEXITCODE -ne 0) { throw "check:cross-platform failed" }

        npm run check:process-launcher
        if ($LASTEXITCODE -ne 0) { throw "check:process-launcher failed" }
    }
    finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "== Public sanitization =="
    python .\scripts\check-public-sanitization.py
    if ($LASTEXITCODE -ne 0) { throw "public sanitization failed" }

    Write-Host ""
    Write-Host "== Diff integrity =="
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw "git diff --check failed" }

    Write-Host ""
    Write-Host "== Changed files =="
    git status --short

    Write-Host ""
    Write-Host "== Review patch =="
    git diff -- `
        constraint_verify.py `
        tests/test_constraint_verify.py `
        dependency_live_roadmap_generator.py `
        desktop/electron/main.ts
}
finally {
    Pop-Location
}
