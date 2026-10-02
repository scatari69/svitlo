from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError

from app.models.enums import PowerState
from app.services.homeassistant import WebhookNotFound, WebhookRateLimited

router = APIRouter(prefix="/api/v1/homeassistant", tags=["Home Assistant"])


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["on", "off"]


@router.post("/webhook/{token}", status_code=204)
async def webhook(token: str, payload: WebhookPayload, request: Request) -> None:
    try:
        await request.app.state.homeassistant_webhooks.receive(token, PowerState(payload.state))
    except WebhookNotFound:
        raise HTTPException(status_code=404, detail="Webhook not found") from None
    except WebhookRateLimited:
        raise HTTPException(
            status_code=429, detail="Too many requests", headers={"Retry-After": "60"}
        ) from None
    except (ConnectionError, TimeoutError, RedisError):
        raise HTTPException(status_code=503, detail="Service unavailable") from None
