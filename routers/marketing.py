import asyncio
import json
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, manaus_tz, Product, ScheduledCampaign
from whatsapp_service import whatsapp_manager

router = APIRouter()


class ScheduleCampaignCreate(BaseModel):
    product_ids: List[int]
    group_jids: List[str]
    opening_msg: Optional[str] = ""
    scheduled_at: str
    frequency: str = "once"
    post_to_status: bool = False


@router.get("/api/marketing/products")
def get_marketing_products(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    prods = db.query(Product).filter(Product.company_id == cid, Product.active == True).all()
    return [
        {"id": p.id, "name": p.name, "price": p.base_price, "description": p.description, "image": p.image_base64}
        for p in prods
    ]


@router.post("/api/marketing/schedule")
def schedule_campaign(data: ScheduleCampaignCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    try:
        dt = datetime.fromisoformat(data.scheduled_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=manaus_tz)
        new_camp = ScheduledCampaign(
            company_id=cid,
            product_ids=json.dumps(data.product_ids or []),
            group_jids=json.dumps(data.group_jids or []),
            opening_msg=data.opening_msg,
            scheduled_at=dt,
            frequency=data.frequency,
            post_to_status=data.post_to_status,
            status="pending",
        )
        db.add(new_camp)
        db.commit()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/marketing/scheduled")
def list_scheduled(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    camps = (
        db.query(ScheduledCampaign)
        .filter(ScheduledCampaign.company_id == cid, ScheduledCampaign.status == "pending")
        .order_by(ScheduledCampaign.scheduled_at.asc())
        .all()
    )
    res = []
    for c in camps:
        pids = json.loads(c.product_ids)
        opening = c.opening_msg[:30] + "..." if c.opening_msg and len(c.opening_msg) > 30 else (c.opening_msg or "N/A")
        res.append({
            "id": c.id,
            "scheduled_at": c.scheduled_at.strftime("%d/%m/%Y %H:%M"),
            "frequency": "Diário" if c.frequency == "daily" else "Uma vez",
            "groups_count": len(json.loads(c.group_jids)),
            "products_count": len(pids),
            "opening": opening,
        })
    return res


@router.delete("/api/marketing/scheduled/{camp_id}")
def delete_scheduled(camp_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    camp = db.query(ScheduledCampaign).filter(
        ScheduledCampaign.id == camp_id, ScheduledCampaign.company_id == cid
    ).first()
    if not camp:
        return JSONResponse(status_code=404, content={"error": "Não encontrado"})
    db.delete(camp)
    db.commit()
    return {"success": True}
