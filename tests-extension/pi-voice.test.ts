// Modified from Hugging Face speech-to-speech; see NOTICE.
import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { setKittyProtocolActive } from "@earendil-works/pi-tui";
import piVoiceExtension, {
	SidecarClient,
	appendTranscript,
	buildSidecarEnvironment,
	colorizeSoftwareCursor,
	requestTimeoutMs,
	resolveSidecarLaunch,
} from "../extensions/pi-voice.ts";

const PYTHON = join(process.cwd(), ".venv", "bin", "python");

async function createFakeSidecar(source: string): Promise<{ executable: string; cleanup: () => Promise<void> }> {
	const directory = await mkdtemp(join(tmpdir(), "pi-voice-test-"));
	const script = join(directory, "sidecar.py");
	const executable = join(directory, "python");
	await writeFile(script, source);
	await writeFile(executable, `#!/bin/sh\nexec "${PYTHON}" "${script}" "$@"\n`);
	await chmod(executable, 0o700);
	return { executable, cleanup: () => rm(directory, { force: true, recursive: true }) };
}

test("transcript append preserves draft spacing", () => {
	assert.equal(appendTranscript("", "open Safari"), "open Safari");
	assert.equal(appendTranscript("draft", "open Safari"), "draft open Safari");
	assert.equal(appendTranscript("draft ", "open Safari"), "draft open Safari");
});

test("software cursor reflects recording state", () => {
	const cursor = ["before\u001b[7m \u001b[0mafter"];
	assert.match(colorizeSoftwareCursor(cursor, "recording")[0], /48;2;255;95;87/);
	assert.match(colorizeSoftwareCursor(cursor, "transcribing")[0], /48;2;255;189;46/);
	assert.deepEqual(colorizeSoftwareCursor(cursor, "idle"), cursor);
});

test("sidecar launch is locked and production-only", () => {
	const previousPython = process.env.PI_VOICE_PYTHON;
	const previousUv = process.env.PI_VOICE_UV;
	delete process.env.PI_VOICE_PYTHON;
	process.env.PI_VOICE_UV = "/custom/uv";
	try {
		const launch = resolveSidecarLaunch();
		assert.equal(launch.command, "/custom/uv");
		assert.ok(launch.args.includes("--locked"));
		assert.ok(launch.args.includes("--no-dev"));
		assert.deepEqual(launch.args.slice(-3), ["python", "-m", "pi_voice.sidecar"]);
	} finally {
		if (previousPython === undefined) delete process.env.PI_VOICE_PYTHON;
		else process.env.PI_VOICE_PYTHON = previousPython;
		if (previousUv === undefined) delete process.env.PI_VOICE_UV;
		else process.env.PI_VOICE_UV = previousUv;
	}
});

test("sidecar environment removes Python startup injection", () => {
	const environment = buildSidecarEnvironment({
		PATH: "/bin",
		PYTHONHOME: "/untrusted",
		PYTHONINSPECT: "1",
		PYTHONPATH: "/untrusted",
		PYTHONSTARTUP: "/untrusted/startup.py",
	});
	assert.equal(environment.PATH, "/bin");
	assert.equal(environment.PYTHONHOME, undefined);
	assert.equal(environment.PYTHONINSPECT, undefined);
	assert.equal(environment.PYTHONSTARTUP, undefined);
	assert.notEqual(environment.PYTHONPATH, "/untrusted");
	assert.equal(environment.PYTHONNOUSERSITE, "1");
	assert.equal(environment.HF_HUB_DISABLE_IMPLICIT_TOKEN, "1");
});

test("first sidecar request receives the bootstrap timeout", () => {
	assert.equal(requestTimeoutMs("ping", true), 10 * 60_000);
	assert.equal(requestTimeoutMs("ping", false), 10_000);
	assert.equal(requestTimeoutMs("record.stop", false), 10 * 60_000);
});

test("sidecar client correlates requests and shuts down", async () => {
	const fake = await createFakeSidecar(`
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    state = "stopped" if method == "shutdown" else "idle"
    if method == "shutdown":
        print(json.dumps({"event": "status", "data": {"state": state}}), flush=True)
    print(json.dumps({"id": request["id"], "ok": True, "result": {"state": state}}), flush=True)
    if method == "shutdown":
        break
`);
	const previousPython = process.env.PI_VOICE_PYTHON;
	process.env.PI_VOICE_PYTHON = fake.executable;
	try {
		const states: string[] = [];
		const client = new SidecarClient((state) => states.push(state));
		assert.deepEqual(await client.request("ping"), { state: "idle" });
		await client.shutdown();
		assert.ok(states.includes("idle"));
		assert.ok(states.includes("stopped"));
	} finally {
		if (previousPython === undefined) delete process.env.PI_VOICE_PYTHON;
		else process.env.PI_VOICE_PYTHON = previousPython;
		await fake.cleanup();
	}
});

