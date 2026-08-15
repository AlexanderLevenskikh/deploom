import { readFileSync } from "node:fs";
import { flowNotificationContent } from "../dist-electron/notifications.js";

const stage = flowNotificationContent({ kind: "stage-complete", projectName: "checkout-form", action: "audit" });
if (!stage.title.includes("Этап завершён") || !stage.body.includes("Независимый аудит")) throw new Error("Stage notification content is invalid");
const group = flowNotificationContent({ kind: "group-complete", projectName: "checkout-form", branch: "deps-demo-group-2", index: 2, total: 5, packages: ["react", "react-dom", "vite", "vitest"] });
if (!group.body.includes("группа 2 из 5") || !group.body.includes("+1")) throw new Error("Group notification content is invalid");
const autopilot = flowNotificationContent({ kind: "autopilot-complete", projectName: "checkout-form", published: true });
if (!autopilot.body.includes("опубликованы")) throw new Error("Autopilot notification content is invalid");
const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8");
const preload = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
for (const required of ["Notification.isSupported()", "flow:set-notifications-enabled", "group-complete", "stage-complete", "app.setAppUserModelId('io.github.alexanderlevenskikh.deploom')"]) {
  if (!main.includes(required)) throw new Error("Main notification wiring missing: " + required);
}
if (!preload.includes("setNotificationsEnabled") || !preload.includes("notifyAutopilotComplete")) throw new Error("Preload notification API is incomplete");
if (!app.includes('className="notification-toggle"') || !app.includes("flow.setNotificationsEnabled")) throw new Error("Header notification toggle is missing");
if (!main.includes("if (exitCode === 0 && !job.autopilot)")) throw new Error("Autopilot must suppress per-stage completion notifications");
console.log("Windows notification contract OK");