import { buildClaudeAgentArgs, buildClaudeResumeArgs, buildCodexAgentArgs, buildCodexResumeArgs, buildOpenCodeAgentArgs, buildOpenCodeResumeArgs, parseOpencodeModelsOutput } from "../dist-electron/agent-command.js";
import { agentScopeFingerprint, extractAgentSessionId, resumableAgentSessionId } from "../dist-electron/agent-session.js";

const projectPath = "C:\\work dir\\project";
const promptPath = "C:\\team state\\migration prompt.md";
const args = buildOpenCodeAgentArgs(projectPath, promptPath);

if (args[0] !== "run") throw new Error("OpenCode run subcommand must be first");
if (!args.some((arg) => arg.includes("Branch plan исчерпывающий и неизменяемый"))) {
  throw new Error("OpenCode migration message is missing");
}
if (args.includes("--file")) throw new Error("Standalone --file greedily consumes the following message");
if (!args.includes(`--file=${promptPath}`)) throw new Error("Prompt must use an unambiguous --file=<path> argument");
if (args[args.indexOf("--dir") + 1] !== projectPath) throw new Error("OpenCode project directory is malformed");
const liveServer = "http://127.0.0.1:45678";
const attachedStart = buildOpenCodeAgentArgs(projectPath, promptPath, undefined, undefined, liveServer);
if (attachedStart[attachedStart.indexOf("--attach") + 1] !== liveServer) throw new Error("OpenCode start did not attach to the live server");

const openCodeSession = "ses_048bc08c5ffea4zBiD2T0mu5M2";
const openCodeResume = buildOpenCodeResumeArgs(projectPath, openCodeSession);
const attachedResume = buildOpenCodeResumeArgs(projectPath, openCodeSession, undefined, promptPath, undefined, undefined, liveServer);
if (attachedResume[attachedResume.indexOf("--attach") + 1] !== liveServer) throw new Error("OpenCode resume did not attach to the live server");
if (openCodeResume[0] !== "run" || openCodeResume[openCodeResume.indexOf("--session") + 1] !== openCodeSession) throw new Error("OpenCode resume command is malformed");
if (openCodeResume.some((arg) => arg.startsWith("--file"))) throw new Error("Resume must not attach the original prompt again");
const codexSession = "019fa7af-21e2-74e3-8dbd-5d7281bd2378";
const codexResume = buildCodexResumeArgs(codexSession);
if (codexResume.slice(0, 4).join(" ") !== `exec resume --json ${codexSession}`) throw new Error("Codex resume command is malformed");
if (extractAgentSessionId(`{"type":"step_start","sessionID":"${openCodeSession}"}`, "opencode") !== openCodeSession) throw new Error("OpenCode session id was not extracted");
if (extractAgentSessionId(`{"type":"thread.started","thread_id":"${codexSession}"}`, "codex") !== codexSession) throw new Error("Codex session id was not extracted");

const scopeFingerprint = agentScopeFingerprint({ provider: "opencode", project: "Demo", branch: "libs-group-5", scopeHash: "abc", promptVersion: "v2" });
if (resumableAgentSessionId({ provider: "opencode", id: openCodeSession, interrupted: true, updatedAt: "now", scopeFingerprint }, "opencode", scopeFingerprint) !== openCodeSession) throw new Error("Matching interrupted session must resume");
if (resumableAgentSessionId({ provider: "opencode", id: openCodeSession, interrupted: true, updatedAt: "old" }, "opencode", scopeFingerprint)) throw new Error("Legacy session without a scope fingerprint must not resume");
if (resumableAgentSessionId({ provider: "opencode", id: openCodeSession, interrupted: true, updatedAt: "old", scopeFingerprint: "stale" }, "opencode", scopeFingerprint)) throw new Error("Stale migration scope must not reuse a prior session");

const claudeSession = "a1b2c3d4-e5f6-4789-9abc-def012345678";
const claudeStart = buildClaudeAgentArgs();
if (!claudeStart.includes("--print")) throw new Error("Claude initial run must use --print");
if (claudeStart.includes("--resume")) throw new Error("Claude initial run must not pass --resume");
const claudeResume = buildClaudeResumeArgs(claudeSession);
if (claudeResume[claudeResume.indexOf("--resume") + 1] !== claudeSession) throw new Error("Claude resume command is malformed");
if (!claudeResume.includes("--print")) throw new Error("Claude resume must stay in --print mode");
if (extractAgentSessionId(`{"type":"system","subtype":"init","session_id":"${claudeSession}"}`, "claude") !== claudeSession) throw new Error("Claude session id was not extracted");

