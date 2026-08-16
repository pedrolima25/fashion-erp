from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import (
    get_db, get_manaus_time, manaus_tz,
    Company, User, Subscription, pwd_context,
)
from whatsapp_service import whatsapp_manager

router = APIRouter()


def _require_master(request: Request):
    return request.session.get("is_master")


class CompanyCreateMaster(BaseModel):
    name: str
    category: Optional[str] = "Loja de Calçados"
    admin_username: str
    admin_password: str


class CompanyUpdateMaster(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    expiry_date: Optional[str] = None


@router.get("/api/master/stats")
def get_master_stats(request: Request, db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    total_lojas = db.query(Company).filter(Company.is_archived == False).count()
    ativas = db.query(Company).filter(Company.active == True, Company.is_archived == False).count()
    return {"total_lojas": total_lojas, "ativas": ativas, "receita_mensal": total_lojas * 99.90}


@router.get("/api/master/companies")
def get_master_companies(request: Request, db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    companies = db.query(Company).filter(Company.is_archived == False).all()
    res = []
    for c in companies:
        sub = db.query(Subscription).filter(Subscription.company_id == c.id).order_by(Subscription.end_date.desc()).first()
        if not sub:
            sub = Subscription(
                company_id=c.id,
                start_date=get_manaus_time(),
                end_date=get_manaus_time() + timedelta(days=30),
                status="active",
            )
            db.add(sub)
            db.commit()
            db.refresh(sub)
        status_wpp = whatsapp_manager.get_status(c.id).get("status", "OFFLINE")
        res.append({
            "id": c.id, "name": c.name, "category": c.category,
            "active": c.active,
            "expiry": sub.end_date.strftime("%d/%m/%Y") if sub.end_date else "N/A",
            "whatsapp_status": status_wpp,
        })
    return res


@router.post("/api/master/companies")
def create_master_company(data: CompanyCreateMaster, request: Request, db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    if db.query(User).filter(User.username == data.admin_username).first():
        return JSONResponse(status_code=400, content={"error": "Usuário já em uso"})
    new_co = Company(name=data.name, category=data.category)
    db.add(new_co)
    db.flush()
    new_user = User(
        username=data.admin_username,
        hashed_password=pwd_context.hash(data.admin_password),
        company_id=new_co.id,
    )
    db.add(new_user)
    db.add(Subscription(
        company_id=new_co.id,
        plan_type="mensal",
        start_date=get_manaus_time(),
        end_date=get_manaus_time() + timedelta(days=30),
        status="active",
    ))
    db.commit()
    return {"success": True, "company_id": new_co.id}


@router.put("/api/master/companies/{cid}")
def update_master_company(cid: int, data: CompanyUpdateMaster, request: Request, db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp:
        return JSONResponse(status_code=404, content={"error": "Empresa não encontrada"})
    if data.name is not None:
        comp.name = data.name
    if data.category is not None:
        comp.category = data.category
    if data.expiry_date:
        sub = db.query(Subscription).filter(Subscription.company_id == cid).order_by(Subscription.end_date.desc()).first()
        new_dt = datetime.strptime(data.expiry_date, "%Y-%m-%d").replace(tzinfo=manaus_tz)
        if not sub:
            sub = Subscription(company_id=cid, start_date=get_manaus_time(), end_date=new_dt, status="active")
            db.add(sub)
        else:
            sub.end_date = new_dt
    db.commit()
    return {"success": True}


@router.post("/api/master/companies/{cid}/extend")
def extend_subscription(cid: int, request: Request, days: int = Form(...), db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    sub = db.query(Subscription).filter(Subscription.company_id == cid).order_by(Subscription.end_date.desc()).first()
    if not sub:
        sub = Subscription(
            company_id=cid, start_date=get_manaus_time(),
            end_date=get_manaus_time() + timedelta(days=days), status="active",
        )
        db.add(sub)
    else:
        base_date = max(sub.end_date.date(), get_manaus_time().date())
        sub.end_date = datetime.combine(base_date + timedelta(days=days), datetime.min.time()).replace(tzinfo=manaus_tz)
        sub.status = "active"
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp:
        comp.active = True
    db.commit()
    return {"success": True}


@router.post("/api/master/companies/{cid}/toggle")
def toggle_company(cid: int, request: Request, db: Session = Depends(get_db)):
    if not _require_master(request):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp:
        return {"error": "Não encontrado"}
    comp.active = not comp.active
    db.commit()
    return {"success": True, "is_active": comp.active}
