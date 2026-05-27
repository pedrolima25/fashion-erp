import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from whatsapp_service import whatsapp_manager

router = APIRouter()


class BroadcastRequest(BaseModel):
    jids: list[str]
    message: str
    image_base64: Optional[str] = None


@router.get("/api/whatsapp/status")
def whatsapp_status(request: Request):
    company_id = request.session.get("company_id")
    if not company_id:
        return {"status": "DISCONNECTED", "qr_code": None}
    return whatsapp_manager.get_status(company_id)


@router.post("/api/whatsapp/start")
def whatsapp_start(request: Request, force: bool = False):
    company_id = request.session.get("company_id")
    if not company_id:
        return {"error": "Unauthorized"}
    whatsapp_manager.start_session(company_id, force_new=force)
    return {"success": True}


@router.post("/api/whatsapp/clean")
def whatsapp_clean(request: Request):
    company_id = request.session.get("company_id")
    if not company_id:
        return {"error": "Unauthorized"}
    whatsapp_manager.clean_session(company_id)
    return {"success": True}


@router.get("/api/whatsapp/groups")
async def get_whatsapp_groups(request: Request, refresh: bool = False):
    cid = request.session.get("company_id")
    if not cid:
        return []
    try:
        return await asyncio.to_thread(whatsapp_manager.get_groups, cid, force_refresh=refresh)
    except Exception:
        return []


@router.post("/api/whatsapp/broadcast")
async def whatsapp_broadcast(data: BroadcastRequest, request: Request):
    cid = request.session.get("company_id")
    if not cid:
        return {"error": "Unauthorized"}
    success_count = 0
    for jid in data.jids:
        if data.image_base64:
            res = await asyncio.to_thread(whatsapp_manager.send_image, cid, jid, data.image_base64, data.message)
        else:
            res = await asyncio.to_thread(whatsapp_manager.send_message, cid, jid, data.message)
        if res:
            success_count += 1
        await asyncio.sleep(0.5)
    return {"success": True, "sent": success_count}