// The desktop app never silently substitutes a model for the user: no model
// configured means no --model flag at all, and a configured model must reach
// every provider's start and resume invocation, including codex's initial
// stdin run (previously inlined ad hoc in main.ts, not agent-command.ts).
const model = "preview-code-pro";
const codexStart = buildCodexAgentArgs(projectPath, model);
if (codexStart.includes("--model") === false || codexStart[codexStart.indexOf("--model") + 1] !== model) throw new Error("Codex start must pass --model");
if (codexStart[codexStart.length - 1] !== "-") throw new Error("Codex start must keep the stdin sentinel as the final argument");
if (buildCodexAgentArgs(projectPath).includes("--model")) throw new Error("Codex start must omit --model when no model is configured");
const codexResumeWithModel = buildCodexResumeArgs(codexSession, model);
if (codexResumeWithModel[codexResumeWithModel.indexOf("--model") + 1] !== model) throw new Error("Codex resume must pass --model");
if (buildCodexResumeArgs(codexSession).includes("--model")) throw new Error("Codex resume must omit --model when no model is configured");

const freshRetryFeedback = "не достигнуты package targets: jsdom; рабочее дерево чистое";
const openCodeFreshRetry = buildOpenCodeAgentArgs(projectPath, promptPath, undefined, undefined, undefined, freshRetryFeedback);
if (!openCodeFreshRetry.some((arg) => arg.includes("jsdom") && arg.includes("Не повторяй"))) throw new Error("A fresh autonomous retry must carry factual remaining-scope feedback without resuming the old session");

const openCodeStartWithModel = buildOpenCodeAgentArgs(projectPath, promptPath, model);
if (openCodeStartWithModel[openCodeStartWithModel.indexOf("--model") + 1] !== model) throw new Error("OpenCode start must pass --model");
if (args.includes("--model")) throw new Error("OpenCode start must omit --model when no model is configured");
const openCodeResumeWithModel = buildOpenCodeResumeArgs(projectPath, openCodeSession, model);
if (openCodeResumeWithModel[openCodeResumeWithModel.indexOf("--model") + 1] !== model) throw new Error("OpenCode resume must pass --model");
if (openCodeResume.includes("--model")) throw new Error("OpenCode resume must omit --model when no model is configured");

const claudeStartWithModel = buildClaudeAgentArgs(model);
if (claudeStartWithModel[claudeStartWithModel.indexOf("--model") + 1] !== model) throw new Error("Claude start must pass --model");
if (claudeStart.includes("--model")) throw new Error("Claude start must omit --model when no model is configured");
const claudeResumeWithModel = buildClaudeResumeArgs(claudeSession, model);
if (claudeResumeWithModel[claudeResumeWithModel.indexOf("--model") + 1] !== model) throw new Error("Claude resume must pass --model");
if (claudeResume.includes("--model")) throw new Error("Claude resume must omit --model when no model is configured");

// Verified against real `opencode models` output on Windows (CRLF line
// endings, no trailing-newline surprises).
if (parseOpencodeModelsOutput("local_ai/code-pro\r\nlocal_ai/preview-code-pro\r\n").join(",") !== "local_ai/code-pro,local_ai/preview-code-pro") {
  throw new Error("opencode models output was not parsed correctly");
}
if (parseOpencodeModelsOutput("").length !== 0) throw new Error("Empty opencode models output must yield no suggestions");
if (parseOpencodeModelsOutput("\n\n  \n").length !== 0) throw new Error("Blank lines must not become empty-string suggestions");

