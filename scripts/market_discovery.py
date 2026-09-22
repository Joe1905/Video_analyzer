"""Isolated, model-driven market research API. No UI, scheduler or chat state."""
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs

PREFIX = "/api/market-discovery/"
SKILLS = Path(__file__).parent / "market_discovery_skills"
DATA = Path.cwd() / "data" / "market_discovery"
# Explicit read-only research tools; never inherit an official preset's permissions.
SELLER_TOOLS = frozenset({
    "google_trend", "aba_research_weekly", "aba_research_monthly", "aba_research_trend",
    "keyword_research", "keyword_research_trends", "keyword_miner", "market_research",
    "product_node", "product_research", "competitor_lookup", "product_detail",
    "market_ebc_distribution", "market_price_distribution", "market_ratings_count_distribution",
    "market_listing_date_distribution", "market_product_demand_trend", "market_product_concentration",
    "market_brand_concentration", "market_seller_concentration",
})


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ApiError(400, "Invalid workspace or category identifier")
    return value


def document_key(value):
    if not isinstance(value, str) or len(value) > 240 or "\\" in value:
        raise ApiError(400, "Invalid document key")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ApiError(400, "Invalid document key")
    if path.suffix not in {".md", ".json"} or not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ApiError(400, "Document key must be a relative .md or .json path")
    return value


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


