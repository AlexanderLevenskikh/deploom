import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const workflow = readFileSync(join(root, ".github", "workflows", "release.yml"), "utf8");
const builder = readFileSync(join(root, "desktop", "electron-builder.yml"), "utf8");

const requiredWorkflow = [
  "gh release create $env:GITHUB_REF_NAME",
  "$installer",
  '"$installer.blockmap"',
  "'release/latest.yml'",
  "GH_TOKEN: ${{ github.token }}",
  "contents: write",
];
for (const needle of requiredWorkflow) {
  if (!workflow.includes(needle)) throw new Error(`Missing public GitHub release asset contract: ${needle}`);
}

for (const needle of [
  "provider: github",
  "owner: AlexanderLevenskikh",
  "repo: deploom",
  "releaseType: release",
]) {
  if (!builder.includes(needle)) throw new Error(`Missing electron-builder GitHub provider contract: ${needle}`);
}

if (/setFeedURL\s*\(/.test(readFileSync(join(root, "desktop", "electron", "main.ts"), "utf8"))) {
  throw new Error("Desktop updater must use packaged app-update.yml instead of a runtime token/feed override");
}

console.log("Public GitHub release assets/update provider contract OK");
