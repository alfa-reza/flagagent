import { describe, it, expect } from "vitest";
import { createServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AgentLoop } from "../src/flagagent/loop.js";
import { Limits } from "../src/flagagent/limits.js";
import { ExactStringVerifier } from "../src/flagagent/tools.js";
import { ChatCompletionsModel } from "../src/flagagent/providers/chat.js";
import { ResponsesModel } from "../src/flagagent/providers/responses.js";
import { AnthropicMessagesModel } from "../src/flagagent/providers/anthropic.js";
import { readEvents } from "../src/flagagent/artifacts.js";

const NOW = new Date("2026-08-14T16:15:30.000Z");

function chatPayload(tool = false): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    object: "chat.completion",
    created: 0,
    model: "m",
    choices: [
      {
        index: 0,
        message: tool
          ? {
              role: "assistant",
              content: "done",
              tool_calls: [
                {
                  id: "c1",
                  type: "function",
                  function: { name: "shell", arguments: '{"command":"echo hi"}' },
                },
              ],
            }
          : { role: "assistant", content: "done", tool_calls: null },
        finish_reason: tool ? "tool_calls" : "stop",
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 2 },
  });
}

function responsesPayload(tool = false): string {
  return JSON.stringify({
    id: "resp_1",
    object: "response",
    created_at: 0,
    status: "completed",
    model: "m",
    output: tool
      ? [
          {
            type: "function_call",
            call_id: "c1",
            name: "shell",
            arguments: '{"command":"echo hi"}',
          },
        ]
      : [
          {
            type: "message",
            id: "msg_1",
            role: "assistant",
            status: "completed",
            content: [{ type: "output_text", text: "done" }],
          },
        ],
    usage: { input_tokens: 3, output_tokens: 2 },
  });
}

function anthropicPayload(tool = false): string {
  return JSON.stringify({
    id: "msg_1",
    type: "message",
    role: "assistant",
    model: "m",
    content: tool
      ? [{ type: "tool_use", id: "c1", name: "shell", input: { command: "echo hi" } }]
      : [{ type: "text", text: "done" }],
    stop_reason: tool ? "tool_use" : "end_turn",
    stop_sequence: null,
    usage: { input_tokens: 3, output_tokens: 2 },
  });
}

type AdapterSpec = {
  name: string;
  make: (base: string) => unknown;
  okBody: string;
  toolBody: string;
};

const ADAPTERS: AdapterSpec[] = [
  {
    name: "chat",
    make: (b) =>
      new ChatCompletionsModel({ model: "m", apiKey: "sk-test", baseURL: b }),
    okBody: chatPayload(false),
    toolBody: chatPayload(true),
  },
  {
    name: "responses",
    make: (b) => new ResponsesModel({ model: "m", apiKey: "sk-test", baseURL: b }),
    okBody: responsesPayload(false),
    toolBody: responsesPayload(true),
  },
  {
    name: "anthropic",
    make: (b) =>
      new AnthropicMessagesModel({ model: "m", apiKey: "sk-test", baseURL: b }),
    okBody: anthropicPayload(false),
    toolBody: anthropicPayload(true),
  },
];

function startServer(
  handler: (
    req: import("node:http").IncomingMessage,
    res: import("node:http").ServerResponse,
  ) => void,
) {
  const server = createServer(handler);
  return new Promise<{ server: import("node:http").Server; port: number }>(
    (resolve) => {
      server.listen(0, "127.0.0.1", () => {
        const addr = server.address() as { port: number };
        resolve({ server, port: addr.port });
      });
    },
  );
}

