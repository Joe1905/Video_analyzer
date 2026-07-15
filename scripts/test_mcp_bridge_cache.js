#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const { mcpToolResponseIsError } = require("../sellersprite_mcp_chat/server.js");

assert.equal(mcpToolResponseIsError({ isError: true, content: [{ type: "text", text: "keyword is required" }] }), true);
assert.equal(mcpToolResponseIsError({ error: { message: "failed" } }), true);
assert.equal(mcpToolResponseIsError({ isError: false, content: [{ type: "text", text: "ok" }] }), false);
assert.equal(mcpToolResponseIsError(null), false);

console.log("MCP bridge cache tests passed");
