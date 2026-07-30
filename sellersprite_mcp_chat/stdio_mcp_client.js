const { randomUUID } = require("node:crypto");
const { spawn } = require("node:child_process");

class StdioMcpClient {
  constructor({
    command,
    args = [],
    env = process.env,
    requestTimeoutMs = 180_000,
    logger = console,
    clientName = "stdio-mcp-bridge",
  }) {
    if (!command) throw new Error("MCP stdio command is required");
    this.command = command;
    this.args = Array.isArray(args) ? args : [];
    this.env = env;
    this.requestTimeoutMs = requestTimeoutMs;
    this.logger = logger;
    this.clientName = clientName;
    this.process = null;
    this.startPromise = null;
    this.initializePromise = null;
    this.pending = new Map();
    this.stdoutBuffer = "";
    this.cachedTools = null;
    this.closing = false;
  }

  async ensureProcess() {
    if (this.process && this.process.exitCode === null && !this.process.killed) return this.process;
    if (this.startPromise) return this.startPromise;
    this.closing = false;
    this.startPromise = new Promise((resolve, reject) => {
      const child = spawn(this.command, this.args, {
        env: this.env,
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
      this.process = child;
      this.stdoutBuffer = "";

      const failStart = (error) => {
        reject(error);
        this._handleProcessFailure(error);
      };
      child.once("spawn", () => resolve(child));
      child.once("error", failStart);
      child.once("exit", (code, signal) => {
        const suffix = signal ? `signal ${signal}` : `code ${code}`;
        this._handleProcessFailure(new Error(`MCP stdio process exited with ${suffix}`));
      });
      child.stdout.on("data", (chunk) => this._handleStdout(chunk));
      child.stderr.on("data", (chunk) => {
        const line = chunk.toString("utf8").trim();
        if (line) this.logger.warn(`[${this.clientName}] ${line}`);
      });
    }).finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  _handleStdout(chunk) {
    this.stdoutBuffer += chunk.toString("utf8");
    for (;;) {
      const newline = this.stdoutBuffer.indexOf("\n");
      if (newline < 0) return;
      const line = this.stdoutBuffer.slice(0, newline).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch (error) {
        this.logger.warn(`[${this.clientName}] ignored invalid JSON-RPC output: ${line.slice(0, 300)}`);
        continue;
      }
      if (message.id === undefined || message.id === null) continue;
      const pending = this.pending.get(String(message.id));
      if (!pending) continue;
      this.pending.delete(String(message.id));
      clearTimeout(pending.timer);
      if (message.error) {
        pending.reject(new Error(message.error.message || JSON.stringify(message.error)));
      } else {
        pending.resolve(message.result);
      }
    }
  }

  _handleProcessFailure(error) {
    const child = this.process;
    this.process = null;
    this.initializePromise = null;
    this.cachedTools = null;
    this.stdoutBuffer = "";
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    if (!this.closing && child) this.logger.warn(`[${this.clientName}] ${error.message}`);
  }

  async _write(message) {
    const child = await this.ensureProcess();
    if (!child.stdin || child.stdin.destroyed) throw new Error("MCP stdio stdin is unavailable");
    await new Promise((resolve, reject) => {
      child.stdin.write(`${JSON.stringify(message)}\n`, "utf8", (error) => {
        if (error) reject(error);
        else resolve();
      });
    });
  }

  async request(method, params = {}) {
    await this.ensureProcess();
    const id = randomUUID();
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP stdio request timed out: ${method}`));
      }, this.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
    });
    try {
      await this._write({ jsonrpc: "2.0", id, method, params });
    } catch (error) {
      const pending = this.pending.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(id);
        pending.reject(error);
      }
    }
    return result;
  }

  async initialize() {
    if (this.initializePromise) return this.initializePromise;
    this.initializePromise = (async () => {
      await this.request("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: this.clientName, version: "1.0.0" },
      });
      await this._write({ jsonrpc: "2.0", method: "notifications/initialized" });
    })().catch((error) => {
      this.initializePromise = null;
      throw error;
    });
    return this.initializePromise;
  }

  async listTools() {
    if (this.cachedTools) return this.cachedTools;
    await this.initialize();
    const result = await this.request("tools/list", {});
    this.cachedTools = Array.isArray(result?.tools) ? result.tools : [];
    return this.cachedTools;
  }

  async callTool(name, args = {}) {
    await this.initialize();
    return this.request("tools/call", { name, arguments: args || {} });
  }

  async close() {
    this.closing = true;
    const child = this.process;
    this.process = null;
    this.initializePromise = null;
    this.cachedTools = null;
    if (child && child.exitCode === null && !child.killed) child.kill();
  }
}

module.exports = { StdioMcpClient };
