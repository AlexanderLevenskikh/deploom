import { rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = path.resolve(desktopDir, "..");
const vendorDir = path.join(desktopDir, "vendor");
const python = process.platform === "win32" ? "python" : "python3";

rmSync(vendorDir, { recursive: true, force: true });

function runPip(args, label) {
  const result = spawnSync(python, ["-m", "pip", "install", "--disable-pip-version-check", "--upgrade", ...args], {
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if ((result.status ?? 1) !== 0) {
    throw new Error(`${label} failed with exit code ${result.status ?? 1}`);
  }
}

// Pure-Python dependencies remain portable and can be shared by every packaged
// target. This is intentionally the old universal vendor pass.
runPip([
  "--only-binary=:all:",
  "--platform", "any",
  "--implementation", "py",
  "--abi", "none",
  "-r", path.join(repoDir, "requirements.txt"),
  "--target", vendorDir,
], "portable tool dependency installation");

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
