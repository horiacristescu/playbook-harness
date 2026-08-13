import type {
  ExtensionAPI,
  ToolCallEvent,
  ToolResultEvent,
} from "@earendil-works/pi-coding-agent";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { spawn, spawnSync } from "node:child_process";

type HookResult = {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: string;
};

type PlaybookMetadata = {
  managed_by?: string;
  schema?: number;
  provider?: string;
  hook_dir?: string;
  runtime_schema?: number;
  central_commit?: string;
  _playbook_harness?: {
    managed_by?: string;
    schema?: number;
  };
};

const REQUIRED_HOOK_FILES = [
  "task-gate-hook",
  "state-echo-hook",
  "chat-log-hook",
  "session-start-hook",
  "session-end-hook",
  "gate-echo-lib.sh",
];
const MUTATING_TOOLS = new Set(["edit", "write"]);
const resolvedHookDirs = new Map<string, string>();
const ompRuntimeChecks = new Map<string, string | undefined>();
const EMBEDDED_PROVIDER: "omp" | undefined = undefined;

const TOOL_NAME_MAP: Record<string, string> = {
  bash: "Bash",
  read: "Read",
  edit: "Edit",
  write: "Write",
  grep: "Grep",
  glob: "Glob",
  find: "Glob",
  ls: "LS",
};

// Mirror gate-echo-lib.sh find_project_root: walk up looking for either the
// legacy `.agent/tasks/` marker OR a multi-user `.agent/<user>/tasks/` marker.
// CLAUDE.md/MIND_MAP.md alone are deliberately NOT sufficient.
function hasPlaybookMarker(dir: string): boolean {
  if (existsSync(join(dir, ".agent", "tasks"))) return true;
  const agentDir = join(dir, ".agent");
  if (!existsSync(agentDir)) return false;
  try {
    for (const entry of readdirSync(agentDir, { withFileTypes: true })) {
      if (entry.isDirectory() && existsSync(join(agentDir, entry.name, "tasks"))) {
        return true;
      }
    }
  } catch {
    // unreadable .agent — treat as no marker
  }
  return false;
}

function findProjectRoot(start: string): string | undefined {
  let dir = start;
  while (dir !== dirname(dir)) {
    if (hasPlaybookMarker(dir)) return dir;
    dir = dirname(dir);
  }
  return undefined;
}

type SessionContext = {
  sessionManager?: { getSessionId?: () => unknown };
};

function sessionId(ctx: SessionContext): string | undefined {
  const value = ctx.sessionManager?.getSessionId?.();
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/.test(value)) {
    return undefined;
  }
  return value;
}

function bindSessionIdentity(projectRoot: string, ctx: SessionContext): string | undefined {
  const value = sessionId(ctx);
  const provider = providerName(projectRoot);
  if (!value || !provider) return undefined;
  // Pi/OMP's Bash tools are spawned after callbacks. Refreshing process.env on
  // every callback makes their ctx-native session ID the exact command
  // transport and overwrites any stale parent/wrapper value.
  delete process.env.CLAUDE_CODE_SESSION_ID;
  delete process.env.CODEX_THREAD_ID;
  delete process.env.ANTIGRAVITY_CONVERSATION_ID;
  process.env.PLAYBOOK_SESSION_ID = value;
  process.env.PLAYBOOK_PROVIDER = provider;
  process.env.PLAYBOOK_BRIDGE_PROVIDER = provider;
  return value;
}

function bindBashCommand(event: ToolCallEvent, nativeSessionId: string, provider: string): void {
  if (event.toolName !== "bash" || typeof event.input.command !== "string") return;
  event.input.command =
    "unset CLAUDE_CODE_SESSION_ID CODEX_THREAD_ID ANTIGRAVITY_CONVERSATION_ID; "
    + `export PLAYBOOK_SESSION_ID='${nativeSessionId}'; `
    + `export PLAYBOOK_PROVIDER='${provider}'; `
    + `export PLAYBOOK_BRIDGE_PROVIDER='${provider}'; `
    + event.input.command;
}

