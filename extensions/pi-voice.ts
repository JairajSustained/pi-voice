// Modified from Hugging Face speech-to-speech; see NOTICE.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { CustomEditor, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { isKeyRelease, isKeyRepeat, isKittyProtocolActive, Key, matchesKey, type TUI } from "@earendil-works/pi-tui";

type VoiceState = "idle" | "recording" | "transcribing" | "stopped";
type VoiceMethod = "ping" | "record.start" | "record.stop" | "cancel" | "shutdown";
type JsonObject = Record<string, unknown>;

type PendingRequest = {
	resolve: (result: JsonObject) => void;
	reject: (error: Error) => void;
	timer: NodeJS.Timeout;
};

type SpaceHold = {
	timer: NodeJS.Timeout;
	activated: boolean;
	released: boolean;
	context: ExtensionContext;
};

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON_SOURCE = join(PACKAGE_ROOT, "src");
const MAX_PROTOCOL_LINE_BYTES = 64 * 1024;
const MAX_ERROR_CHARS = 512;
const DEFAULT_TIMEOUT_MS = 10_000;
const LONG_OPERATION_TIMEOUT_MS = 10 * 60_000;
const SPACE_HOLD_THRESHOLD_MS = 250;
const RECORDING_CURSOR_STYLE = "\u001b[48;2;255;95;87m\u001b[38;2;20;20;20m";
const TRANSCRIBING_CURSOR_STYLE = "\u001b[48;2;255;189;46m\u001b[38;2;20;20;20m";
const ANSI_RESET = "\u001b[0m";
const SOFTWARE_CURSOR_PATTERN = /\u001b\[7m([^\u001b]*)\u001b\[(?:0|27)m/;

class SidecarError extends Error {
	constructor(
		readonly code: string,
		message: string,
	) {
		super(message);
		this.name = "SidecarError";
	}
}

class SidecarClient {
	state: VoiceState = "stopped";
	private child: ChildProcessWithoutNullStreams | undefined;
	private outputBuffer = Buffer.alloc(0);
	private pending = new Map<string, PendingRequest>();
	private nextRequestId = 1;

	constructor(private readonly onStateChange: (state: VoiceState) => void) {}

	async request(method: VoiceMethod, params: JsonObject = {}): Promise<JsonObject> {
		this.ensureStarted();
		const child = this.child;
		if (!child || child.stdin.destroyed) {
			throw new SidecarError("SIDECAR_UNAVAILABLE", "Pi voice sidecar is unavailable");
		}

		const id = String(this.nextRequestId++);
		const timeoutMs = method === "record.stop" ? LONG_OPERATION_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
		return await new Promise<JsonObject>((resolve, reject) => {
			const timer = setTimeout(() => {
				const error = new SidecarError("TIMEOUT", `Voice operation ${method} timed out; restart required`);
				const timedOut = this.takePending(id);
				timedOut?.reject(error);
				this.failProcess(error);
			}, timeoutMs);
			this.pending.set(id, { resolve, reject, timer });
			child.stdin.write(`${JSON.stringify({ id, method, params })}\n`, (error) => {
				if (!error) return;
				const writeError = new SidecarError("SIDECAR_WRITE_FAILED", error.message);
				const pending = this.takePending(id);
				pending?.reject(writeError);
				this.failProcess(writeError);
			});
		});
	}

	async shutdown(): Promise<void> {
		const child = this.child;
		if (!child) return;
		try {
			await this.request("shutdown");
		} catch {
			child.kill("SIGTERM");
		}
		this.disposeProcess();
	}

	private ensureStarted(): void {
		if (this.child && !this.child.killed) return;
		const launch = resolveSidecarLaunch();
		const child = spawn(launch.command, launch.args, {
			cwd: PACKAGE_ROOT,
			env: {
				...process.env,
				PYTHONPATH: [PYTHON_SOURCE, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
				PYTHONUNBUFFERED: "1",
			},
			stdio: ["pipe", "pipe", "pipe"],
		});
		this.child = child;
		this.state = "idle";
		this.onStateChange("idle");
		child.stdout.on("data", (chunk: Buffer) => this.handleOutputChunk(chunk));
		child.stderr.resume();
		child.once("error", (error) => this.failProcess(new SidecarError("SIDECAR_START_FAILED", error.message)));
		child.once("exit", (code, signal) => {
			if (this.child !== child) return;
			const reason = `Pi voice sidecar exited (${signal ?? code ?? "unknown"}). Run it directly for diagnostics.`;
			this.failProcess(new SidecarError("SIDECAR_EXITED", reason));
		});
	}

	private handleOutputChunk(chunk: Buffer): void {
		let offset = 0;
		while (offset < chunk.length) {
			const newline = chunk.indexOf(0x0a, offset);
			const end = newline === -1 ? chunk.length : newline;
			const fragment = chunk.subarray(offset, end);
			if (this.outputBuffer.length + fragment.length > MAX_PROTOCOL_LINE_BYTES) {
				this.failProcess(new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned an oversized protocol line"));
				return;
			}
			this.outputBuffer = Buffer.concat([this.outputBuffer, fragment]);
			if (newline === -1) return;

			const line = this.outputBuffer.toString("utf8").replace(/\r$/, "");
			this.outputBuffer = Buffer.alloc(0);
			offset = newline + 1;
			if (line) this.handleLine(line);
			if (!this.child) return;
		}
	}

	private handleLine(line: string): void {
		let message: JsonObject;
		try {
			const parsed: unknown = JSON.parse(line);
			if (!isJsonObject(parsed)) throw new Error("not an object");
			message = parsed;
		} catch {
			this.failProcess(new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned malformed JSON"));
			return;
		}

		if (message.event !== undefined) {
			const data = message.data;
			if (message.event !== "status" || !isJsonObject(data) || !isVoiceState(data.state)) {
				this.failProcess(new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned an invalid notification"));
				return;
			}
			this.state = data.state;
			this.onStateChange(data.state);
			return;
		}

		const id = message.id;
		if (typeof id !== "string") {
			this.failProcess(new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned an uncorrelated response"));
			return;
		}
		const pending = this.takePending(id);
		if (!pending) {
			this.failProcess(new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned a response for an unknown request"));
			return;
		}
		if (message.ok === true && isJsonObject(message.result)) {
			pending.resolve(message.result);
			return;
		}
		if (message.ok === false && isJsonObject(message.error)) {
			const code = typeof message.error.code === "string" ? message.error.code : "SIDECAR_ERROR";
			const text = typeof message.error.message === "string" ? message.error.message : "Voice operation failed";
			pending.reject(new SidecarError(code, text.slice(0, MAX_ERROR_CHARS)));
			return;
		}
		const error = new SidecarError("INVALID_SIDECAR_OUTPUT", "Sidecar returned an invalid response");
		pending.reject(error);
		this.failProcess(error);
	}

	private takePending(id: string): PendingRequest | undefined {
		const pending = this.pending.get(id);
		if (!pending) return undefined;
		clearTimeout(pending.timer);
		this.pending.delete(id);
		return pending;
	}

	private failProcess(error: SidecarError): void {
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(error);
		}
		this.pending.clear();
		this.child?.kill("SIGTERM");
		this.disposeProcess();
		this.state = "stopped";
		this.onStateChange("stopped");
	}

	private disposeProcess(): void {
		this.outputBuffer = Buffer.alloc(0);
		this.child = undefined;
	}
}

export function resolveSidecarLaunch(): { command: string; args: string[] } {
	const configuredPython = process.env.PI_VOICE_PYTHON?.trim();
	if (configuredPython) return { command: configuredPython, args: ["-m", "pi_voice.sidecar"] };

	const virtualenvPython = join(PACKAGE_ROOT, ".venv", "bin", "python");
	if (existsSync(virtualenvPython)) return { command: virtualenvPython, args: ["-m", "pi_voice.sidecar"] };

	return {
		command: process.env.PI_VOICE_UV?.trim() || "uv",
		args: ["run", "--project", PACKAGE_ROOT, "python", "-m", "pi_voice.sidecar"],
	};
}

function isJsonObject(value: unknown): value is JsonObject {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isVoiceState(value: unknown): value is VoiceState {
	return ["idle", "recording", "transcribing", "stopped"].includes(String(value));
}

function stateLabel(enabled: boolean, state: VoiceState): string {
	if (!enabled) return "voice off";
	return `voice ${state}`;
}

export function appendTranscript(editorText: string, transcript: string): string {
	if (!editorText) return transcript;
	return `${editorText}${/\s$/.test(editorText) ? "" : " "}${transcript}`;
}

export function colorizeSoftwareCursor(lines: string[], state: VoiceState): string[] {
	const style =
		state === "recording" ? RECORDING_CURSOR_STYLE : state === "transcribing" ? TRANSCRIBING_CURSOR_STYLE : undefined;
	if (!style) return lines;
	return lines.map((line) => line.replace(SOFTWARE_CURSOR_PATTERN, `${style}$1${ANSI_RESET}`));
}

export default function piVoiceExtension(pi: ExtensionAPI): void {
	let client: SidecarClient | undefined;
	let enabled = false;
	let recordingEditorText: string | undefined;
	let spaceHold: SpaceHold | undefined;
	let unsubscribeTerminalInput: (() => void) | undefined;
	let activeContext: ExtensionContext | undefined;
	let activeTui: TUI | undefined;
	let voiceState: VoiceState = "stopped";

	function updateStatus(): void {
		if (!activeContext?.hasUI) return;
		const state = client?.state ?? "stopped";
		voiceState = state;
		activeContext.ui.setStatus("pi-voice", activeContext.ui.theme.fg(enabled ? "accent" : "dim", stateLabel(enabled, state)));
		activeTui?.requestRender();
	}

	function getClient(ctx: ExtensionContext): SidecarClient {
		activeContext = ctx;
		client ??= new SidecarClient(() => updateStatus());
		return client;
	}

	function notifyError(ctx: ExtensionContext, error: unknown): void {
		const message = error instanceof Error ? error.message : String(error);
		ctx.ui.notify(message.slice(0, MAX_ERROR_CHARS), "error");
	}

	async function enableVoice(ctx: ExtensionContext): Promise<void> {
		if (!ctx.hasUI) {
			ctx.ui.notify("Pi voice requires interactive or RPC UI mode", "warning");
			return;
		}
		enabled = true;
		try {
			await getClient(ctx).request("ping");
			updateStatus();
			ctx.ui.notify(
				isKittyProtocolActive()
					? "Pi voice ready. Hold Space to talk; F8 remains the toggle fallback."
					: "Pi voice ready. Key-release reporting is unavailable, so use F8 to toggle recording.",
				"info",
			);
		} catch (error) {
			enabled = false;
			updateStatus();
			notifyError(ctx, error);
		}
	}

	async function startRecording(ctx: ExtensionContext): Promise<void> {
		if (!enabled) await enableVoice(ctx);
		if (!enabled) return;
		if (!ctx.isIdle()) {
			ctx.ui.notify("Wait for Pi to finish before recording another request.", "warning");
			return;
		}
		recordingEditorText = ctx.ui.getEditorText();
		try {
			await getClient(ctx).request("record.start");
			updateStatus();
			ctx.ui.notify("Recording… release Space to stop, or use F8 as the toggle fallback.", "info");
		} catch (error) {
			recordingEditorText = undefined;
			notifyError(ctx, error);
		}
	}

	function stopRecording(ctx: ExtensionContext): void {
		const sidecar = getClient(ctx);
		const expectedEditorText = recordingEditorText ?? ctx.ui.getEditorText();
		recordingEditorText = undefined;
		void sidecar
			.request("record.stop")
			.then((result) => {
				const transcript = typeof result.transcript === "string" ? result.transcript.trim() : "";
				if (!transcript) throw new SidecarError("NO_SPEECH", "No speech was detected");
				if (ctx.ui.getEditorText() !== expectedEditorText) {
					throw new SidecarError("EDITOR_CHANGED", "The editor changed while transcribing; transcript was not inserted");
				}
				ctx.ui.setEditorText(appendTranscript(expectedEditorText, transcript));
				ctx.ui.notify("Transcript is ready. Review it, then press Enter to send.", "info");
			})
			.catch((error) => notifyError(ctx, error))
			.finally(updateStatus);
	}

	async function cancelVoice(ctx: ExtensionContext): Promise<void> {
		clearSpaceHold();
		try {
			if (client) await client.request("cancel");
		} catch (error) {
			notifyError(ctx, error);
		} finally {
			recordingEditorText = undefined;
			updateStatus();
		}
	}

	async function disableVoice(ctx: ExtensionContext): Promise<void> {
		clearSpaceHold();
		enabled = false;
		recordingEditorText = undefined;
		await client?.shutdown();
		client = undefined;
		updateStatus();
		ctx.ui.notify("Pi voice disabled.", "info");
	}

	function clearSpaceHold(): void {
		if (!spaceHold) return;
		clearTimeout(spaceHold.timer);
		spaceHold = undefined;
	}

	function handleTerminalInput(data: string, ctx: ExtensionContext): { consume?: boolean } | undefined {
		if (!isKittyProtocolActive() || !matchesKey(data, Key.space)) return;

		if (isKeyRelease(data)) {
			const hold = spaceHold;
			if (!hold) return { consume: true };
			hold.released = true;
			clearTimeout(hold.timer);
			if (!hold.activated) {
				spaceHold = undefined;
				ctx.ui.pasteToEditor(" ");
			} else if (client?.state === "recording") {
				spaceHold = undefined;
				stopRecording(hold.context);
			}
			return { consume: true };
		}

		if (isKeyRepeat(data)) return spaceHold ? { consume: true } : undefined;
		if (spaceHold) return { consume: true };
		if (!ctx.isIdle()) return;
		if (client && client.state !== "idle" && client.state !== "stopped") return;

		const hold: SpaceHold = {
			activated: false,
			released: false,
			context: ctx,
			timer: setTimeout(() => {
				hold.activated = true;
				void startRecording(ctx).then(() => {
					if (spaceHold !== hold) return;
					if (hold.released && client?.state === "recording") {
						spaceHold = undefined;
						stopRecording(ctx);
					} else if (client?.state !== "recording") {
						spaceHold = undefined;
					}
				});
			}, SPACE_HOLD_THRESHOLD_MS),
		};
		spaceHold = hold;
		return { consume: true };
	}

	async function toggleRecording(ctx: ExtensionContext): Promise<void> {
		activeContext = ctx;
		clearSpaceHold();
		if (client?.state === "recording") {
			stopRecording(ctx);
			return;
		}
		if (client && client.state !== "idle" && client.state !== "stopped") {
			ctx.ui.notify(`Voice is currently ${client.state}. Use /voice cancel if needed.`, "warning");
			return;
		}
		await startRecording(ctx);
	}

	pi.registerShortcut(Key.f8, {
		description: "Toggle Pi voice recording",
		handler: toggleRecording,
	});

	pi.registerCommand("voice", {
		description: "Control local Pi voice (on, off, start, stop, cancel, status, ping)",
		handler: async (args, ctx) => {
			activeContext = ctx;
			const action = args.trim().toLowerCase() || "on";
			switch (action) {
				case "on":
					await enableVoice(ctx);
					break;
				case "off":
					await disableVoice(ctx);
					break;
				case "start":
					await startRecording(ctx);
					break;
				case "stop":
					if (client?.state !== "recording") {
						ctx.ui.notify("Voice is not recording.", "warning");
						break;
					}
					stopRecording(ctx);
					break;
				case "cancel":
					await cancelVoice(ctx);
					break;
				case "ping":
					try {
						const result = await getClient(ctx).request("ping");
						ctx.ui.notify(`Pi voice sidecar: ${String(result.state ?? "unknown")}`, "info");
					} catch (error) {
						notifyError(ctx, error);
					}
					break;
				case "status":
					ctx.ui.notify(stateLabel(enabled, client?.state ?? "stopped"), "info");
					break;
				default:
					ctx.ui.notify("Usage: /voice [on|off|start|stop|cancel|status|ping]", "warning");
			}
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		activeContext = ctx;
		recordingEditorText = undefined;
		clearSpaceHold();
		unsubscribeTerminalInput?.();
		unsubscribeTerminalInput = ctx.ui.onTerminalInput((data) => handleTerminalInput(data, ctx));

		const existingEditorFactory = ctx.ui.getEditorComponent();
		ctx.ui.setEditorComponent((tui, theme, keybindings) => {
			activeTui = tui;
			const editor = existingEditorFactory
				? existingEditorFactory(tui, theme, keybindings)
				: new CustomEditor(tui, theme, keybindings);
			const renderEditor = editor.render.bind(editor);
			editor.render = (width) => colorizeSoftwareCursor(renderEditor(width), voiceState);
			return editor;
		});
		updateStatus();
	});

	pi.on("session_shutdown", async () => {
		enabled = false;
		recordingEditorText = undefined;
		clearSpaceHold();
		unsubscribeTerminalInput?.();
		unsubscribeTerminalInput = undefined;
		await client?.shutdown();
		client = undefined;
		voiceState = "stopped";
		activeTui?.requestRender();
		activeTui = undefined;
		activeContext = undefined;
	});
}
