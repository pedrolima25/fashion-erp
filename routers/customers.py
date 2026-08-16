import logging
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db, get_manaus_time, manaus_tz, Customer, Sale

logger = logging.getLogger(__name__)

router = APIRouter()


class CustomerCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    birthday: Optional[str] = None
    customer_type: Optional[str] = "varejo"


class CustomerUpdate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    birthday: Optional[str] = None
    customer_type: Optional[str] = "varejo"


def _clean_customer_type(value):
    return "atacado" if value == "atacado" else "varejo"


def _fmt_date(dt, fmt="%d/%m/%Y"):
    if dt is None:
        return "—"
    try:
        if dt.tzinfo is None:
            dt = manaus_tz.localize(dt)
        return dt.astimezone(manaus_tz).strftime(fmt)
    except Exception:
        return "—"


@router.get("/api/customers")
def get_customers(request: Request, db: Session = Depends(get_db), limit: int = 200, offset: int = 0):
    try:
        cid = request.session.get("company_id")
        if not cid:
            return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
        cid = int(cid)

        customers = (
            db.query(Customer)
            .filter(Customer.company_id == cid)
            .order_by(Customer.name)
            .offset(offset)
            .limit(limit)
            .all()
        )

        totals = {}
        try:
            rows = (
                db.query(
                    Sale.customer_id,
                    func.sum(Sale.total_amount).label("total"),
                    func.max(Sale.date).label("last"),
                )
                .filter(
                    Sale.company_id == cid,
                    Sale.customer_id.isnot(None),
                    Sale.status != "cancelled",
                )
                .group_by(Sale.customer_id)
                .all()
            )
            for row in rows:
                totals[row.customer_id] = {
                    "total": float(row.total or 0),
                    "last": row.last,
                }
        except Exception as e:
            logger.error(f"Erro ao calcular totais de clientes: {e}")

        result = []
        for c in customers:
            t = totals.get(c.id, {})
            result.append({
                "id": c.id,
                "name": c.name or "",
                "phone": c.phone or "",
                "email": c.email or "",
                "birthday": c.birthday or "",
                "loyalty_points": int(c.loyalty_points or 0),
                "total_purchased": float(t.get("total") or 0),
                "last_purchase": _fmt_date(t.get("last")),
                "customer_type": c.customer_type or "varejo",
            })
        return result
    except Exception as e:
        logger.error(f"Erro em get_customers: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/api/customers")
def create_customer(data: CustomerCreate, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    phone_clean = "".join(filter(str.isdigit, data.phone)) if data.phone else ""
    if phone_clean:
        existing = db.query(Customer).filter(Customer.company_id == cid, Customer.phone == phone_clean).first()
        if existing:
            return JSONResponse(status_code=400, content={"error": "Cliente já cadastrado com esse telefone."})
    customer = Customer(
        company_id=cid,
        name=data.name.strip(),
        phone=phone_clean or None,
        email=data.email.strip() if data.email else None,
        birthday=data.birthday or None,
        customer_type=_clean_customer_type(data.customer_type),
    )
    db.add(customer)
    db.commit()
    return {"success": True, "id": customer.id}


@router.put("/api/customers/{customer_id}")
def update_customer(customer_id: int, data: CustomerUpdate, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        return JSONResponse(status_code=404, content={"error": "Cliente não encontrado."})
    if customer.company_id and customer.company_id != cid:
        return JSONResponse(status_code=403, content={"error": "Sem permissão."})
    phone_clean = "".join(filter(str.isdigit, data.phone)) if data.phone else ""
    if phone_clean and phone_clean != customer.phone:
        existing = db.query(Customer).filter(
            Customer.company_id == cid, Customer.phone == phone_clean, Customer.id != customer_id
        ).first()
        if existing:
            return JSONResponse(status_code=400, content={"error": "Telefone já usado por outro cliente."})
    customer.name = data.name.strip()
    customer.phone = phone_clean or customer.phone
    customer.email = data.email.strip() if data.email else None
    customer.birthday = data.birthday or None
    customer.customer_type = _clean_customer_type(data.customer_type)
    db.commit()
    return {"success": True}


@router.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    raw_cid = request.session.get("company_id")
    if not raw_cid:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    cid = int(raw_cid)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        return JSONResponse(status_code=404, content={"error": "Cliente não encontrado."})
    if customer.company_id and customer.company_id != cid:
        return JSONResponse(status_code=403, content={"error": "Sem permissão."})
    # Desvincula vendas para não violar FK
    db.query(Sale).filter(Sale.customer_id == customer_id).update(
        {"customer_id": None}, synchronize_session=False
    )
    db.delete(customer)
    db.commit()
    return {"success": True}


@router.get("/api/customers/segments/inactive")
def get_inactive_customers(request: Request, db: Session = Depends(get_db), days: int = 30):
    cid = request.session.get("company_id")
    if not cid:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    cid = int(cid)
    cutoff = get_manaus_time() - timedelta(days=days)
    last_sale = (
        db.query(Sale.customer_id, func.max(Sale.date).label("last"))
        .filter(Sale.company_id == cid, Sale.status != "cancelled", Sale.customer_id.isnot(None))
        .group_by(Sale.customer_id)
        .subquery()
    )
    customers = (
        db.query(Customer)
        .join(last_sale, last_sale.c.customer_id == Customer.id)
        .filter(Customer.company_id == cid, last_sale.c.last < cutoff)
        .order_by(last_sale.c.last)
        .all()
    )
    result = []
    for c in customers:
        last = db.query(func.max(Sale.date)).filter(Sale.customer_id == c.id).scalar()
        result.append({
            "id": c.id, "name": c.name or "", "phone": c.phone or "",
            "last_purchase": _fmt_date(last),
            "loyalty_points": int(c.loyalty_points or 0),
        })
    return result


@router.get("/api/customers/segments/birthdays")
def get_birthday_customers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    cid = int(cid)
    now = get_manaus_time()
    month_str = f"-{now.month:02d}-"
    customers = (
        db.query(Customer)
        .filter(Customer.company_id == cid, Customer.birthday.isnot(None), Customer.birthday.contains(month_str))
        .order_by(Customer.name)
        .all()
    )
    return [
        {"id": c.id, "name": c.name or "", "phone": c.phone or "", "birthday": c.birthday or ""}
        for c in customers
    ]


@router.get("/api/customers/{customer_id}/history")
def get_customer_history(customer_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sales = (
        db.query(Sale)
        .filter(Sale.customer_id == customer_id, Sale.company_id == cid)
        .order_by(Sale.date.desc())
        .all()
    )
    return [
        {
            "id": s.id,
            "date": _fmt_date(s.date, "%d/%m/%Y %H:%M"),
            "total": s.total_amount,
            "status": s.status,
            "payment": s.payment_method or "",
        }
        for s in sales
    ]
