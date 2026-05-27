import json
from typing import Optional, List
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import (
    get_db, Seller, Supplier, Exchange, Sale,
    ProductVariation, StockTransaction, Transaction,
)

router = APIRouter()


class SellerCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    commission_rate: float = 0.0


class SupplierCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    cnpj: Optional[str] = None
    notes: Optional[str] = None


class ExchangeCreate(BaseModel):
    sale_id: int
    items: List[dict]
    type: str = "exchange"
    reason: str = ""
    refund_amount: float = 0.0


# --- SELLERS ---

@router.get("/api/sellers")
def list_sellers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sellers = db.query(Seller).filter(Seller.company_id == cid, Seller.active == True).all()
    return [{"id": s.id, "name": s.name, "phone": s.phone, "commission_rate": s.commission_rate} for s in sellers]


@router.post("/api/sellers")
def create_seller(data: SellerCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    seller = Seller(company_id=cid, name=data.name, phone=data.phone, commission_rate=data.commission_rate)
    db.add(seller)
    db.commit()
    return {"success": True, "id": seller.id}


@router.delete("/api/sellers/{seller_id}")
def delete_seller(seller_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    seller = db.query(Seller).filter(Seller.id == seller_id, Seller.company_id == cid).first()
    if not seller:
        return JSONResponse(status_code=404, content={"error": "Vendedor nao encontrado."})
    seller.active = False
    db.commit()
    return {"success": True}


# --- SUPPLIERS ---

@router.get("/api/suppliers")
def list_suppliers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    suppliers = db.query(Supplier).filter(Supplier.company_id == cid, Supplier.active == True).all()
    return [
        {"id": sup.id, "name": sup.name, "phone": sup.phone, "email": sup.email, "cnpj": sup.cnpj}
        for sup in suppliers
    ]


@router.post("/api/suppliers")
def create_supplier(data: SupplierCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    supplier = Supplier(
        company_id=cid, name=data.name, phone=data.phone,
        email=data.email, cnpj=data.cnpj, notes=data.notes,
    )
    db.add(supplier)
    db.commit()
    return {"success": True, "id": supplier.id}


# --- EXCHANGES ---

@router.post("/api/exchanges")
def register_exchange(data: ExchangeCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sale = db.query(Sale).filter(Sale.id == data.sale_id, Sale.company_id == cid).first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Venda nao encontrada."})
    for item in data.items:
        var = db.query(ProductVariation).filter(ProductVariation.id == item["variation_id"]).first()
        if var:
            var.stock_quantity += item["qty"]
            db.add(StockTransaction(
                variation_id=var.id, type="entry", quantity=item["qty"],
                description=f"Devolucao/Troca ref. Venda #{sale.id}",
            ))
    exchange = Exchange(
        company_id=cid, sale_id=data.sale_id,
        items_json=json.dumps(data.items), type=data.type,
        reason=data.reason, refund_amount=data.refund_amount,
    )
    db.add(exchange)
    if data.type == "refund" and data.refund_amount > 0:
        db.add(Transaction(
            company_id=cid, type="expense", amount=data.refund_amount,
            description=f"Reembolso Venda #{sale.id}", payment_method="OUTRO",
        ))
    db.commit()
    return {"success": True}
