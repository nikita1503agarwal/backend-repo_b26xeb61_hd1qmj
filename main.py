import os
from typing import List, Optional, Literal
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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


def require_api_key(x_api_key: Optional[str]):
    """Basic API key guard. In production, verify against Supabase api_keys table or a KMS secret.
    For now, allow if any non-empty key is provided OR a configured API_DASHBOARD_KEY matches.
    """
    if API_DASHBOARD_KEY:
        if x_api_key == API_DASHBOARD_KEY:
            return True
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")
    return True


def supabase_configured() -> bool:
    return bool(SUPABASE_URL and (SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY))


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
# Core endpoints (MVP). These are wired for Supabase but operate in stub mode
# until environment variables are provided.
# -----------------------------------------------------------------------------
@app.post("/identify")
async def identify(payload: IdentifyPayload, request: Request, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    if not supabase_configured():
        # In stub mode, echo back
        return {"status": "stub", "message": "Supabase not configured", "received": payload.dict()}

    # TODO: Implement Supabase insert into users (and optionally traits JSONB) via REST or client
    return {"status": "ok", "message": "Identify accepted", "received": payload.dict()}


@app.post("/events")
async def events(payload: EventPayload, request: Request, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    if not supabase_configured():
        return {"status": "stub", "message": "Supabase not configured", "received": payload.dict()}

    # TODO: Insert into user_activities (event stream)
    return {"status": "ok", "message": "Event accepted", "received": payload.dict()}


@app.get("/features")
async def features(tenant_id: str, item_ids: Optional[str] = None, limit: int = 50, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    if not supabase_configured():
        # Return placeholder features
        items = item_ids.split(",") if item_ids else ["demo-item-1", "demo-item-2"]
        return {
            "status": "stub",
            "message": "Supabase not configured",
            "items": [{"item_id": i, "features": {"category": "demo", "score": 0.5}} for i in items][:limit]
        }

    # TODO: Query features table filtered by tenant_id and item_ids
    return {"status": "ok", "items": []}


@app.post("/recommendations")
async def recommendations(payload: RecommendationRequest, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    if not supabase_configured():
        # Simple fallback: popular + recent demo
        demo = [
            {"item_id": f"demo-{i}", "score": 1.0 - i * 0.05, "reason": payload.strategy}
            for i in range(max(1, min(payload.count, 20)))
        ]
        if payload.include_features:
            for d in demo:
                d["features"] = {"category": "demo", "popularity": 0.8}
        return {"status": "stub", "message": "Supabase not configured", "items": demo}

    # TODO: Fetch recent/popular/personalized using Supabase (SQL or materialized views)
    return {"status": "ok", "items": []}


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
        },
        "note": "Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to enable persistence."
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
