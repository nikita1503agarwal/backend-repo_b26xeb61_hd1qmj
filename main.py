import os
from typing import List, Optional, Literal, Tuple
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timezone

# Optional Supabase client (installed via requirements)
try:
    from supabase import create_client, Client
except Exception:  # pragma: no cover
    create_client = None
    Client = None  # type: ignore

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = FastAPI(title="YouKnowMe API (Supabase-backed)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# -----------------------------------------------------------------------------
# Models (mirror planned schema; persisted in Supabase Postgres in production)
# -----------------------------------------------------------------------------
class IdentifyPayload(BaseModel):
    tenant_id: str = Field(..., description="Tenant identifier")
    user_id: str = Field(..., description="Stable user identifier")
    traits: dict = Field(default_factory=dict)
    timestamp: Optional[str] = None  # ISO8601

class EventPayload(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    anonymous_id: Optional[str] = None
    event: str
    properties: dict = Field(default_factory=dict)
    timestamp: Optional[str] = None

class RecommendationRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    anonymous_id: Optional[str] = None
    count: int = 10
    strategy: Optional[Literal["popular","recent","personalized"]] = "personalized"
    include_features: bool = False

class FeatureQuery(BaseModel):
    tenant_id: str
    item_ids: Optional[List[str]] = None
    limit: int = 50

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
API_DASHBOARD_KEY = os.getenv("API_DASHBOARD_KEY")  # optional admin key for dashboard calls

_supabase_client: Optional[Client] = None


def supabase_configured() -> bool:
    return bool(SUPABASE_URL and (SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY) and create_client)


def get_supabase_client() -> Optional[Client]:
    global _supabase_client
    if not supabase_configured():
        return None
    if _supabase_client is None:
        key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY  # prefer service role for server-side ops
        _supabase_client = create_client(SUPABASE_URL, key)  # type: ignore[arg-type]
    return _supabase_client


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_api_key(x_api_key: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Verify API key.
    Returns (ok, tenant_id_from_key). If API_DASHBOARD_KEY matches, returns (True, None)
    If Supabase is configured and api_keys table exists, try to map key->tenant_id.
    """
    if not x_api_key:
        return False, None
    # Admin override
    if API_DASHBOARD_KEY and x_api_key == API_DASHBOARD_KEY:
        return True, None
    # Try Supabase lookup
    sb = get_supabase_client()
    if sb is None:
        # No lookup possible; accept any non-empty key for MVP as previously
        return True, None
    try:
        res = sb.table("api_keys").select("tenant_id, active").eq("api_key", x_api_key).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            if row.get("active", True):
                return True, row.get("tenant_id")
    except Exception:
        # If table missing or error, fall back to permissive behavior for MVP
        return True, None
    return False, None


# -----------------------------------------------------------------------------
# Health and root
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "name": "YouKnowMe API",
        "status": "ok",
        "supabase": {
            "url": "set" if SUPABASE_URL else "not_set",
            "anon_key": "set" if SUPABASE_ANON_KEY else "not_set",
            "service_role_key": "set" if SUPABASE_SERVICE_ROLE_KEY else "not_set",
        }
    }


@app.get("/health")
def health():
    return {"status": "ok", "supabase_configured": supabase_configured()}


# -----------------------------------------------------------------------------
# Core endpoints (MVP)
# -----------------------------------------------------------------------------
@app.post("/identify")
async def identify(payload: IdentifyPayload, request: Request, x_api_key: Optional[str] = Header(default=None)):
    ok, key_tenant = verify_api_key(x_api_key)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if not supabase_configured():
        return {"status": "stub", "message": "Supabase not configured", "received": payload.model_dump()}

    # Enforce tenant match if key maps to tenant
    if key_tenant and key_tenant != payload.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch for API key")

    sb = get_supabase_client()
    assert sb is not None

    try:
        data = {
            "tenant_id": payload.tenant_id,
            "user_id": payload.user_id,
            "traits": payload.traits or {},
            "updated_at": now_iso(),
        }
        # Upsert on (tenant_id, user_id)
        res = sb.table("users").upsert(data, on_conflict="tenant_id,user_id").execute()
        return {"status": "ok", "result": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error (identify): {e}")


@app.post("/events")
async def events(payload: EventPayload, request: Request, x_api_key: Optional[str] = Header(default=None)):
    ok, key_tenant = verify_api_key(x_api_key)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if not supabase_configured():
        return {"status": "stub", "message": "Supabase not configured", "received": payload.model_dump()}

    if key_tenant and key_tenant != payload.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch for API key")

    sb = get_supabase_client()
    assert sb is not None

    try:
        data = {
            "tenant_id": payload.tenant_id,
            "user_id": payload.user_id,
            "anonymous_id": payload.anonymous_id,
            "event": payload.event,
            "properties": payload.properties or {},
            "timestamp": payload.timestamp or now_iso(),
            "ingested_at": now_iso(),
        }
        res = sb.table("user_activities").insert(data).execute()
        return {"status": "ok", "result": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error (events): {e}")


@app.get("/features")
async def features(tenant_id: str, item_ids: Optional[str] = None, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    ok, key_tenant = verify_api_key(x_api_key)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if not supabase_configured():
        items = item_ids.split(",") if item_ids else ["demo-item-1", "demo-item-2"]
        return {
            "status": "stub",
            "message": "Supabase not configured",
            "items": [{"item_id": i, "features": {"category": "demo", "score": 0.5}} for i in items][:limit]
        }

    if key_tenant and key_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch for API key")

    sb = get_supabase_client()
    assert sb is not None

    try:
        query = sb.table("features").select("item_id, features").eq("tenant_id", tenant_id).limit(limit)
        if item_ids:
            ids_list = [i.strip() for i in item_ids.split(",") if i.strip()]
            if ids_list:
                query = query.in_("item_id", ids_list)
        res = query.execute()
        # Normalize output
        items = [{"item_id": row.get("item_id"), "features": row.get("features", {})} for row in (res.data or [])]
        return {"status": "ok", "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error (features): {e}")


@app.post("/recommendations")
async def recommendations(payload: RecommendationRequest, x_api_key: Optional[str] = Header(default=None)):
    ok, key_tenant = verify_api_key(x_api_key)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if not supabase_configured():
        demo = [
            {"item_id": f"demo-{i}", "score": 1.0 - i * 0.05, "reason": payload.strategy}
            for i in range(max(1, min(payload.count, 20)))
        ]
        if payload.include_features:
            for d in demo:
                d["features"] = {"category": "demo", "popularity": 0.8}
        return {"status": "stub", "message": "Supabase not configured", "items": demo}

    if key_tenant and key_tenant != payload.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch for API key")

    sb = get_supabase_client()
    assert sb is not None

    try:
        # Simple MVP strategies
        if payload.strategy == "recent":
            # Join user_activities -> items by item_id if exists; fallback to selecting latest items
            res = sb.rpc("get_recent_items", {"p_tenant_id": payload.tenant_id, "p_limit": payload.count}).execute()
            rows = res.data or []
        elif payload.strategy == "popular":
            res = sb.rpc("get_popular_items", {"p_tenant_id": payload.tenant_id, "p_limit": payload.count}).execute()
            rows = res.data or []
        else:
            # personalized baseline: fallback to popular for now
            res = sb.rpc("get_popular_items", {"p_tenant_id": payload.tenant_id, "p_limit": payload.count}).execute()
            rows = res.data or []

        items = []
        for r in rows:
            item = {"item_id": r.get("item_id"), "score": r.get("score", 0.0)}
            if payload.include_features:
                item["features"] = r.get("features")
            items.append(item)
        return {"status": "ok", "items": items}
    except Exception as e:
        # If RPCs not present, do a simple table-based fallback
        try:
            q = sb.table("items").select("item_id, features, popularity").eq("tenant_id", payload.tenant_id).order("popularity", desc=True).limit(payload.count)
            res = q.execute()
            items = []
            for r in (res.data or []):
                item = {"item_id": r.get("item_id"), "score": r.get("popularity", 0.0)}
                if payload.include_features:
                    item["features"] = r.get("features")
                items.append(item)
            return {"status": "ok", "items": items, "note": "fallback without RPC", "error": str(e)}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Supabase error (recommendations): {e2}")


# -----------------------------------------------------------------------------
# Backward-compat simple hello + test endpoints
# -----------------------------------------------------------------------------
@app.get("/api/hello")
async def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
async def test_info():
    return {
        "backend": "✅ Running",
        "supabase": {
            "url": "✅ Set" if SUPABASE_URL else "❌ Not Set",
            "anon_key": "✅ Set" if SUPABASE_ANON_KEY else "❌ Not Set",
            "service_role_key": "✅ Set" if SUPABASE_SERVICE_ROLE_KEY else "❌ Not Set",
            "client_loaded": bool(create_client) and bool(get_supabase_client())
        },
        "note": "Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to enable full persistence."
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