function readMetadata(projectRoot: string): PlaybookMetadata | undefined {
  try {
    const value = JSON.parse(readFileSync(join(projectRoot, ".omp", "playbook.json"), "utf8"));
    if (!value || typeof value !== "object") return undefined;
    const legacy = value.managed_by === "claude-playbook"
      && value.schema === 1
      && value.provider === "omp";
    const directStandalone = value.managed_by === "playbook-harness"
      && value.schema === 1
      && value.provider === "omp";
    const standalone = value._playbook_harness?.managed_by === "playbook-harness"
      && value._playbook_harness?.schema === 2
      && value.provider === "omp"
      && value.runtime_schema === 1
      && typeof value.central_commit === "string"
      && /^[0-9a-f]{40}$/.test(value.central_commit);
    if (!legacy && !directStandalone && !standalone) {
      return undefined;
    }
    return value;
  } catch {
    return undefined;
  }
}

function completeHookDir(path: unknown): path is string {
  return typeof path === "string" && path.length > 0
    && REQUIRED_HOOK_FILES.every((name) => existsSync(join(path, name)));
}

export function resolveHookDir(projectRoot: string): string | undefined {
  const cached = resolvedHookDirs.get(projectRoot);
  if (completeHookDir(cached)) return cached;

  const candidates = [
    process.env.PLAYBOOK_HOOK_DIR,
    join(projectRoot, "scripts"),
    readMetadata(projectRoot)?.hook_dir,
  ];
  const resolved = candidates.find(completeHookDir);
  if (resolved) resolvedHookDirs.set(projectRoot, resolved);
  return resolved;
}

export function providerName(projectRoot: string): string | undefined {
  if (EMBEDDED_PROVIDER) return EMBEDDED_PROVIDER;
  const explicitProvider = process.env.PLAYBOOK_PROVIDER;
  if (explicitProvider === "pi" || explicitProvider === "omp") return explicitProvider;
  const metadataProvider = readMetadata(projectRoot)?.provider;
  return metadataProvider === "omp" ? metadataProvider : undefined;
}

function hookTimeoutMs(): number {
  const parsed = Number(process.env.PLAYBOOK_HOOK_TIMEOUT_MS || "5000");
  return Number.isFinite(parsed) ? Math.min(30_000, Math.max(100, parsed)) : 5_000;
}

function diagnostic(message: string): void {
  process.stderr.write(`[Playbook] ${message}\n`);
}

function repairMessage(projectRoot: string): string {
  return providerName(projectRoot) === "omp"
    ? "Run `pb-tasks init --provider omp <project>` to repair."
    : "Run `pb-tasks init` and relaunch through `pb-pi`.";
}

function ompRuntimeFailure(projectRoot: string): string | undefined {
  if (ompRuntimeChecks.has(projectRoot)) return ompRuntimeChecks.get(projectRoot);
  const metadata = readMetadata(projectRoot);
  if (!metadata) {
    const failure = "Playbook OMP metadata missing or incompatible";
    ompRuntimeChecks.set(projectRoot, failure);
    return failure;
  }
  const result = spawnSync("pb-tasks", ["runtime-info"], {
    cwd: projectRoot,
    env: process.env,
    encoding: "utf8",
    timeout: hookTimeoutMs(),
  });
  let info: any;
  try {
    info = result.status === 0 ? JSON.parse(result.stdout || "") : undefined;
  } catch {
    info = undefined;
  }
  if (!info || !Number.isInteger(info.runtime_schema) || typeof info.commit !== "string") {
    const failure = "Playbook runtime compatibility handshake failed";
    ompRuntimeChecks.set(projectRoot, failure);
    return failure;
  }
  if (info.runtime_schema < (metadata.runtime_schema || 0)) {
    const failure = `Playbook runtime schema ${info.runtime_schema} is older than required ${metadata.runtime_schema}`;
    ompRuntimeChecks.set(projectRoot, failure);
    return failure;
  }
  if (metadata.central_commit && info.commit !== metadata.central_commit) {
    diagnostic(`runtime commit changed (${metadata.central_commit.slice(0, 12)} -> ${info.commit.slice(0, 12)}) with compatible schema; run pb-tasks init --provider omp to refresh`);
  }
  ompRuntimeChecks.set(projectRoot, undefined);
  return undefined;
}

function absPath(path: unknown, cwd: string): unknown {
  if (typeof path !== "string" || path.length === 0) return path;
  return isAbsolute(path) ? path : join(cwd, path);
}

