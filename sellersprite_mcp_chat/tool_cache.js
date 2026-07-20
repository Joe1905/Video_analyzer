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

  async getOrCall(toolName, args, caller) {
    await this.ensureMigrated();
    const request = normalizeCacheValue(args || {});
    const key = this.cacheKey(toolName, request);
    const now = Date.now();
    const ttlMs = this.ttlSeconds * 1000;
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
    if (entry && now - Number(entry.createdAt || 0) <= ttlMs && !this.isError(entry.response)) {
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
          ttl_seconds: this.ttlSeconds,
          age_seconds: Math.round((now - Number(entry.createdAt || now)) / 1000),
        },
      };
    }

    const response = await caller();
    if (!this.isError(response)) {
      const createdAt = Date.now();
      await this.writeEntryIfNewer(key, {
        provider: this.provider,
        scope: this.scope,
        endpoint: String(toolName),
        request,
        response,
        createdAt,
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
        ttl_seconds: this.ttlSeconds,
        age_seconds: 0,
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
