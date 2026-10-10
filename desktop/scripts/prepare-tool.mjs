import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = path.resolve(desktopDir, "..");
const vendorDir = path.join(desktopDir, "vendor");
const python = process.platform === "win32" ? "python" : "python3";

rmSync(vendorDir, { recursive: true, force: true });

function runPython(args, label, env = process.env) {
  const result = spawnSync(python, args, { stdio: "inherit", env });
  if (result.error) throw result.error;
  if ((result.status ?? 1) !== 0) {
    throw new Error(`${label} failed with exit code ${result.status ?? 1}`);
  }
}

function runPip(args, label) {
  runPython(["-m", "pip", "install", "--disable-pip-version-check", "--upgrade", ...args], label);
}

// PyYAML publishes native wheels, but its supported pure-Python implementation
// works across target platforms and Python ABIs. Build that wheel explicitly;
// never copy the build host's optional LibYAML extension into a portable bundle.
const yamlRequirements = readFileSync(path.join(repoDir, "requirements.txt"), "utf8")
  .split(/\r?\n/).map(line => line.trim()).filter(line => /^pyyaml\b/i.test(line));
if (yamlRequirements.length !== 1) throw new Error("Expected exactly one PyYAML requirement");
const wheelDir = mkdtempSync(path.join(desktopDir, ".portable-wheels-"));
try {
  runPython([
    "-m", "pip", "wheel", "--disable-pip-version-check", "--no-cache-dir",
    "--only-binary=:all:", "--no-binary=PyYAML",
    yamlRequirements[0], "--no-deps", "--wheel-dir", wheelDir,
  ], "portable wheel preparation", { ...process.env, PYYAML_FORCE_LIBYAML: "0" });
  const yamlWheels = readdirSync(wheelDir).filter(name => /^pyyaml-.*\.whl$/i.test(name));
  if (yamlWheels.length !== 1 || !/-py3-none-any\.whl$/i.test(yamlWheels[0])) {
    throw new Error("PyYAML must produce exactly one universal pure-Python wheel");
  }
  runPip([
    "--find-links", wheelDir, "--only-binary=:all:",
    "--platform", "any", "--implementation", "py", "--abi", "none",
    "-r", path.join(repoDir, "requirements.txt"), "--target", vendorDir,
  ], "portable tool dependency installation");
  // -S prevents installed host packages from hiding a broken vendor directory.
  runPython(["-S", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import yaml; assert not yaml.__with_libyaml__; assert yaml.safe_load('version: 4') == {'version': 4}", vendorDir],
    "portable YAML parser smoke test");
} finally {
  rmSync(wheelDir, { recursive: true, force: true });
}

// z3-solver is *not* a universal wheel: it ships libz3 for a concrete OS/arch.
// Package builds therefore vendor the wheel for the target runtime explicitly.
// TOOL_VENDOR_PLATFORM lets Linux CI prepare a Windows bundle.
const targetPlatform = process.env.TOOL_VENDOR_PLATFORM || (
  process.platform === "win32" ? "win_amd64" :
  process.platform === "darwin" ? (process.arch === "arm64" ? "macosx_11_0_arm64" : "macosx_11_0_x86_64") :
  (process.arch === "arm64" ? "manylinux_2_34_aarch64" : "manylinux2014_x86_64")
);

runPip([
  "--only-binary=:all:",
  "--platform", targetPlatform,
  "--implementation", "py",
  "--abi", "none",
  "-r", path.join(repoDir, "requirements-solver.txt"),
  "--target", vendorDir,
], `solver dependency installation for ${targetPlatform}`);
