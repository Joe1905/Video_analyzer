#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { mcpToolResponseIsError } = require("../sellersprite_mcp_chat/server.js");
const { ToolCacheStore } = require("../sellersprite_mcp_chat/tool_cache.js");

async function main() {
  assert.equal(mcpToolResponseIsError({ isError: true, content: [{ type: "text", text: "keyword is required" }] }), true);
  assert.equal(mcpToolResponseIsError({ error: { message: "failed" } }), true);
  assert.equal(mcpToolResponseIsError({ isError: false, content: [{ type: "text", text: "ok" }] }), false);
  assert.equal(mcpToolResponseIsError(null), false);

  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "mcp-shared-cache-test-"));
  try {
    const options = {
      rootDir: path.join(tempRoot, "shared"),
      provider: "sellersprite_mcp",
      scope: "same-credential-fingerprint",
      ttlSeconds: 3600,
      isError: mcpToolResponseIsError,
      logger: { warn() {} },
    };
    const production = new ToolCacheStore(options);
    const development = new ToolCacheStore(options);
    let liveCalls = 0;
    const first = await production.getOrCall("keyword_research", { marketplace: "US", keyword: "stroller fan" }, async () => {
      liveCalls += 1;
      return { content: [{ type: "text", text: "production result" }], isError: false };
    });
    const second = await development.getOrCall("keyword_research", { keyword: "stroller fan", marketplace: "US" }, async () => {
      liveCalls += 1;
      return { content: [{ type: "text", text: "should not run" }], isError: false };
    });
    assert.equal(first.meta.hit, false);
    assert.equal(second.meta.hit, true);
    assert.equal(liveCalls, 1);
    assert.equal(second.value.content[0].text, "production result");

    const otherRegion = await development.getOrCall("keyword_research", { marketplace: "UK", keyword: "stroller fan" }, async () => {
      liveCalls += 1;
      return { content: [{ type: "text", text: "UK result" }], isError: false };
    });
    assert.equal(otherRegion.meta.hit, false);

    const otherCredential = new ToolCacheStore({ ...options, scope: "different-credential-fingerprint" });
    const isolated = await otherCredential.getOrCall("keyword_research", { marketplace: "US", keyword: "stroller fan" }, async () => {
      liveCalls += 1;
      return { content: [{ type: "text", text: "isolated result" }], isError: false };
    });
    assert.equal(isolated.meta.hit, false);
    assert.equal(isolated.value.content[0].text, "isolated result");

    const otherProvider = new ToolCacheStore({ ...options, provider: "fastmoss_mcp" });
    const providerIsolated = await otherProvider.getOrCall("keyword_research", { marketplace: "US", keyword: "stroller fan" }, async () => ({
      content: [{ type: "text", text: "other provider result" }], isError: false,
    }));
    assert.equal(providerIsolated.meta.hit, false);
    assert.equal(providerIsolated.value.content[0].text, "other provider result");

    const exactArgs = await development.getOrCall("keyword_research", { marketplace: "US", keyword: "stroller fan " }, async () => ({
      content: [{ type: "text", text: "whitespace-distinct result" }], isError: false,
    }));
    assert.equal(exactArgs.meta.hit, false);

    const concurrentOptions = { ...options, rootDir: path.join(tempRoot, "concurrent-shared") };
    const concurrentA = new ToolCacheStore(concurrentOptions);
    const concurrentB = new ToolCacheStore(concurrentOptions);
    await Promise.all([
      concurrentA.getOrCall("product_node", { marketplace: "US", keyword: "concurrent" }, async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
        return { content: [{ type: "text", text: "latest response" }], isError: false };
      }),
      concurrentB.getOrCall("product_node", { marketplace: "US", keyword: "concurrent" }, async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        return { content: [{ type: "text", text: "earlier response" }], isError: false };
      }),
    ]);
    const concurrentFinal = await new ToolCacheStore(concurrentOptions).getOrCall(
      "product_node",
      { marketplace: "US", keyword: "concurrent" },
      async () => { throw new Error("concurrent cache entry should exist"); },
    );
    assert.equal(concurrentFinal.meta.hit, true);
    assert.equal(concurrentFinal.value.content[0].text, "latest response");

    let errorCalls = 0;
    for (let index = 0; index < 2; index += 1) {
      const failed = await production.getOrCall("review", { marketplace: "US", asin: "INVALID" }, async () => {
        errorCalls += 1;
        return { isError: true, error: "invalid ASIN" };
      });
      assert.equal(failed.meta.hit, false);
    }
    assert.equal(errorCalls, 2);

    const now = Date.now();
    const legacyProduction = path.join(tempRoot, "legacy-production.json");
    const legacyDevelopment = path.join(tempRoot, "legacy-development.json");
    const request = { marketplace: "US", asin: "B0TESTCACHE" };
    await fs.writeFile(legacyProduction, JSON.stringify({ old: {
      provider: "sellersprite_mcp", endpoint: "asin_detail", request,
      response: { content: [{ type: "text", text: "older" }] }, createdAt: now - 1000,
    } }));
    await fs.writeFile(legacyDevelopment, JSON.stringify({ newer: {
      provider: "sellersprite_mcp", endpoint: "asin_detail", request,
      response: { content: [{ type: "text", text: "newer" }] }, createdAt: now,
    } }));
    const legacyOptions = { ...options, rootDir: path.join(tempRoot, "legacy-shared") };
    await new ToolCacheStore({ ...legacyOptions, legacyFile: legacyProduction }).ensureMigrated();
    const migratedDevelopment = new ToolCacheStore({ ...legacyOptions, legacyFile: legacyDevelopment });
    await migratedDevelopment.ensureMigrated();
    const migrated = await migratedDevelopment.getOrCall("asin_detail", request, async () => {
      throw new Error("newer migrated entry should have been shared");
    });
    assert.equal(migrated.meta.hit, true);
    assert.equal(migrated.value.content[0].text, "newer");

    const cacheFiles = (await fs.readdir(production.entryDir)).filter((name) => name.endsWith(".json"));
    assert.ok(cacheFiles.length >= 2);
    for (const file of cacheFiles) JSON.parse(await fs.readFile(path.join(production.entryDir, file), "utf8"));
  } finally {
    await fs.rm(tempRoot, { recursive: true, force: true });
  }

  console.log("MCP bridge cache tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