test("voice off disables hold-Space and concurrent starts are serialized", async () => {
	const fake = await createFakeSidecar(`
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(method + "\\n")
    if method == "record.start":
        print(json.dumps({"event": "status", "data": {"state": "recording"}}), flush=True)
        time.sleep(0.2)
        state = "recording"
    elif method == "shutdown":
        print(json.dumps({"event": "status", "data": {"state": "stopped"}}), flush=True)
        state = "stopped"
    else:
        state = "idle"
    print(json.dumps({"id": request["id"], "ok": True, "result": {"state": state}}), flush=True)
    if method == "shutdown":
        break
`);
	const logPath = join(tmpdir(), `pi-voice-methods-${process.pid}-${Date.now()}.log`);
	const previousPython = process.env.PI_VOICE_PYTHON;
	const previousLog = process.env.FAKE_LOG;
	process.env.PI_VOICE_PYTHON = fake.executable;
	process.env.FAKE_LOG = logPath;
	setKittyProtocolActive(true);
	try {
		let terminalInput: ((data: string) => { consume?: boolean } | undefined) | undefined;
		let voiceCommand: ((args: string, context: unknown) => Promise<void>) | undefined;
		const notifications: string[] = [];
		let editorText = "draft";
		const editor = {
			render: () => ["\u001b[7m \u001b[0m"],
			getText: () => editorText,
			setText: (value: string) => {
				editorText = value;
			},
			handleInput: () => undefined,
		};
		const context = {
			cwd: process.cwd(),
			hasUI: true,
			isIdle: () => true,
			ui: {
				notify: (message: string) => notifications.push(message),
				setStatus: () => undefined,
				getEditorText: () => editorText,
				setEditorText: (value: string) => {
					editorText = value;
				},
				pasteToEditor: (value: string) => {
					editorText += value;
				},
				onTerminalInput: (handler: typeof terminalInput) => {
					terminalInput = handler;
					return () => undefined;
				},
				getEditorComponent: () => () => editor,
				setEditorComponent: (factory: (tui: unknown, theme: unknown, keybindings: unknown) => unknown) =>
					factory({ requestRender: () => undefined }, {}, {}),
				theme: { fg: (_color: string, value: string) => value },
			},
		};
		const extensionApi = {
			registerShortcut: () => undefined,
			registerCommand: (_name: string, command: { handler: typeof voiceCommand }) => {
				voiceCommand = command.handler;
			},
			on: (name: string, handler: (event: unknown, context: unknown) => Promise<void>) => {
				if (name === "session_start") void handler({}, context);
			},
		};
		piVoiceExtension(extensionApi as never);
		assert.ok(voiceCommand);
		assert.ok(terminalInput);

		await voiceCommand("on", context);
		await voiceCommand("off", context);
		assert.equal(terminalInput("\u001b[32;1:1u"), undefined);

		const firstStart = voiceCommand("start", context);
		const secondStart = voiceCommand("start", context);
		await Promise.all([firstStart, secondStart]);
		await voiceCommand("off", context);

		const methods = (await readFile(logPath, "utf8")).trim().split("\n");
		assert.equal(methods.filter((method) => method === "record.start").length, 1);
		assert.ok(notifications.includes("Pi voice is already starting."));
	} finally {
		if (previousPython === undefined) delete process.env.PI_VOICE_PYTHON;
		else process.env.PI_VOICE_PYTHON = previousPython;
		if (previousLog === undefined) delete process.env.FAKE_LOG;
		else process.env.FAKE_LOG = previousLog;
		await rm(logPath, { force: true });
		await fake.cleanup();
	}
});

test("sidecar client fails closed on malformed output", async () => {
	const fake = await createFakeSidecar(`
import sys
sys.stdin.readline()
print("not-json", flush=True)
`);
	const previousPython = process.env.PI_VOICE_PYTHON;
	process.env.PI_VOICE_PYTHON = fake.executable;
	try {
		const client = new SidecarClient(() => undefined);
		await assert.rejects(client.request("ping"), /malformed JSON/);
		await client.shutdown();
	} finally {
		if (previousPython === undefined) delete process.env.PI_VOICE_PYTHON;
		else process.env.PI_VOICE_PYTHON = previousPython;
		await fake.cleanup();
	}
});
