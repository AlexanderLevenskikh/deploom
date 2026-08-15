import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const github = readFileSync(join(root, ".github", "workflows", "release.yml"), "utf8");

for (const needle of [
  "$maxAttempts = 3",
  "socket hang up|ECONNRESET|ETIMEDOUT|EAI_AGAIN",
  'package-win-attempt-$attempt.log',
  "Windows packaging failed with a non-transient error",
  "ELECTRON_BUILDER_CACHE:",
  "desktop/package-win-attempt-*.log",
]) {
  if (!github.includes(needle)) throw new Error(`Missing GitHub Windows packaging resilience contract: ${needle}`);
}

console.log("Windows packaging resilience contract OK for GitHub release workflow");