function normalizeToolInput(event: ToolCallEvent | ToolResultEvent, cwd: string): Record<string, unknown> {
  const input = { ...(event.type === "tool_call" ? event.input : event.input) };
  switch (event.toolName) {
    case "bash":
      return input;
    case "write":
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
      };
    case "edit": {
      const edits = Array.isArray(input.edits) ? input.edits : [];
      const oldTexts = edits
        .map((edit: any) => edit?.oldText)
        .filter((value: unknown): value is string => typeof value === "string");
      const newTexts = edits
        .map((edit: any) => edit?.newText)
        .filter((value: unknown): value is string => typeof value === "string");
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
        old_string: input.old_string ?? oldTexts.join("\n"),
        new_string: input.new_string ?? newTexts.join("\n"),
      };
    }
    case "read":
    case "grep":
    case "find":
    case "ls":
      return {
        ...input,
        file_path: absPath(input.path ?? input.file_path, cwd),
      };
    default:
      return input;
  }
}

function hookPayload(
  hookEventName: string,
  nativeSessionId: string,
  cwd: string,
  extra: Record<string, unknown>,
): Record<string, unknown> {
  return {
    hook_event_name: hookEventName,
    session_id: nativeSessionId,
    cwd,
    ...extra,
  };
}

function runHook(
  projectRoot: string,
  scriptName: string,
  payload: Record<string, unknown>,
  nativeSessionId: string,
): Promise<HookResult> {
  const provider = providerName(projectRoot);
  if (!provider) {
    return Promise.resolve({ status: null, stdout: "", stderr: "", error: "Playbook provider identity missing or invalid" });
  }
  const embeddedOmp = EMBEDDED_PROVIDER === "omp";
  if (embeddedOmp) {
    const compatibilityFailure = ompRuntimeFailure(projectRoot);
    if (compatibilityFailure) {
      return Promise.resolve({ status: null, stdout: "", stderr: "", error: compatibilityFailure });
    }
  }
  const hookDir = embeddedOmp ? undefined : resolveHookDir(projectRoot);
  if (!embeddedOmp && !hookDir) {
    return Promise.resolve({ status: null, stdout: "", stderr: "", error: "Playbook hook directory not found" });
  }
  const command = embeddedOmp ? "pb-tasks" : join(hookDir!, scriptName);
  const args = embeddedOmp ? ["hook", scriptName] : [];
  const env = {
    ...process.env,
    PLAYBOOK_PROVIDER: provider,
    PLAYBOOK_BRIDGE_PROVIDER: provider,
    PLAYBOOK_SESSION_ID: nativeSessionId,
    PLAYBOOK_PROJECT_ROOT: projectRoot,
    // Pi invokes the central script directly; OMP goes through pb-tasks,
    // which overwrites this with its own trusted checkout root.
    ...(hookDir ? { PLAYBOOK_RUNTIME_ROOT: dirname(hookDir) } : {}),
  };
  const timeoutMs = hookTimeoutMs();
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timedOut = false;
    const detached = process.platform !== "win32";
    const child = spawn(command, args, {
      cwd: projectRoot,
      env,
      detached,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const finish = (status: number | null, error?: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ status, stdout, stderr, error });
    };
    const timer = setTimeout(() => {
      timedOut = true;
      const timeoutError = `${scriptName} timed out after ${timeoutMs}ms`;
      try {
        if (detached && child.pid) process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
      // The detached process group owns every inherited pipe, so close should
      // follow promptly. Keep a final JS-side bound for spawn/error edge cases.
      setTimeout(() => finish(null, timeoutError), 100).unref();
    }, timeoutMs);
    timer.unref();
    child.stdout?.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr?.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", (error) => finish(null, error.message));
    child.on("close", (status, signal) => {
      const error = timedOut
        ? `${scriptName} timed out after ${timeoutMs}ms`
        : signal ? `hook terminated by ${signal}` : undefined;
      finish(status, error);
    });
    child.stdin?.end(JSON.stringify(payload));
  });
}

function hookFailure(result: HookResult, scriptName: string): string | undefined {
  if (result.status === 0 || result.status === 2) return undefined;
  return result.error || result.stderr.trim() || `${scriptName} exited with status ${result.status}`;
}

