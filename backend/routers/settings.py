from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..db.database import AsyncSessionLocal
from ..db.models import UserSetting
from ..db.persistence import save_user_setting

router = APIRouter(tags=["settings"])

MAX_SETTINGS_BYTES = 32_000


@router.get("/api/settings")
async def get_settings(key: str = "dashboard") -> JSONResponse:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSetting).where(UserSetting.key == key))
        setting = result.scalar_one_or_none()

    if not setting:
        return JSONResponse({"ok": True, "key": key, "settings": {}, "updated_at": None})

    return JSONResponse({
        "ok": True,
        "key": setting.key,
        "settings": setting.payload,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    })


@router.put("/api/settings")
async def put_settings(request: Request, key: str = "dashboard") -> JSONResponse:
    payload = await request.json()
    settings = payload.get("settings", payload) if isinstance(payload, dict) else {}
    if not isinstance(settings, dict):
        return JSONResponse({"ok": False, "message": "settings must be an object"}, status_code=400)

    # Keep this table for preferences only, not large logs or credentials.
    if len(str(settings).encode("utf-8")) > MAX_SETTINGS_BYTES:
        return JSONResponse({"ok": False, "message": "settings payload is too large"}, status_code=413)

    await save_user_setting(key, settings)
    return JSONResponse({"ok": True, "key": key, "settings": settings})