describe("provider transport via real SDK adapters", () => {
  for (const spec of ADAPTERS) {
    it(`${spec.name}: immediate response parses as model_stop`, async () => {
      const { server, port } = await startServer((_req, res) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(spec.okBody);
      });
      const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
      try {
        const model = spec.make(`http://127.0.0.1:${port}`) as never;
        const loop = new AgentLoop({
          model,
          executor: {
            execute: async () =>
              ({
                stdout: "",
                stderr: "",
                exitCode: 0,
                timedOut: false,
                toObject() {
                  return {
                    stdout: "",
                    stderr: "",
                    exit_code: 0,
                    timed_out: false,
                    truncated: false,
                  };
                },
              }) as never,
          },
          verifier: new ExactStringVerifier("Flag{ok}"),
          challenge: { identity: "fixture", description: "solve it" },
          limits: new Limits({
            maxModelTurns: 3,
            wallTimeoutSeconds: 5,
            commandTimeoutSeconds: 10,
          }),
          runsRoot: tmp,
          monotonic: () => Date.now() / 1000,
          utcNow: () => NOW,
          runId: "FA-20260814T161530Z-a13f4c2d",
        });
        const result = await loop.run();
        expect(result["status:reason"]).toBe("unsolved:model_stop");
        expect(result.model_calls).toBe(1);
      } finally {
        server.close();
        rmSync(tmp, { recursive: true, force: true });
      }
    });

    it(`${spec.name}: slow drip body loses to absolute wall deadline`, async () => {
      const body = spec.okBody;
      const chunk = 15;
      const interval = 400;
      const { server, port } = await startServer((_req, res) => {
        res.writeHead(200, {
          "Content-Type": "application/json",
          "Content-Length": String(Buffer.byteLength(body)),
        });
        let idx = 0;
        const send = () => {
          if (idx >= body.length) {
            res.end();
            return;
          }
          const slice = body.slice(idx, idx + chunk);
          idx += chunk;
          try {
            res.write(slice);
          } catch {
            return;
          }
          setTimeout(send, interval);
        };
        send();
      });
      const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
      try {
        const model = spec.make(`http://127.0.0.1:${port}`) as never;
        const loop = new AgentLoop({
          model,
          executor: {
            execute: async () =>
              ({
                stdout: "",
                stderr: "",
                exitCode: 0,
                timedOut: false,
                toObject() {
                  return {
                    stdout: "",
                    stderr: "",
                    exit_code: 0,
                    timed_out: false,
                    truncated: false,
                  };
                },
              }) as never,
          },
          verifier: new ExactStringVerifier("Flag{ok}"),
          challenge: { identity: "fixture", description: "solve it" },
          limits: new Limits({
            maxModelTurns: 3,
            wallTimeoutSeconds: 0.6,
            commandTimeoutSeconds: 10,
          }),
          runsRoot: tmp,
          monotonic: () => Date.now() / 1000,
          utcNow: () => NOW,
          runId: "FA-20260814T161530Z-a13f4c2d",
        });
        const t0 = Date.now();
        const result = await loop.run();
        const elapsed = (Date.now() - t0) / 1000;
        expect(result["status:reason"]).toBe("unsolved:wall_limit");
        expect(elapsed).toBeLessThan(3);
        const events = readEvents(
          (loop as unknown as { artifacts: { eventsPath: string } }).artifacts
            .eventsPath,
        );
        expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
      } finally {
        server.close();
        rmSync(tmp, { recursive: true, force: true });
      }
    });

    it(`${spec.name}: header stall loses to absolute wall deadline`, async () => {
      const { server, port } = await startServer((_req, res) => {
        setTimeout(() => {
          try {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end("{}");
          } catch {
            /* ignore */
          }
        }, 3000);
      });
      const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
      try {
        const model = spec.make(`http://127.0.0.1:${port}`) as never;
        const loop = new AgentLoop({
          model,
          executor: {
            execute: async () =>
              ({
                stdout: "",
                stderr: "",
                exitCode: 0,
                timedOut: false,
                toObject() {
                  return {
                    stdout: "",
                    stderr: "",
                    exit_code: 0,
                    timed_out: false,
                    truncated: false,
                  };
                },
              }) as never,
          },
          verifier: new ExactStringVerifier("Flag{ok}"),
          challenge: { identity: "fixture", description: "solve it" },
          limits: new Limits({
            maxModelTurns: 3,
            wallTimeoutSeconds: 0.5,
            commandTimeoutSeconds: 10,
          }),
          runsRoot: tmp,
          monotonic: () => Date.now() / 1000,
          utcNow: () => NOW,
          runId: "FA-20260814T161530Z-a13f4c2d",
        });
        const t0 = Date.now();
        const result = await loop.run();
        const elapsed = (Date.now() - t0) / 1000;
        expect(result["status:reason"]).toBe("unsolved:wall_limit");
        expect(elapsed).toBeLessThan(2.5);
        const events = readEvents(
          (loop as unknown as { artifacts: { eventsPath: string } }).artifacts
            .eventsPath,
        );
        expect(events.filter((e) => e.type === "tool_call").length).toBe(0);
      } finally {
        server.close();
        rmSync(tmp, { recursive: true, force: true });
      }
    });
  }

  it("Responses replay survives supervision (builtInput persists across turns)", async () => {
    const calls: unknown[][] = [];
    let count = 0;
    const { server, port } = await startServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        try {
          calls.push(JSON.parse(body).input ?? []);
        } catch {
          calls.push([]);
        }
        count++;
        const payload =
          count === 1
            ? {
                id: "resp_1",
                object: "response",
                created_at: 0,
                status: "completed",
                model: "m",
                output: [
                  {
                    type: "function_call",
                    call_id: "c1",
                    name: "shell",
                    arguments: '{"command":"echo hi"}',
                  },
                ],
              }
            : {
                id: "resp_2",
                object: "response",
                created_at: 0,
                status: "completed",
                model: "m",
                output: [
                  {
                    type: "message",
                    id: "msg_1",
                    role: "assistant",
                    status: "completed",
                    content: [{ type: "output_text", text: "done" }],
                  },
                ],
                usage: { input_tokens: 1, output_tokens: 1 },
              };
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(payload));
      });
    });
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new ResponsesModel({
        model: "m",
        apiKey: "sk-test",
        baseURL: `http://127.0.0.1:${port}`,
      });
      const loop = new AgentLoop({
        model: model as never,
        executor: {
          execute: async () =>
            ({
              stdout: "out",
              stderr: "",
              exitCode: 0,
              timedOut: false,
              toObject() {
                return {
                  stdout: "out",
                  stderr: "",
                  exit_code: 0,
                  timed_out: false,
                  truncated: false,
                };
              },
            }) as never,
        },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 3,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect(calls.length).toBe(2);
      expect(
        calls[1]!.some(
          (x: unknown) =>
            (x as Record<string, unknown>).type === "function_call_output",
        ),
      ).toBe(true);
    } finally {
      server.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("Anthropic thinking persists across turns", async () => {
    const bodies: Record<string, unknown>[] = [];
    let count = 0;
    const { server, port } = await startServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        try {
          bodies.push(JSON.parse(body));
        } catch {
          bodies.push({});
        }
        count++;
        const payload =
          count === 1
            ? {
                id: "msg_1",
                type: "message",
                role: "assistant",
                model: "m",
                content: [
                  { type: "thinking", thinking: "plan foo", signature: "sig-123" },
                  {
                    type: "tool_use",
                    id: "c1",
                    name: "shell",
                    input: { command: "echo hi" },
                  },
                ],
                stop_reason: "tool_use",
                stop_sequence: null,
                usage: { input_tokens: 3, output_tokens: 2 },
              }
            : {
                id: "msg_2",
                type: "message",
                role: "assistant",
                model: "m",
                content: [{ type: "text", text: "done" }],
                stop_reason: "end_turn",
                stop_sequence: null,
                usage: { input_tokens: 3, output_tokens: 2 },
              };
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(payload));
      });
    });
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new AnthropicMessagesModel({
        model: "m",
        apiKey: "sk-test",
        baseURL: `http://127.0.0.1:${port}`,
      });
      const loop = new AgentLoop({
        model: model as never,
        executor: {
          execute: async () =>
            ({
              stdout: "out",
              stderr: "",
              exitCode: 0,
              timedOut: false,
              toObject() {
                return {
                  stdout: "out",
                  stderr: "",
                  exit_code: 0,
                  timed_out: false,
                  truncated: false,
                };
              },
            }) as never,
        },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 3,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      expect(bodies.length).toBe(2);
      const second = bodies[1] as Record<string, unknown>;
      const msgs = (second.messages as unknown[]) ?? [];
      let found = false;
      for (const m of msgs as Record<string, unknown>[]) {
        for (const block of ((m.content as unknown[]) ?? []) as Record<
          string,
          unknown
        >[]) {
          if (block.type === "thinking" && block.signature === "sig-123") found = true;
        }
      }
      expect(found).toBe(true);
    } finally {
      server.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("large 300 KiB response under supervision does not deadlock", async () => {
    const large = "X".repeat(300 * 1024);
    const body = JSON.stringify({
      id: "chatcmpl-1",
      object: "chat.completion",
      created: 0,
      model: "m",
      choices: [
        {
          index: 0,
          message: { role: "assistant", content: large },
          finish_reason: "stop",
        },
      ],
      usage: { prompt_tokens: 11, completion_tokens: 22 },
    });
    const { server, port } = await startServer((_req, res) => {
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Content-Length": String(Buffer.byteLength(body)),
      });
      res.end(body);
    });
    const tmp = mkdtempSync(join(tmpdir(), "flagagent-"));
    try {
      const model = new ChatCompletionsModel({
        model: "m",
        apiKey: "sk-test",
        baseURL: `http://127.0.0.1:${port}`,
      });
      const loop = new AgentLoop({
        model: model as never,
        executor: {
          execute: async () =>
            ({
              stdout: "",
              stderr: "",
              exitCode: 0,
              timedOut: false,
              toObject() {
                return {
                  stdout: "",
                  stderr: "",
                  exit_code: 0,
                  timed_out: false,
                  truncated: false,
                };
              },
            }) as never,
        },
        verifier: new ExactStringVerifier("Flag{ok}"),
        challenge: { identity: "fixture", description: "solve it" },
        limits: new Limits({
          maxModelTurns: 3,
          wallTimeoutSeconds: 5,
          commandTimeoutSeconds: 10,
        }),
        runsRoot: tmp,
        monotonic: () => Date.now() / 1000,
        utcNow: () => NOW,
        runId: "FA-20260814T161530Z-a13f4c2d",
      });
      const result = await loop.run();
      expect(result["status:reason"]).toBe("unsolved:model_stop");
      const events = readEvents(
        (loop as unknown as { artifacts: { eventsPath: string } }).artifacts.eventsPath,
      );
      const mr = events.find((e) => e.type === "model_response");
      expect((mr!.payload as Record<string, unknown>).content).toBe(large);
    } finally {
      server.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
