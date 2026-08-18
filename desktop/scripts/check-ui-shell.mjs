import { readFileSync } from "node:fs";

const flow = readFileSync(new URL("../src/components/FlowWorkspace.tsx", import.meta.url), "utf8");
const hook = readFileSync(new URL("../src/hooks/useDependencyFlow.ts", import.meta.url), "utf8");
const appCss = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
const indexCss = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

for (const sentinel of [
  "skipBaselineModeConfirmation",
  "baselineResume !== 'auto'",
  "details.baselineRecovery?.available",
]) {
  if (!flow.includes(sentinel)) throw new Error(`Strict Baseline Continue UI contract missing: ${sentinel}`);
}

if (!/useEffect\(\(\) => \{\s*document\.documentElement\.dataset\.theme = themePreference/.test(hook)) {
  throw new Error("Persisted theme is not applied to the renderer root");
}

for (const sentinel of [
  "BLOCK_VG_UI_LAYOUT_THEME_COMPLETION_V1",
  ".monitoring-body > .run-monitor",
  ".monitoring-ring > div",
  "html[data-theme='dark']",
]) {
  if (!appCss.includes(sentinel)) throw new Error(`Monitoring/theme CSS contract missing: ${sentinel}`);
}

if (!indexCss.includes("BLOCK_VG_DARK_THEME_COMPLETION_V1")) {
  throw new Error("Base design tokens do not contain explicit dark-theme completion");
}

console.log("UI shell / strict Baseline Continue contract OK");