function parseHookJson(stdout: string): any | undefined {
  const trimmed = stdout.trim();
  if (!trimmed.startsWith("{")) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

function applyUpdatedInput(event: ToolCallEvent, stdout: string): void {
  const parsed = parseHookJson(stdout);
  const updated = parsed?.hookSpecificOutput?.updatedInput;
  if (!updated || typeof updated !== "object") return;
  Object.assign(event.input, updated);
}

function additionalContext(stdout: string): string | undefined {
  const parsed = parseHookJson(stdout);
  const value = parsed?.hookSpecificOutput?.additionalContext;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    const nativeId = bindSessionIdentity(projectRoot, ctx);
    if (!nativeId) return diagnostic("native session ID unavailable; session hooks disabled");
    const result = await runHook(projectRoot, "session-start-hook", hookPayload("SessionStart", nativeId, ctx.cwd, {}), nativeId);
    const failure = hookFailure(result, "session-start-hook");
    if (failure) diagnostic(`${failure}. ${repairMessage(projectRoot)}`);
    else if (providerName(projectRoot) === "omp") diagnostic("active (omp; project-root discovery)");
  });

  pi.on("input", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return { action: "continue" };
    const nativeId = bindSessionIdentity(projectRoot, ctx);
    if (!nativeId) {
      diagnostic("native session ID unavailable; prompt not logged");
      return { action: "continue" };
    }
    const result = await runHook(
      projectRoot,
      "chat-log-hook",
      hookPayload("UserPromptSubmit", nativeId, ctx.cwd, { prompt: event.text }),
      nativeId,
    );
    const failure = hookFailure(result, "chat-log-hook");
    if (failure) diagnostic(failure);
    return { action: "continue" };
  });

  pi.on("tool_call", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    const nativeId = bindSessionIdentity(projectRoot, ctx);
    if (!nativeId) {
      if (MUTATING_TOOLS.has(event.toolName)) {
        return { block: true, reason: "Playbook native session identity unavailable" };
      }
      diagnostic("native session ID unavailable; allowing nonmutating tool");
      return;
    }
    bindBashCommand(event, nativeId, providerName(projectRoot)!);

    const result = await runHook(
      projectRoot,
      "task-gate-hook",
      hookPayload("PreToolUse", nativeId, ctx.cwd, {
        tool_name: TOOL_NAME_MAP[event.toolName] || event.toolName,
        tool_input: normalizeToolInput(event, ctx.cwd),
      }),
      nativeId,
    );

    applyUpdatedInput(event, result.stdout);

    if (result.status === 2) {
      return {
        block: true,
        reason: result.stderr.trim() || "Blocked by Playbook task-gate-hook",
      };
    }
    const failure = hookFailure(result, "task-gate-hook");
    if (failure) {
      if (MUTATING_TOOLS.has(event.toolName)) {
        return {
          block: true,
          reason: `Playbook enforcement unavailable: ${failure}. ${repairMessage(projectRoot)}`,
        };
      }
      diagnostic(`${failure}; allowing nonmutating or unclassified tool ${event.toolName}`);
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    const nativeId = bindSessionIdentity(projectRoot, ctx);
    if (!nativeId) return diagnostic("native session ID unavailable; gate echo skipped");

    const result = await runHook(
      projectRoot,
      "state-echo-hook",
      hookPayload("PostToolUse", nativeId, ctx.cwd, {
        tool_name: TOOL_NAME_MAP[event.toolName] || event.toolName,
        tool_input: normalizeToolInput(event, ctx.cwd),
        tool_result: {
          is_error: event.isError,
          content: event.content,
        },
      }),
      nativeId,
    );

    const context = additionalContext(result.stdout);
    const failure = hookFailure(result, "state-echo-hook");
    if (failure) diagnostic(failure);
    if (!context) return;
    return {
      content: [
        ...event.content,
        { type: "text" as const, text: `\n\n[Playbook]\n${context}` },
      ],
    };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const projectRoot = findProjectRoot(ctx.cwd);
    if (!projectRoot) return;
    const nativeId = bindSessionIdentity(projectRoot, ctx);
    if (!nativeId) return diagnostic("native session ID unavailable; no cleanup performed");
    const result = await runHook(projectRoot, "session-end-hook", hookPayload("SessionEnd", nativeId, ctx.cwd, {}), nativeId);
    const failure = hookFailure(result, "session-end-hook");
    if (failure) diagnostic(failure);
  });
}
