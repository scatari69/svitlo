from fastapi import APIRouter, Request, Response

from app.services.health import HealthStatus, check_health

router = APIRouter()


@router.get("/health", response_model=HealthStatus, response_model_exclude_none=True)
async def health(request: Request) -> HealthStatus:
    return await check_health(
        request.app.state.engine,
        request.app.state.redis,
        request.app.state.settings.health_timeout,
    )


@router.get("/readiness", response_model=HealthStatus, response_model_exclude_none=True)
async def readiness(request: Request, response: Response) -> HealthStatus:
    result = await health(request)
    tasks = getattr(request.app.state, "worker_tasks", {})
    if tasks:
        result.workers = {
            name: "unavailable" if task.done() else "ok" for name, task in tasks.items()
        }
        if "unavailable" in result.workers.values():
            result.status = "degraded"
    if not getattr(request.app.state, "accepting_requests", True):
        result.process, result.status = "stopping", "degraded"
    if result.status != "ok":
        response.status_code = 503
    return result
