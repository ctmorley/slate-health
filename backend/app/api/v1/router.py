"""API v1 router — aggregates all sub-routers into a single API prefix."""

from fastapi import APIRouter

from app.api.v1.agents import router as agents_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.payers import router as payers_router
from app.api.v1.reviews import router as reviews_router
from app.api.v1.workflows import router as workflows_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth_router)
api_router.include_router(agents_router)
api_router.include_router(reviews_router)
api_router.include_router(workflows_router)
api_router.include_router(payers_router)
api_router.include_router(audit_router)
api_router.include_router(dashboard_router)
