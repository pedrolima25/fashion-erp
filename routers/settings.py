from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import (
    get_db, get_manaus_time, manaus_tz,
    Company, Neighborhood, Coupon, LoyaltyConfig, Customer, User, pwd_context, AuditLog,
)

router = APIRouter()


class SettingsUpdate(BaseModel):
    name: str
    category: Optional[str] = None
    whatsapp_number: Optional[str] = None
    pix_key: Optional[str] = None
    address: Optional[str] = None
    location_link: Optional[str] = None
    delivery_fee: float = 0.0
    delivery_mode: str = "fixed"
    logo_base64: Optional[str] = None
    show_retail_price: bool = True
    show_wholesale_price: bool = True


class NeighborhoodSchema(BaseModel):
    id: Optional[int] = None
    name: str
    fee: float


class CouponCreate(BaseModel):
    code: str
    discount_type: str = "percent"
    discount_value: float = 10.0
    max_uses: int = 0
    valid_until: Optional[str] = None


class LoyaltyConfigCreate(BaseModel):
    points_per_real: float = 1.0
    redemption_threshold: int = 100
    redemption_value: float = 10.0
    active: bool = True


class UserCreate(BaseModel):
    username: str
    password: str
    role: str


# --- CONFIG ---

@router.get("/api/config")
def get_settings(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp:
        return {"error": "Empresa não encontrada"}
    return {
        "name": comp.name,
        "category": comp.category or "",
        "whatsapp_number": comp.whatsapp_number,
        "pix_key": comp.pix_key,
        "address": comp.address,
        "location_link": comp.location_link,
        "delivery_fee": comp.delivery_fee,
        "delivery_mode": comp.delivery_mode or "fixed",
        "slug": comp.slug or "",
        "logo_base64": comp.logo_base64,
        "show_retail_price": comp.show_retail_price if comp.show_retail_price is not None else True,
        "show_wholesale_price": comp.show_wholesale_price if comp.show_wholesale_price is not None else True,
    }


@router.post("/api/config/save")
def save_settings(data: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp:
        return {"error": "Não encontrado"}
    comp.name = data.name
    if data.category:
        comp.category = data.category
    comp.whatsapp_number = data.whatsapp_number
    comp.pix_key = data.pix_key
    comp.address = data.address
    comp.location_link = data.location_link
    comp.delivery_fee = data.delivery_fee
    comp.delivery_mode = data.delivery_mode
    comp.show_retail_price = data.show_retail_price
    comp.show_wholesale_price = data.show_wholesale_price
    if data.logo_base64:
        comp.logo_base64 = data.logo_base64
    db.commit()
    return {"success": True}


@router.post("/api/config/slug")
def save_company_slug(request: Request, db: Session = Depends(get_db), slug: str = ""):
    cid = request.session.get("company_id")
    slug = slug.strip().lower().replace(" ", "-")
    if not slug:
        return JSONResponse(status_code=400, content={"error": "Slug obrigatorio."})
    existing = db.query(Company).filter(Company.slug == slug, Company.id != cid).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Este slug ja esta em uso."})
    company = db.query(Company).filter(Company.id == cid).first()
    company.slug = slug
    db.commit()
    return {"success": True, "url": f"/catalogo/{slug}"}


# --- NEIGHBORHOODS ---

@router.get("/api/neighborhoods")
def list_neighborhoods(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    return db.query(Neighborhood).filter(Neighborhood.company_id == cid).all()


@router.post("/api/neighborhoods")
def save_neighborhood(data: NeighborhoodSchema, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if data.id:
        item = db.query(Neighborhood).filter(Neighborhood.id == data.id, Neighborhood.company_id == cid).first()
        if item:
            item.name = data.name
            item.fee = data.fee
    else:
        item = Neighborhood(company_id=cid, name=data.name, fee=data.fee)
        db.add(item)
    db.commit()
    return {"success": True}


@router.delete("/api/neighborhoods/{nid}")
def delete_neighborhood(nid: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    item = db.query(Neighborhood).filter(Neighborhood.id == nid, Neighborhood.company_id == cid).first()
    if item:
        db.delete(item)
        db.commit()
    return {"success": True}


# --- COUPONS ---

@router.get("/api/coupons")
def list_coupons(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    coupons = (
        db.query(Coupon)
        .filter(Coupon.company_id == cid, Coupon.active == True)
        .order_by(Coupon.created_at.desc())
        .all()
    )
    return [
        {
            "id": c.id, "code": c.code,
            "discount_type": c.discount_type, "discount_value": c.discount_value,
            "max_uses": c.max_uses, "current_uses": c.current_uses,
            "valid_until": c.valid_until.astimezone(manaus_tz).strftime("%d/%m/%Y") if c.valid_until else None,
        }
        for c in coupons
    ]


@router.post("/api/coupons")
def create_coupon(data: CouponCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    code = data.code.strip().upper()
    if not code:
        return JSONResponse(status_code=400, content={"error": "Codigo obrigatorio."})
    existing = db.query(Coupon).filter(Coupon.company_id == cid, Coupon.code == code, Coupon.active == True).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Cupom ja existe."})
    valid_dt = None
    if data.valid_until:
        try:
            valid_dt = datetime.fromisoformat(data.valid_until).replace(tzinfo=manaus_tz)
        except Exception:
            pass
    coupon = Coupon(
        company_id=cid, code=code, discount_type=data.discount_type,
        discount_value=data.discount_value, max_uses=data.max_uses, valid_until=valid_dt,
    )
    db.add(coupon)
    db.commit()
    return {"success": True, "id": coupon.id}


@router.delete("/api/coupons/{coupon_id}")
def delete_coupon(coupon_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.company_id == cid).first()
    if not coupon:
        return JSONResponse(status_code=404, content={"error": "Cupom nao encontrado."})
    coupon.active = False
    db.commit()
    return {"success": True}


@router.post("/api/coupons/validate")
def validate_coupon(request: Request, db: Session = Depends(get_db), code: str = ""):
    cid = request.session.get("company_id")
    code = code.strip().upper()
    coupon = db.query(Coupon).filter(Coupon.company_id == cid, Coupon.code == code, Coupon.active == True).first()
    if not coupon:
        return JSONResponse(status_code=404, content={"error": "Cupom invalido."})
    if coupon.max_uses > 0 and coupon.current_uses >= coupon.max_uses:
        return JSONResponse(status_code=400, content={"error": "Cupom esgotado."})
    if coupon.valid_until and get_manaus_time() > coupon.valid_until:
        return JSONResponse(status_code=400, content={"error": "Cupom expirado."})
    return {
        "valid": True, "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value, "code": coupon.code,
    }


# --- LOYALTY ---

@router.get("/api/loyalty/config")
def get_loyalty_config(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid).first()
    if not config:
        return {"active": False, "points_per_real": 1.0, "redemption_threshold": 100, "redemption_value": 10.0}
    return {
        "active": config.active, "points_per_real": config.points_per_real,
        "redemption_threshold": config.redemption_threshold, "redemption_value": config.redemption_value,
    }


@router.post("/api/loyalty/config")
def save_loyalty_config(data: LoyaltyConfigCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid).first()
    if not config:
        config = LoyaltyConfig(company_id=cid)
        db.add(config)
    config.points_per_real = data.points_per_real
    config.redemption_threshold = data.redemption_threshold
    config.redemption_value = data.redemption_value
    config.active = data.active
    db.commit()
    return {"success": True}


@router.post("/api/loyalty/redeem")
def redeem_loyalty_points(request: Request, db: Session = Depends(get_db), customer_id: int = 0):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid, LoyaltyConfig.active == True).first()
    if not config:
        return JSONResponse(status_code=400, content={"error": "Programa de fidelidade nao ativo."})
    customer = db.query(Customer).filter(Customer.id == customer_id, Customer.company_id == cid).first()
    if not customer:
        return JSONResponse(status_code=404, content={"error": "Cliente nao encontrado."})
    if (customer.loyalty_points or 0) < config.redemption_threshold:
        return JSONResponse(
            status_code=400,
            content={"error": f"Pontos insuficientes. Necessario: {config.redemption_threshold}, Atual: {customer.loyalty_points or 0}"},
        )
    customer.loyalty_points -= config.redemption_threshold
    db.commit()
    return {"success": True, "discount": config.redemption_value, "remaining_points": customer.loyalty_points}


# --- USERS ---

@router.get("/api/users")
def get_company_users(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    users = db.query(User).filter(User.company_id == cid).all()
    return [{"id": u.id, "username": u.username, "role": u.role} for u in users]


@router.post("/api/users")
def create_company_user(request: Request, data: UserCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    existing = db.query(User).filter(User.username == data.username.strip().lower()).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Usuário já existe."})
    new_user = User(
        company_id=cid,
        username=data.username.strip().lower(),
        hashed_password=pwd_context.hash(data.password),
        role=data.role,
    )
    db.add(new_user)
    db.commit()
    return {"success": True}


@router.delete("/api/users/{user_id}")
def delete_company_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    user = db.query(User).filter(User.id == user_id, User.company_id == cid).first()
    if user:
        db.add(AuditLog(
            company_id=cid, username=request.session.get("user", ""),
            action="delete_user", entity="user", entity_id=user_id,
            detail=f'{{"username": "{user.username}"}}',
            ip=request.client.host if request.client else None,
        ))
        db.delete(user)
        db.commit()
    return {"success": True}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/users/change-password")
def change_password(data: PasswordChange, request: Request, db: Session = Depends(get_db)):
    username = request.session.get("user")
    if not username:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(data.current_password, user.hashed_password):
        return JSONResponse(status_code=400, content={"error": "Senha atual incorreta."})
    if len(data.new_password) < 6:
        return JSONResponse(status_code=400, content={"error": "Nova senha deve ter ao menos 6 caracteres."})
    user.hashed_password = pwd_context.hash(data.new_password)
    db.commit()
    return {"success": True}


# --- AUDIT LOG ---

@router.get("/api/audit-log")
def get_audit_log(request: Request, db: Session = Depends(get_db), limit: int = 100, offset: int = 0):
    cid = request.session.get("company_id")
    if request.session.get("role") != "admin" and not request.session.get("is_master"):
        return JSONResponse(status_code=403, content={"error": "Acesso restrito a administradores."})
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == cid)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": lg.id,
            "data": lg.created_at.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
            "usuario": lg.username,
            "acao": lg.action,
            "entidade": lg.entity,
            "entidade_id": lg.entity_id,
            "detalhe": lg.detail,
            "ip": lg.ip,
        }
        for lg in logs
    ]
