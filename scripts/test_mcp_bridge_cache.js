#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {
  mcpToolResponseIsError,
  mcpToolResponseIsNormalEmpty,
  normalizeToolCacheRequest,
  shouldBypassToolCache,
  toolCacheCoveragePolicy,
} = require("../sellersprite_mcp_chat/server.js");
const { ToolCacheStore } = require("../sellersprite_mcp_chat/tool_cache.js");

async function main() {
  assert.equal(mcpToolResponseIsError({ isError: true, content: [{ type: "text", text: "keyword is required" }] }), true);
  assert.equal(mcpToolResponseIsError({ error: { message: "failed" } }), true);
  assert.equal(mcpToolResponseIsError({ isError: false, content: [{ type: "text", text: "ok" }] }), false);
  assert.equal(mcpToolResponseIsError(null), false);
  assert.equal(shouldBypassToolCache("sociavault", "check_credits"), true);
  assert.equal(shouldBypassToolCache("sociavault", "tiktok_profile"), false);
  assert.equal(shouldBypassToolCache("sellersprite", "check_credits"), false);
  assert.equal(mcpToolResponseIsNormalEmpty({ content: [{ type: "text", text: JSON.stringify({ items: [] }) }] }), true);
  assert.equal(mcpToolResponseIsNormalEmpty({ content: [{ type: "text", text: JSON.stringify({ items: [{ product_id: "p1" }] }) }] }), false);
  assert.deepEqual(
    normalizeToolCacheRequest({ query: ["fidget toys"], top_k: "5", filter: { region: "us", ignored: "" } }),
    { query: ["fidget toys"], top_k: 5, filter: { region: "US" } },
  );
  assert.deepEqual(
    normalizeToolCacheRequest({ action: "search", marketplace: "US" }, "chuhaijiang", "amazon"),
    { action: "search", marketplace: "us" },
  );
  assert.deepEqual(
    normalizeToolCacheRequest({ entity: "products", country: "us" }, "chuhaijiang", "search"),
    { entity: "products", country: "US" },
  );

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

    const coverageCache = new ToolCacheStore({ ...options, rootDir: path.join(tempRoot, "coverage") });
    let coverageCalls = 0;
    const rowResponse = (ids) => ({ content: [{ type: "text", text: JSON.stringify({ items: ids.map((product_id) => ({ product_id, price: 10 })) }) }] });
    await coverageCache.getOrCall("product_search", { filter: { region: "US" }, top_k: 10 }, async () => {
      coverageCalls += 1;
      return rowResponse(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]);
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    const coveredTopK = await coverageCache.getOrCall("product_search", { filter: { region: "US" }, top_k: 5 }, async () => {
      coverageCalls += 1;
      throw new Error("covered top_k request should not call MCP");
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(coveredTopK.meta.match, "covered");
    assert.equal(coveredTopK.meta.projected_rows, 5);
    assert.equal(JSON.parse(coveredTopK.value.content[0].text).items.length, 5);
    const differentMarket = await coverageCache.getOrCall("product_search", { filter: { region: "UK" }, top_k: 5 }, async () => {
      coverageCalls += 1;
      return rowResponse(["UK1"]);
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(differentMarket.meta.match, "miss");
    const insufficientRows = await coverageCache.getOrCall("product_search", { filter: { region: "CA" }, top_k: 10 }, async () => rowResponse(["CA1", "CA2"]), { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    const shortRequest = await coverageCache.getOrCall("product_search", { filter: { region: "CA" }, top_k: 5 }, async () => {
      coverageCalls += 1;
      return rowResponse(["CA1"]);
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(insufficientRows.meta.match, "miss");
    assert.equal(shortRequest.meta.match, "miss");
    const differentSort = await coverageCache.getOrCall("product_search", { filter: { region: "US" }, top_k: 5, orderby: [{ field: "gmv", order: "desc" }] }, async () => {
      coverageCalls += 1;
      return rowResponse(["S1"]);
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(differentSort.meta.match, "miss");
    const differentPeriod = await coverageCache.getOrCall("product_search", { filter: { region: "US", date_value: "2026-W30" }, top_k: 5 }, async () => {
      coverageCalls += 1;
      return rowResponse(["T1"]);
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(differentPeriod.meta.match, "miss");
    assert.equal(coverageCalls, 5);

    const entityCache = new ToolCacheStore({ ...options, rootDir: path.join(tempRoot, "entity-coverage") });
    await entityCache.getOrCall("product_search", { filter: { region: "US" }, product_ids: ["A", "B", "C"] }, async () => rowResponse(["A", "B", "C"]), { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    const coveredEntities = await entityCache.getOrCall("product_search", { filter: { region: "US" }, product_ids: ["A", "B"] }, async () => {
      throw new Error("covered entity subset should not call MCP");
    }, { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(coveredEntities.meta.match, "covered");
    assert.deepEqual(JSON.parse(coveredEntities.value.content[0].text).items.map((item) => item.product_id), ["A", "B"]);

    const expiryCache = new ToolCacheStore({ ...options, rootDir: path.join(tempRoot, "expiry"), ttlSeconds: 1 });
    await expiryCache.getOrCall("product_search", { filter: { region: "US" }, top_k: 1 }, async () => rowResponse(["E1"]), { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    const [expiryFile] = (await fs.readdir(expiryCache.entryDir)).filter((name) => name.endsWith(".json"));
    const expiryPath = path.join(expiryCache.entryDir, expiryFile);
    const expiredEntry = JSON.parse(await fs.readFile(expiryPath, "utf8"));
    expiredEntry.createdAt = Date.now() - 2_000;
    await fs.writeFile(expiryPath, JSON.stringify(expiredEntry));
    const expired = await expiryCache.getOrCall("product_search", { filter: { region: "US" }, top_k: 1 }, async () => rowResponse(["E2"]), { coveragePolicy: toolCacheCoveragePolicy("fastmoss", "product_search") });
    assert.equal(expired.meta.match, "miss");

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

    const cacheFiles = (await fs.readdir(production.entryDir)).filter((name) => name.endsWith(".json"));
    const cacheEntry = JSON.parse(await fs.readFile(path.join(production.entryDir, cacheFiles[0]), "utf8"));
    assert.equal(cacheEntry.ttl_seconds, 3600);
    assert.equal(cacheEntry.response_schema.version, "mcp-response-v1");
    assert.ok(cacheEntry.raw_business_response);

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