class Store:
    def __init__(self, root=DATA):
        self.root = Path(root)

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / "research.sqlite", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS categories (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    record TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(workspace,id));
                CREATE TABLE IF NOT EXISTS category_versions (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    record TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(workspace,id,revision));
                CREATE TABLE IF NOT EXISTS documents (
                    workspace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
                    content TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(workspace,key,revision));
                CREATE TABLE IF NOT EXISTS calls (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    request TEXT NOT NULL, result TEXT NOT NULL, started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL, PRIMARY KEY(workspace,id));
            """)
            yield db
            db.commit()
        finally:
            db.close()

    def category(self, workspace, cid, revision=None):
        identifier(workspace)
        identifier(cid)
        with self.db() as db:
            if revision is None:
                row = db.execute("SELECT * FROM categories WHERE workspace=? AND id=?", (workspace, cid)).fetchone()
            else:
                row = db.execute("SELECT * FROM category_versions WHERE workspace=? AND id=? AND revision=?",
                                 (workspace, cid, int(revision))).fetchone()
        if not row:
            raise ApiError(404, "Category not found")
        return {**json.loads(row["record"]), "id": cid, "revision": row["revision"], "updated_at": row["updated_at"]}

    def save_category(self, workspace, payload):
        identifier(workspace)
        record = payload.get("record")
        if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"].strip():
            raise ApiError(400, "record.name is required")
        aliases = record.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise ApiError(400, "record.aliases must be an array of strings")
        cid = identifier(payload["id"]) if payload.get("id") else uuid.uuid4().hex
        content = json.dumps(record, ensure_ascii=False)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT revision FROM categories WHERE workspace=? AND id=?", (workspace, cid)).fetchone()
            revision = old["revision"] if old else 0
            if payload.get("expected_revision", 0) != revision:
                raise ApiError(409, "Category changed; read current revision before updating")
            revision += 1
            stamp = now()
            db.execute("INSERT OR REPLACE INTO categories VALUES (?,?,?,?,?)", (workspace, cid, revision, content, stamp))
            db.execute("INSERT INTO category_versions VALUES (?,?,?,?,?)", (workspace, cid, revision, content, stamp))
        return self.category(workspace, cid)

    def search(self, workspace, query="", limit=30):
        identifier(workspace)
        if not isinstance(query, str) or len(query) > 500:
            raise ApiError(400, "Invalid search query")
        limit = min(100, max(1, int(limit)))
        terms = [normalized(q) for q in re.split(r"[,，;；\n]+", query) if normalized(q)]
        with self.db() as db:
            rows = db.execute("SELECT * FROM categories WHERE workspace=? ORDER BY updated_at DESC", (workspace,)).fetchall()
        matches = []
        for row in rows:
            record = json.loads(row["record"])
            fields = [record["name"], *record.get("aliases", [])]
            fields += [str(record.get(k, "")) for k in ("need", "audience", "scene", "region")]
            fields = [normalized(f) for f in fields if normalized(f)]
            score = max((1.0 if q in f else difflib.SequenceMatcher(None, q, f).ratio()
                         for q in terms for f in fields), default=0.0)
            # Retrieval relevance only; never a feasibility score or auto-merge decision.
            if not terms or score >= 0.3:
                matches.append({**record, "id": row["id"], "revision": row["revision"],
                                "updated_at": row["updated_at"], "match_score": round(score, 3)})
        return sorted(matches, key=lambda r: r["match_score"], reverse=True)[:limit]

    def save_document(self, workspace, payload):
        identifier(workspace)
        key = document_key(payload.get("key"))
        content = payload.get("content")
        if not isinstance(content, str):
            raise ApiError(400, "content must be text")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM documents WHERE workspace=? AND key=?",
                                  (workspace, key)).fetchone()[0]
            if payload.get("expected_revision", 0) != revision:
                raise ApiError(409, "Document changed; read current revision before updating")
            revision += 1
            stamp = now()
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?)", (workspace, key, revision, content, stamp))
        return {"key": key, "revision": revision, "updated_at": stamp}

    def documents(self, workspace, key=None, revision=None):
        identifier(workspace)
        with self.db() as db:
            if key is None:
                return [dict(r) for r in db.execute(
                    "SELECT key,MAX(revision) AS revision,MAX(updated_at) AS updated_at FROM documents WHERE workspace=? GROUP BY key", (workspace,))]
            key = document_key(key)
            sql = "SELECT key,revision,content,updated_at FROM documents WHERE workspace=? AND key=?"
            args = [workspace, key]
            if revision is not None:
                sql += " AND revision=?"
                args.append(int(revision))
            row = db.execute(sql + " ORDER BY revision DESC LIMIT 1", args).fetchone()
        if not row:
            raise ApiError(404, "Document not found")
        return dict(row)

    def save_call(self, workspace, name, args, result, started):
        identifier(workspace)
        call_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?)", (
                workspace, call_id, name, json.dumps(args, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False), started, now()))
        return call_id

    def call(self, workspace, call_id):
        identifier(workspace)
        identifier(call_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM calls WHERE workspace=? AND id=?", (workspace, call_id)).fetchone()
        if not row:
            raise ApiError(404, "Tool record not found")
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["result"] = json.loads(result["result"])
        return result


def socia_tools():
    return json.loads((SKILLS / "sociavault-tools.json").read_text(encoding="utf-8"))


def catalog(list_seller):
    tools = [{"name": "sociavault__" + t["name"], "description": t["description"],
              "inputSchema": t["inputSchema"]} for t in socia_tools()]
    unavailable = []
    try:
        tools.extend({**t, "name": "sellersprite__" + t["name"]}
                     for t in list_seller("sellersprite") if t["name"] in SELLER_TOOLS)
    except Exception:
        unavailable.append("SellerSprite tool schema unavailable")
    return {"tools": tools, "unavailable": unavailable}


def execute(name, args, list_seller, call_seller):
    if not isinstance(args, dict):
        raise ApiError(400, "arguments must be an object")
    available = {t["name"]: t for t in catalog(list_seller)["tools"]}
    if name not in available:
        raise ApiError(403, "Tool is not in the isolated research allowlist")
    schema = available[name]["inputSchema"]
    if set(args) - set(schema.get("properties", {})):
        raise ApiError(400, "Unknown tool argument; use the current schema")
    if set(schema.get("required", [])) - set(args):
        raise ApiError(400, "Missing required tool argument")
    if name.startswith("sellersprite__"):
        # Existing provider adapter handles runtime normalization and credentials.
        result = call_seller(name, args)
        if not result.get("ok"):
            return {"isError": True, "error": "SellerSprite request failed", "provider_result": result.get("data")}
        return result["data"]
    import requests
    from sociavault_usage import update_sociavault_usage_from_response
    key = os.getenv("SOCIAVAULT_API_KEY", "")
    if not key:
        raise ApiError(503, "SociaVault credential is not configured")
    spec = next(t for t in socia_tools() if "sociavault__" + t["name"] == name)
    params = {k: str(v).lower() if isinstance(v, bool) else v for k, v in args.items() if v is not None}
    for k, field in spec["inputSchema"].get("properties", {}).items():
        if k not in params and "default" in field:
            params[k] = field["default"]
    base = os.getenv("SOCIAVAULT_API_BASE", "https://api.sociavault.com").rstrip("/")
    response = requests.get(base + spec["path"], params=params,
                            headers={"X-API-Key": key, "Accept": "application/json"}, timeout=(15, 120))
    try:
        body = response.json()
    except ValueError:
        body = {"isError": True, "error": "Provider returned non-JSON", "status": response.status_code}
    update_sociavault_usage_from_response(response, body)
    if not response.ok:
        return {"isError": True, "error": "SociaVault request failed", "status": response.status_code, "data": body}
    return body


def manifest():
    files = {}
    for name in ("sociavault-market-discovery", "market-feasibility"):
        for path in sorted((SKILLS / name).rglob("*.md")):
            files[path.relative_to(SKILLS).as_posix()] = path.read_text(encoding="utf-8")
    return {"version": "v1", "execution_mode": "external_agent", "frontend": False,
            "skills": files, "runtime": (SKILLS / "runtime.md").read_text(encoding="utf-8")}


def reply(handler, status, data, markdown=False):
    body = data.encode("utf-8") if markdown else json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/markdown; charset=utf-8" if markdown else "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def handle(handler, parsed, list_seller, call_seller, store=None):
    if not parsed.path.startswith(PREFIX):
        return False
    store = store or Store()
    route = parsed.path[len(PREFIX):]
    query = parse_qs(parsed.query)
    try:
        workspace = identifier(query.get("workspace", ["default"])[0])
        payload = {}
        if handler.command == "POST":
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ApiError(413, "JSON body must be between 1 and 2000000 bytes")
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ApiError(400, "Expected JSON object")
            workspace = identifier(payload.get("workspace", workspace))
        if handler.command == "GET" and route == "manifest":
            result = manifest()
        elif handler.command == "GET" and route == "tools":
            result = catalog(list_seller)
        elif handler.command == "POST" and route == "tools/call":
            name, args = payload.get("name"), payload.get("arguments", {})
            if not isinstance(name, str):
                raise ApiError(400, "name is required")
            started = now()
            try:
                result = execute(name, args, list_seller, call_seller)
            except ApiError:
                raise
            except Exception as exc:
                result = {"isError": True, "error": "Provider request failed", "error_type": type(exc).__name__}
            call_id = store.save_call(workspace, name, args, result, started)
            result = {"call_id": call_id, "result": result}
        elif handler.command == "GET" and route.startswith("calls/"):
            result = store.call(workspace, route.removeprefix("calls/"))
        elif handler.command == "GET" and route == "categories":
            result = {"categories": store.search(workspace, query.get("q", [""])[0], query.get("limit", [30])[0])}
        elif handler.command == "GET" and route.startswith("categories/"):
            result = store.category(workspace, route.removeprefix("categories/"), query.get("revision", [None])[0])
        elif handler.command == "POST" and route == "categories":
            result = store.save_category(workspace, payload)
        elif handler.command == "POST" and route == "documents":
            result = store.save_document(workspace, payload)
        elif handler.command == "GET" and route == "documents":
            result = store.documents(workspace, query.get("key", [None])[0], query.get("revision", [None])[0])
            if query.get("format") == ["markdown"] and isinstance(result, dict):
                reply(handler, 200, result["content"], markdown=True)
                return True
        else:
            raise ApiError(404, "Market discovery endpoint not found")
        reply(handler, 200, result)
    except ApiError as exc:
        reply(handler, exc.status, {"error": str(exc)})
    except (ValueError, TypeError) as exc:
        reply(handler, 400, {"error": "Invalid request: " + type(exc).__name__})
    except Exception as exc:
        # Do not expose connection headers, credentials or filesystem paths.
        print("[market-discovery] request failed:", type(exc).__name__, flush=True)
        reply(handler, 502, {"error": "Research backend request failed", "error_type": type(exc).__name__})
    return True
