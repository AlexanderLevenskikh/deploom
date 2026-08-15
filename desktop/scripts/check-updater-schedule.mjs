import { INITIAL_UPDATE_CHECK_DELAY_MS, UPDATE_CHECK_INTERVAL_MS, scheduleUpdateChecks } from "../dist-electron/updater-schedule.js";

if (INITIAL_UPDATE_CHECK_DELAY_MS !== 10_000) throw new Error(`Initial update check must run after 10s, got ${INITIAL_UPDATE_CHECK_DELAY_MS}`);
if (UPDATE_CHECK_INTERVAL_MS !== 30 * 60 * 1_000) throw new Error(`Background update interval must be 30m, got ${UPDATE_CHECK_INTERVAL_MS}`);

const calls = [];
const cleared = [];
const callbacks = [];
const scheduler = {
  setTimeout(callback, delay) { calls.push(["timeout", delay]); callbacks.push(callback); return { id: "timeout" }; },
  setInterval(callback, delay) { calls.push(["interval", delay]); callbacks.push(callback); return { id: "interval" }; },
  clearTimeout(handle) { cleared.push(handle.id); },
  clearInterval(handle) { cleared.push(handle.id); },
};
let checks = 0;
const stop = scheduleUpdateChecks(() => { checks += 1; }, scheduler);
if (calls[0]?.[1] !== INITIAL_UPDATE_CHECK_DELAY_MS || calls[1]?.[1] !== UPDATE_CHECK_INTERVAL_MS) throw new Error(`Unexpected updater schedule: ${JSON.stringify(calls)}`);
callbacks.forEach((callback) => callback());
if (checks !== 2) throw new Error(`Scheduled callbacks did not check for updates: ${checks}`);
stop();
if (cleared.join(",") !== "timeout,interval") throw new Error(`Updater timers were not cleared: ${cleared}`);
console.log("Background updater schedule OK");
