import { changedOverrideProjects } from "../dist-electron/dashboard-state.js";

const state = (overrides) => JSON.stringify({ schemaVersion: 1, packageOverrides: overrides });

// The real motivation: the dashboard lists every project at once, so a scope
// change can land on a project other than the one selected in the desktop app.
// Recalculating "the selected project" rebuilt the wrong roadmap and left the
// edited one stale.
const edited = changedOverrideProjects(
  state({ Alpha: { "dev:eslint": { excluded: true } }, Beta: { "dev:vite": { excluded: true } } }),
  state({ Alpha: { "dev:eslint": { excluded: true } }, Beta: { "dev:vite": { excluded: true }, "dev:jsdom": { excluded: true } } }),
);
if (edited.join(",") !== "Beta") throw new Error(`Only the edited project must be recalculated: ${JSON.stringify(edited)}`);

// Re-saving identical state must not trigger anything: the dashboard rewrites
// the whole object on every save, so a naive text comparison would report
// every project as changed on every keystroke.
const sameContentDifferentOrder = changedOverrideProjects(
  state({ Alpha: { "dev:a": { excluded: true, exclusionReason: "x" } } }),
  JSON.stringify({ packageOverrides: { Alpha: { "dev:a": { exclusionReason: "x", excluded: true } } }, schemaVersion: 1 }),
);
if (sameContentDifferentOrder.length !== 0) throw new Error(`Key order must not count as a change: ${JSON.stringify(sameContentDifferentOrder)}`);

// A first-ever exclusion (no previous file) and a full removal both count.
if (changedOverrideProjects("", state({ Alpha: { "dev:a": { excluded: true } } })).join(",") !== "Alpha") throw new Error("A first exclusion must be detected");
if (changedOverrideProjects(state({ Alpha: { "dev:a": { excluded: true } } }), state({})).join(",") !== "Alpha") throw new Error("Returning a package to scope must be detected");

// Several projects edited before a single save must all be recalculated.
const multiple = changedOverrideProjects(
  state({ Alpha: {}, Beta: {} }),
  state({ Alpha: { "dev:a": { excluded: true } }, Beta: { "dev:b": { excluded: true } } }),
);
if (multiple.join(",") !== "Alpha,Beta") throw new Error(`Every edited project must be queued: ${JSON.stringify(multiple)}`);

// Unparsable or empty input must never claim a change; the caller falls back
// to the selected project rather than recalculating something arbitrary.
if (changedOverrideProjects("not json", "also not json").length !== 0) throw new Error("Unparsable state must not report changes");
if (changedOverrideProjects("", "").length !== 0) throw new Error("Empty state must not report changes");

console.log("Dashboard state diff OK");
