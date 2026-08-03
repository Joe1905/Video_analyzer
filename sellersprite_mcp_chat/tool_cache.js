"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");

function normalizeCacheValue(value) {
  if (Array.isArray(value)) return value.map(normalizeCacheValue);
  if (!value || typeof value !== "object") return value;
  return Object.keys(value)
    .sort()
    .reduce((acc, key) => {
      acc[key] = normalizeCacheValue(value[key]);
      return acc;
    }, {});
}

function canonicalJson(value) {
  return JSON.stringify(normalizeCacheValue(value));
}

async function readJson(filePath) {
  try {
    return JSON.parse(await fs.readFile(filePath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

async function atomicWriteJson(filePath, value) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, JSON.stringify(value), { encoding: "utf8", mode: 0o644 });
    await fs.rename(temporary, filePath);
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
}

const ROW_COLLECTION_KEYS = new Set(["items", "list", "results", "products", "records"]);
const ENTITY_ID_KEYS = new Set(["id", "asin", "productid", "goodsid", "itemid", "creatorid", "shopid", "videoid"]);

function responseSchema(value, depth = 0) {
  if (depth > 6) return "…";
  if (Array.isArray(value)) return [value.length ? responseSchema(value[0], depth + 1) : "[]"];
  if (!value || typeof value !== "object") return typeof value;
  return Object.keys(value).sort().reduce((shape, key) => {
    shape[key] = responseSchema(value[key], depth + 1);
    return shape;
  }, {});
}

function getPath(value, pathText) {
  return String(pathText || "").split(".").filter(Boolean).reduce(
    (current, key) => (current && typeof current === "object" ? current[key] : undefined), value,
  );
}

function omitPaths(value, paths) {
  const output = JSON.parse(JSON.stringify(value || {}));
  for (const pathText of paths) {
    const parts = String(pathText || "").split(".").filter(Boolean);
    const key = parts.pop();
    const parent = parts.reduce(
      (current, part) => (current && typeof current === "object" ? current[part] : undefined), output,
    );
    if (parent && typeof parent === "object" && key) delete parent[key];
  }
  return output;
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function locateRowCollections(value, found = []) {
  if (!value || typeof value !== "object") return found;
  if (Array.isArray(value)) {
    for (const item of value) locateRowCollections(item, found);
    return found;
  }
  for (const [key, item] of Object.entries(value)) {
    if (Array.isArray(item) && ROW_COLLECTION_KEYS.has(String(key).toLowerCase())) {
      found.push({ parent: value, key, rows: item });
    } else {
      locateRowCollections(item, found);
    }
  }
  return found;
}

function parseMcpTextCollections(response) {
  const copy = cloneJson(response);
  const holders = [];
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {
      for (const item of value) visit(item);
      return;
    }
    if (value.type === "text" && typeof value.text === "string") {
      try {
        holders.push({ value, parsed: JSON.parse(value.text) });
      } catch {}
    }
    for (const item of Object.values(value)) visit(item);
  };
  visit(copy);
  return { copy, holders };
}

function projectMcpRows(response, requestedCount, wantedEntityIds = null) {
  if (!Number.isInteger(requestedCount) || requestedCount < 1) return null;
  const { copy, holders } = parseMcpTextCollections(response);
  const collections = holders.flatMap((holder) => locateRowCollections(holder.parsed).map((collection) => ({ ...collection, holder })));
  if (collections.length !== 1) return null;
  const collection = collections[0];
  let rows = collection.rows;
  if (wantedEntityIds) {
    const wanted = new Set(wantedEntityIds.map((value) => String(value)));
    const rowEntityId = (row) => {
      if (!row || typeof row !== "object") return "";
      for (const [key, value] of Object.entries(row)) {
        if (ENTITY_ID_KEYS.has(String(key).replace(/[^a-z0-9]/gi, "").toLowerCase()) && value != null) return String(value);
      }
      return "";
    };
    const matched = rows.filter((row) => wanted.has(rowEntityId(row)));
    if (matched.length !== wanted.size || new Set(matched.map(rowEntityId)).size !== wanted.size) return null;
    rows = matched;
  } else if (rows.length < requestedCount) {
    return null;
  } else {
    rows = rows.slice(0, requestedCount);
  }
  collection.parent[collection.key] = rows;
  collection.holder.value.text = JSON.stringify(collection.holder.parsed);
  return { value: copy, projectedRows: rows.length };
}

class ToolCacheStore {
  constructor({ rootDir, provider, scope, legacyFile, ttlSeconds, isError, logger = console }) {
    if (!rootDir || !provider || !scope) throw new Error("ToolCacheStore requires rootDir, provider and scope");
    this.provider = String(provider);
    this.scope = String(scope);
    this.legacyFile = legacyFile ? String(legacyFile) : "";
    this.ttlSeconds = Math.max(1, Number(ttlSeconds) || 1);
    this.isError = typeof isError === "function" ? isError : () => false;
    this.logger = logger;
    this.entryDir = path.join(String(rootDir), this.provider, this.scope);
    this.migrationPromise = null;
  }

  cacheKey(toolName, args) {
    return createHash("sha256")
      .update(canonicalJson({
        provider: this.provider,
        scope: this.scope,
        endpoint: String(toolName || ""),
        // Preserve the legacy empty fields so pre-unification exact entries
        // stay readable. Skill/report versions never split raw MCP caching.
        namespace: "",
        schema_version: "",
        request: args || {},
      }))
      .digest("hex");
  }

  entryPath(key) {
    if (!/^[a-f0-9]{64}$/.test(String(key))) throw new Error("Invalid tool cache key");
    return path.join(this.entryDir, `${key}.json`);
  }

  async readEntry(key) {
    try {
      const entry = await readJson(this.entryPath(key));
      if (!entry || entry.provider !== this.provider || entry.scope !== this.scope) return null;
      return entry;
    } catch (error) {
      this.logger.warn(`Could not read shared tool cache entry ${String(key).slice(0, 16)}: ${error.message}`);
      return null;
    }
  }

  async writeEntryIfNewer(key, incoming) {
    return this.withEntryLock(key, async () => {
      const current = await this.readEntry(key);
      if (current && Number(current.createdAt || 0) > Number(incoming.createdAt || 0)) return current;
      await atomicWriteJson(this.entryPath(key), incoming);
      return incoming;
    });
  }

  async recordHit(key) {
    return this.withEntryLock(key, async () => {
      const current = await this.readEntry(key);
      if (!current) return;
      await atomicWriteJson(this.entryPath(key), {
        ...current,
        hitCount: Number(current.hitCount || 0) + 1,
        lastHitAt: Date.now(),
      });
    });
  }

  async withEntryLock(key, callback) {
    await fs.mkdir(this.entryDir, { recursive: true });
    const lockDir = path.join(this.entryDir, `.${key}.lock`);
    let acquired = false;
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try {
        await fs.mkdir(lockDir);
        acquired = true;
        break;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        try {
          const stat = await fs.stat(lockDir);
          if (Date.now() - stat.mtimeMs > 300_000) {
            await fs.rm(lockDir, { recursive: true, force: true });
            continue;
          }
        } catch (statError) {
          if (statError.code !== "ENOENT") throw statError;
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    if (!acquired) throw new Error("Timed out waiting for shared tool cache entry lock");
    try {
      return await callback();
    } finally {
      await fs.rm(lockDir, { recursive: true, force: true });
    }
  }

  async withMigrationLock(callback) {
    await fs.mkdir(this.entryDir, { recursive: true });
    const lockDir = path.join(this.entryDir, ".migration-lock");
    let acquired = false;
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try {
        await fs.mkdir(lockDir);
        acquired = true;
        break;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        try {
          const stat = await fs.stat(lockDir);
          if (Date.now() - stat.mtimeMs > 300_000) {
            await fs.rm(lockDir, { recursive: true, force: true });
            continue;
          }
        } catch (statError) {
          if (statError.code !== "ENOENT") throw statError;
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    if (!acquired) throw new Error("Timed out waiting for shared tool cache migration lock");
    try {
      return await callback();
    } finally {
      await fs.rm(lockDir, { recursive: true, force: true });
    }
  }

  async migrateLegacy() {
    if (!this.legacyFile) return 0;
    let legacy;
    try {
      legacy = await readJson(this.legacyFile);
    } catch (error) {
      this.logger.warn(`Could not read legacy tool cache ${this.legacyFile}: ${error.message}`);
      return 0;
    }
    if (!legacy || typeof legacy !== "object" || Array.isArray(legacy)) return 0;
    return this.withMigrationLock(async () => {
      let migrated = 0;
      for (const value of Object.values(legacy)) {
        if (!value || typeof value !== "object" || this.isError(value.response)) continue;
        if (value.provider && String(value.provider) !== this.provider) continue;
        const endpoint = String(value.endpoint || "");
        if (!endpoint) continue;
        const request = normalizeCacheValue(value.request || {});
        const key = this.cacheKey(endpoint, request);
        await this.writeEntryIfNewer(key, {
          provider: this.provider,
          scope: this.scope,
          endpoint,
          request,
          response: value.response,
          createdAt: Number(value.createdAt || 0),
          lastHitAt: value.lastHitAt == null ? null : Number(value.lastHitAt),
          hitCount: Number(value.hitCount || 0),
        });
        migrated += 1;
      }
      return migrated;
    });
  }

  async ensureMigrated() {
    if (!this.migrationPromise) {
      this.migrationPromise = this.migrateLegacy().catch((error) => {
        this.logger.warn(`Could not migrate legacy tool cache: ${error.message}`);
        return 0;
      });
    }
    return this.migrationPromise;
  }

  isFreshEntry(entry, now) {
    const ttlSeconds = Math.max(1, Number(entry?.ttl_seconds || this.ttlSeconds) || this.ttlSeconds);
    return Boolean(
      entry
      && entry.provider === this.provider
      && entry.scope === this.scope
      && now - Number(entry.createdAt || 0) <= ttlSeconds * 1000
      && !this.isError(entry.response),
    );
  }

  async coveredEntry(toolName, request, now, coveragePolicy) {
    const policy = coveragePolicy && typeof coveragePolicy === "object" ? coveragePolicy : {};
    const candidates = [];
    try {
      const names = await fs.readdir(this.entryDir);
      for (const name of names) {
        if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
        const entry = await readJson(path.join(this.entryDir, name));
        if (
          this.isFreshEntry(entry, now)
          && String(entry.endpoint || "") === String(toolName || "")
          // Old V2/report namespaces cannot prove raw-response compatibility.
          // Leave them untouched and let their original TTL expire naturally.
          && !String(entry.namespace || "")
          && !String(entry.schema_version || "")
          && canonicalJson(entry.request || {}) !== canonicalJson(request)
        ) candidates.push({ key: name.slice(0, -5), entry });
      }
    } catch (error) {
      if (error.code !== "ENOENT") this.logger.warn(`Could not inspect shared tool cache coverage: ${error.message}`);
      return null;
    }

    for (const { key, entry } of candidates) {
      for (const limitPath of policy.limit_paths || []) {
        const cachedLimit = Number(getPath(entry.request, limitPath));
        const requestedLimit = Number(getPath(request, limitPath));
        if (
          Number.isInteger(cachedLimit) && Number.isInteger(requestedLimit)
          && cachedLimit > requestedLimit && requestedLimit > 0
          && canonicalJson(omitPaths(entry.request, [limitPath])) === canonicalJson(omitPaths(request, [limitPath]))
        ) {
          const projected = projectMcpRows(entry.response, requestedLimit);
          if (projected) return { key, entry, value: projected.value, projectedRows: projected.projectedRows };
        }
      }
      for (const entityPath of policy.entity_list_paths || []) {
        const cachedEntities = getPath(entry.request, entityPath);
        const requestedEntities = getPath(request, entityPath);
        if (
          Array.isArray(cachedEntities) && Array.isArray(requestedEntities) && requestedEntities.length
          && requestedEntities.every((value) => cachedEntities.map(String).includes(String(value)))
          && canonicalJson(omitPaths(entry.request, [entityPath])) === canonicalJson(omitPaths(request, [entityPath]))
        ) {
          const projected = projectMcpRows(entry.response, requestedEntities.length, requestedEntities);
          if (projected) return { key, entry, value: projected.value, projectedRows: projected.projectedRows };
        }
      }
    }
    return null;
  }

  async getOrCall(toolName, args, caller, options = {}) {
    await this.ensureMigrated();
    const normalizer = typeof options.normalizeRequest === "function"
      ? options.normalizeRequest
      : (value) => value;
    const request = normalizeCacheValue(normalizer(args || {}));
    const key = this.cacheKey(toolName, request);
    const now = Date.now();
    let entry = await this.readEntry(key);
    if (
      entry
      && (
        String(entry.endpoint || "") !== String(toolName || "")
        || canonicalJson(entry.request || {}) !== canonicalJson(request)
      )
    ) {
      this.logger.warn(`Ignored mismatched shared tool cache entry ${key.slice(0, 16)}`);
      entry = null;
    }
    const entryTtlSeconds = Math.max(1, Number(entry?.ttl_seconds || this.ttlSeconds) || this.ttlSeconds);
    if (this.isFreshEntry(entry, now)) {
      this.recordHit(key).catch((error) => {
        this.logger.warn(`Could not update shared tool cache hit metadata: ${error.message}`);
      });
      return {
        value: entry.response,
        meta: {
          hit: true,
          label: "缓存命中",
          provider: this.provider,
          endpoint: String(toolName),
          ttl_seconds: entryTtlSeconds,
          age_seconds: Math.round((now - Number(entry.createdAt || now)) / 1000),
          match: "exact",
          projected_rows: 0,
        },
      };
    }

    const covered = await this.coveredEntry(toolName, request, now, options.coveragePolicy);
    if (covered) {
      this.recordHit(covered.key).catch((error) => {
        this.logger.warn(`Could not update covered shared tool cache hit metadata: ${error.message}`);
      });
      return {
        value: covered.value,
        meta: {
          hit: true,
          label: "缓存覆盖命中",
          provider: this.provider,
          endpoint: String(toolName),
          ttl_seconds: Math.max(1, Number(covered.entry.ttl_seconds || this.ttlSeconds) || this.ttlSeconds),
          age_seconds: Math.round((now - Number(covered.entry.createdAt || now)) / 1000),
          match: "covered",
          projected_rows: covered.projectedRows,
        },
      };
    }

    const response = await caller();
    if (!this.isError(response)) {
      const createdAt = Date.now();
      const ttlSeconds = Math.max(
        1,
        Number(
          typeof options.ttlSecondsForResponse === "function"
            ? options.ttlSecondsForResponse(response)
            : this.ttlSeconds,
        ) || this.ttlSeconds,
      );
      await this.writeEntryIfNewer(key, {
        provider: this.provider,
        scope: this.scope,
        endpoint: String(toolName),
        request,
        response,
        raw_business_response: response,
        response_schema: { version: "mcp-response-v1", shape: responseSchema(response) },
        createdAt,
        ttl_seconds: ttlSeconds,
        lastHitAt: null,
        hitCount: 0,
      });
    }
    return {
      value: response,
      meta: {
        hit: false,
        label: "实时调用",
        provider: this.provider,
        endpoint: String(toolName),
        ttl_seconds: Math.max(1, Number(
          typeof options.ttlSecondsForResponse === "function"
            ? options.ttlSecondsForResponse(response)
            : this.ttlSeconds,
        ) || this.ttlSeconds),
        age_seconds: 0,
        match: "miss",
        projected_rows: 0,
      },
    };
  }
}

module.exports = {
  ToolCacheStore,
  atomicWriteJson,
  canonicalJson,
  normalizeCacheValue,
};
