from typing import Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, ProductVariation, StockTransaction

router = APIRouter()


class AdjustmentCreate(BaseModel):
    variation_id: int
    type: str  # entry, exit, adjustment
    quantity: int
    description: Optional[str] = None


@router.post("/api/inventory/adjust")
def adjust_inventory(data: AdjustmentCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    var = db.query(ProductVariation).filter(ProductVariation.id == data.variation_id).first()
    if not var:
        return {"error": "Variação não encontrada"}
    if var.product.company_id != cid:
        return {"error": "Acesso negado"}
    qty_change = data.quantity if data.type != "exit" else -abs(data.quantity)
    var.stock_quantity += qty_change
    db.add(StockTransaction(variation_id=var.id, type=data.type, quantity=qty_change, description=data.description))
    db.commit()
    return {"success": True}
