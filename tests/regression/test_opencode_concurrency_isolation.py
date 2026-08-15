from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / 'desktop' / 'electron' / 'main.ts').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'desktop' / 'electron' / 'opencode-runtime.ts').read_text(encoding='utf-8')


class OpenCodeConcurrencyIsolationTests(unittest.TestCase):
    def test_flow_uses_one_sidecar_owner_for_parallel_workers(self):
        self.assertIn('function openCodeTransportOwner(job: JobRecord): JobRecord', MAIN)
        self.assertIn('return migrationRootJob(job)', MAIN)
        self.assertIn('if (owner !== job) return', MAIN)
        self.assertIn('параллельные агенты подключаются к одному sidecar', MAIN)
        self.assertIn('if (owner.openCodeServerStarting) return owner.openCodeServerStarting', MAIN)
        self.assertIn('owner.openCodeServerStarting = starting', MAIN)

    def test_server_database_is_private_to_flow_runtime(self):
        self.assertIn("OPENCODE_DB", RUNTIME)
        self.assertIn("dependency-flow-opencode", RUNTIME)
        self.assertIn('openCodeDatabaseEnv(process.env, databasePath)', MAIN)

    def test_attached_clients_do_not_share_server_database_writer(self):
        self.assertIn("spec.args.includes('--attach')", MAIN)
        self.assertIn('client-${randomUUID()}', MAIN)
        self.assertIn('openCodeDatabaseEnv(baseEnv, clientRuntime.databasePath)', MAIN)

    def test_planner_attempts_have_independent_database(self):
        self.assertIn('`.dependency-flow-opencode-planner-${attempt}.db`', MAIN)
        self.assertIn("env: { OPENCODE_DB: plannerDbPath }", MAIN)

    def test_database_lock_is_retryable_infrastructure_on_server_start(self):
        self.assertIn('OPENCODE_SERVER_START_ATTEMPTS = 3', MAIN)
        self.assertIn('openCodeDatabaseLocked(lastError)', MAIN)
        self.assertIn('Это INFRA, а не ошибка migration plan', MAIN)
        self.assertIn('database\\s+is\\s+locked', RUNTIME)
        self.assertIn('SQLITE_BUSY', RUNTIME)

    def test_runtime_sqlite_failure_never_enters_dependency_replan(self):
        recovery = (ROOT / 'desktop' / 'electron' / 'flow-recovery.ts').read_text(encoding='utf-8')
        self.assertIn("'OPENCODE_SQLITE_BUSY'", recovery)
        self.assertIn("nestedInfrastructure", recovery)
        self.assertIn("firstClassification.kind === 'infrastructure'", MAIN)
        self.assertIn("failureClassification.kind === 'infrastructure'", MAIN)
        self.assertIn("без Planner и без повторного Z3/Baseline", MAIN)
        self.assertIn("spec.command === 'opencode' && code !== 0 && openCodeDatabaseLocked", MAIN)


if __name__ == '__main__':
    unittest.main()
