import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const desktop = join(dirname(fileURLToPath(import.meta.url)), "..");
const root = join(desktop, "..");
const builder = readFileSync(join(desktop, "electron-builder.yml"), "utf8");
const release = readFileSync(join(root, ".github", "workflows", "release.yml"), "utf8");
const desktopReadme = readFileSync(join(desktop, "README.md"), "utf8");

for (const expected of [
  "provider: github",
  "owner: AlexanderLevenskikh",
  "repo: deploom",
  "releaseType: release",
]) {
  if (!builder.includes(expected)) throw new Error(`electron-builder updater target is missing: ${expected}`);
}
if (builder.includes("repo: dependency-roadmap-tool")) throw new Error("legacy GitHub updater repository is still configured");
if (!release.includes("AlexanderLevenskikh/deploom")) throw new Error("release workflow does not pin the expected public repository");
for (const required of ["release/latest.yml", "app-update.yml", ".exe.blockmap", "gh release create"]) {
  if (!release.includes(required)) throw new Error(`release workflow is missing updater asset contract: ${required}`);
}
if (!desktopReadme.includes("AlexanderLevenskikh/deploom")) throw new Error("Desktop update documentation does not name the public update repository");
console.log("Public GitHub update target OK: AlexanderLevenskikh/deploom");
