"use strict";

const assert = require("node:assert/strict");
const readline = require("node:readline");
const { StdioMcpClient } = require("./stdio_mcp_client.js");

function runFakeMcpServer() {
  const input = readline.createInterface({ input: process.stdin });
  const send = (id, result) => {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
  };
  input.on("line", (line) => {
    const request = JSON.parse(line);
    if (request.id === undefined) return;
    if (request.method === "initialize") {
      send(request.id, {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "fake-sociavault", version: "2.0.0" },
      });
      return;
    }
    if (request.method === "tools/list") {
      send(request.id, {
        tools: Array.from({ length: 107 }, (_, index) => ({
          name: index === 0 ? "check_credits" : `social_tool_${String(index).padStart(3, "0")}`,
          description: `Fake tool ${index}`,
          inputSchema: { type: "object", properties: {} },
        })),
      });
      return;
    }
    if (request.method === "tools/call") {
      const name = request.params?.name;
      if (name === "timeout") return;
      if (name === "crash") {
        process.exit(17);
        return;
      }
      const delay = Number(request.params?.arguments?.delay || 0);
      setTimeout(() => send(request.id, {
        content: [{
          type: "text",
          text: JSON.stringify({
            name,
            value: request.params?.arguments?.value,
          }),
        }],
      }), delay);
    }
  });
}

async function runTests() {
  const quietLogger = { warn() {} };
  const client = new StdioMcpClient({
    command: process.execPath,
    args: [__filename, "--fake-mcp"],
    requestTimeoutMs: 120,
    logger: quietLogger,
    clientName: "sociavault-stdio-test",
  });

  const tools = await client.listTools();
  assert.equal(tools.length, 107);
  assert.equal(tools[0].name, "check_credits");

  const concurrent = await Promise.all([
    client.callTool("echo", { value: "first", delay: 35 }),
    client.callTool("echo", { value: "second", delay: 5 }),
    client.callTool("echo", { value: "third", delay: 20 }),
  ]);
  assert.deepEqual(
    concurrent.map((result) => JSON.parse(result.content[0].text).value),
    ["first", "second", "third"],
  );

  await assert.rejects(client.callTool("timeout"), /timed out: tools\/call/);
  await assert.rejects(client.callTool("crash"), /exited with code 17/);

  const restartedTools = await client.listTools();
  assert.equal(restartedTools.length, 107);
  const restartedCall = await client.callTool("echo", { value: "restarted" });
  assert.equal(JSON.parse(restartedCall.content[0].text).value, "restarted");

  await client.close();
  process.stdout.write("stdio MCP client tests passed\n");
}

if (process.argv.includes("--fake-mcp")) {
  runFakeMcpServer();
} else {
  runTests().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