// Unlike codex (--sandbox workspace-write) and claude (--permission-mode
// acceptEdits), opencode has no default that lets an unattended `run` clear
// its own permission prompts: without --auto every request (including reads
// of files outside --dir, e.g. AGENT_RUNBOOK/roadmap JSON referenced by
// absolute path from the template workspace) is auto-*rejected*, so the
// agent can silently never follow the branch plan it was given.
if (!args.includes("--auto")) throw new Error("OpenCode start must run with --auto so it isn't blind-denied every permission request");
if (!args[1].includes("timeout не менее 10 минут")) throw new Error("OpenCode migration prompt must budget the first dependency install correctly");
if (!openCodeResume.includes("--auto")) throw new Error("OpenCode resume must also run with --auto");

// A resumed session never gets the full prompt reattached (to avoid
// re-billing the manifest on every auto-continue), which is only safe as
// long as the agent's own context still holds the original scope. OpenCode
// can silently compact its own session mid-run and lose that grounding --
// observed in practice inventing extra, never-planned groups afterward.
// Pointing the resume message at the saved prompt file lets the agent
// re-read the real Branch plan instead of improvising one.
const resumeWithPrompt = buildOpenCodeResumeArgs(projectPath, openCodeSession, undefined, promptPath);
if (!resumeWithPrompt.some((arg) => arg.includes(promptPath))) throw new Error("OpenCode resume must point back at the saved prompt file for compaction recovery");
if (!resumeWithPrompt.some((arg) => arg.includes("adoption/quarantine"))) throw new Error("Resume must preserve out-of-plan refs for Supervisor reconciliation instead of asking the user to repair them");
const resumeWithGateFeedback = buildOpenCodeResumeArgs(projectPath, openCodeSession, undefined, promptPath, "HEAD сейчас на libs-group-2. влито в libs-merged: libs-group-1. scope missing: package-a");
if (!resumeWithGateFeedback.some((arg) => arg.includes("package-a"))) throw new Error("Auto-resume must tell the agent which scope target failed");
if (!resumeWithGateFeedback.some((arg) => arg.includes("libs-merged"))) throw new Error("Auto-resume must carry the factual git state, not just the failure");
if (!resumeWithGateFeedback.some((arg) => arg.includes("не повторяй"))) throw new Error("Auto-resume must forbid redoing already merged work");
const resumeWithoutPrompt = buildOpenCodeResumeArgs(projectPath, openCodeSession);
if (resumeWithoutPrompt.some((arg) => arg.includes(".md"))) throw new Error("OpenCode resume must not reference a prompt file when none is known");

// A note typed in the desktop app after an unexpected stop must reach the
// agent on both a fresh start and a resume, for every provider, clearly
// attributed to the user rather than folded silently into the git-state
// feedback -- and it must still not license breaking the immutable plan.
const userNote = "Конфликт в yarn.lock разрешён вручную, продолжай со следующей группы.";
const openCodeStartWithNote = buildOpenCodeAgentArgs(projectPath, promptPath, undefined, userNote);
if (!openCodeStartWithNote.some((arg) => arg.includes(userNote))) throw new Error("OpenCode start must carry the user note");
const openCodeResumeWithNote = buildOpenCodeResumeArgs(projectPath, openCodeSession, undefined, promptPath, undefined, userNote);
if (!openCodeResumeWithNote.some((arg) => arg.includes(userNote))) throw new Error("OpenCode resume must carry the user note");
if (!openCodeResumeWithNote.some((arg) => arg.includes("Сообщение от пользователя"))) throw new Error("User note must be clearly attributed, not conflated with git-state feedback");
if (!openCodeResumeWithNote.some((arg) => arg.includes("не как повод выйти за рамки"))) throw new Error("User note must not override the immutable Branch plan/manifest");
const codexResumeWithNote = buildCodexResumeArgs(codexSession, undefined, promptPath, undefined, userNote);
if (!codexResumeWithNote.some((arg) => arg.includes(userNote))) throw new Error("Codex resume must carry the user note");
const claudeResumeWithNote = buildClaudeResumeArgs(claudeSession, undefined, promptPath, undefined, userNote);
if (!claudeResumeWithNote.some((arg) => arg.includes(userNote))) throw new Error("Claude resume must carry the user note");
if (buildOpenCodeResumeArgs(projectPath, openCodeSession).some((arg) => arg.includes("Сообщение от пользователя"))) {
  throw new Error("No note attribution must appear when no note was given");
}

console.log("Agent start and resume argv OK");
