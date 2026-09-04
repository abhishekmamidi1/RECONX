from fastapi import APIRouter

from app.api.v1 import (
    analytics,
    audit,
    exceptions,
    health,
    ingestions,
    integrations,
    matches,
    policy,
    reconciliation,
    reports,
    review,
    sources,
    transactions,
    uploads,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(health.router)
api_router.include_router(sources.router)
api_router.include_router(uploads.router)
api_router.include_router(transactions.router)
api_router.include_router(ingestions.router)
api_router.include_router(reconciliation.router)
api_router.include_router(matches.router)
api_router.include_router(exceptions.router)
api_router.include_router(review.router)
api_router.include_router(audit.router)
api_router.include_router(policy.router)
api_router.include_router(reports.router)
api_router.include_router(integrations.router)
api_router.include_router(analytics.router)
