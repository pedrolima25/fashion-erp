from typing import Optional
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import case

from database import (
    get_db, get_manaus_time, manaus_tz,
    Sale, SaleItem, Product, ProductVariation, Customer, Transaction, LoyaltyConfig, AuditLog, Coupon,
)
from whatsapp_service import whatsapp_manager

logger = logging.getLogger(__name__)
router = APIRouter()


class SaleItemSchema(BaseModel):
    product_id: int
    variation_id: Optional[int] = None
    quantity: int
    price: float


class PaymentEntry(BaseModel):
    method: str
    amount: float


class SaleCreate(BaseModel):
    items: list[SaleItemSchema]
    payments: list[PaymentEntry]
    discount: float = 0.0
    delivery_type: Optional[str] = "pickup"
    delivery_fee: Optional[float] = 0.0
    delivery_address: Optional[str] = None
    delivery_reference: Optional[str] = None
    delivery_location_link: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_type: Optional[str] = "varejo"
    seller_id: Optional[int] = None
    notes: Optional[str] = None
    coupon_code: Optional[str] = None


@router.post("/api/sales")
def finalize_sale(data: SaleCreate, request: Request, db: Session = Depends(get_db)):
    try:
        cid = request.session.get("company_id")
        if not cid:
            return JSONResponse(status_code=401, content={"error": "Sessão expirada"})

        sale_customer_type = "atacado" if data.customer_type == "atacado" else "varejo"

        customer_id = None
        if data.customer_phone:
            customer = db.query(Customer).filter(Customer.phone == data.customer_phone, Customer.company_id == cid).first()
            if not customer:
                customer = Customer(name=data.customer_name or "Cliente", phone=data.customer_phone, company_id=cid, customer_type=sale_customer_type)
                db.add(customer)
                db.flush()
            customer_id = customer.id
        elif data.customer_name:
            customer = Customer(name=data.customer_name, company_id=cid, customer_type=sale_customer_type)
            db.add(customer)
            db.flush()
            customer_id = customer.id

        subtotal = sum(item.price * item.quantity for item in data.items)
        total = subtotal - data.discount + (data.delivery_fee or 0)
        payment_summary = ", ".join([p.method for p in data.payments])

        coupon_code = data.coupon_code.strip().upper() if data.coupon_code else None
        if coupon_code:
            coupon = db.query(Coupon).filter(
                Coupon.company_id == cid, Coupon.code == coupon_code, Coupon.active == True
            ).first()
            if coupon:
                coupon.current_uses += 1

        new_sale = Sale(
            company_id=cid,
            customer_id=customer_id,
            seller_id=data.seller_id,
            total_amount=total,
            discount=data.discount,
            payment_method=payment_summary,
            delivery_type=data.delivery_type,
            delivery_fee=data.delivery_fee,
            delivery_address=data.delivery_address,
            delivery_reference=data.delivery_reference,
            delivery_location_link=data.delivery_location_link,
            status="paid",
            notes=data.notes or None,
            coupon_code=coupon_code,
        )
        db.add(new_sale)
        db.flush()

        for item in data.items:
            item_cost = 0.0
            if item.variation_id:
                var = db.query(ProductVariation).filter(ProductVariation.id == item.variation_id).first()
                if var:
                    item_cost = var.cost_price_override if var.cost_price_override is not None else var.product.cost_price
                    var.stock_quantity -= item.quantity
            else:
                prod = db.query(Product).filter(Product.id == item.product_id).first()
                if prod:
                    item_cost = prod.cost_price
            db.add(SaleItem(
                sale_id=new_sale.id,
                product_id=item.product_id,
                variation_id=item.variation_id,
                quantity=item.quantity,
                unit_price=item.price,
                cost_price=item_cost,
            ))

        for p in data.payments:
            if p.amount > 0:
                db.add(Transaction(
                    company_id=cid,
                    sale_id=new_sale.id,
                    amount=p.amount,
                    type="receita",
                    category="Venda PDV",
                    payment_method=p.method,
                    description=f"Pgto Venda #{new_sale.id} ({p.method})",
                    date=get_manaus_time(),
                ))

        loyalty_earned = 0
        if customer_id:
            loyalty = db.query(LoyaltyConfig).filter(
                LoyaltyConfig.company_id == cid, LoyaltyConfig.active == True
            ).first()
            if loyalty:
                loyalty_earned = int(total * loyalty.points_per_real)
                customer = db.query(Customer).filter(Customer.id == customer_id).first()
                if customer and loyalty_earned > 0:
                    customer.loyalty_points = (customer.loyalty_points or 0) + loyalty_earned
                    customer.total_points_earned = (customer.total_points_earned or 0) + loyalty_earned

        db.commit()
        return {"success": True, "sale_id": new_sale.id, "loyalty_earned": loyalty_earned}
    except Exception as e:
        db.rollback()
        logger.error(f"Erro no finalize_sale: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/api/sales/customer-lookup")
def customer_lookup_by_phone(request: Request, db: Session = Depends(get_db), phone: str = ""):
    cid = request.session.get("company_id")
    phone = phone.strip()
    if not phone:
        return JSONResponse(status_code=400, content={"error": "Telefone obrigatório"})
    customer = db.query(Customer).filter(Customer.phone == phone, Customer.company_id == cid).first()
    if not customer:
        return JSONResponse(status_code=404, content={"error": "Cliente não encontrado"})
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "loyalty_points": customer.loyalty_points or 0,
        "customer_type": customer.customer_type or "varejo",
    }


@router.get("/api/sales/{sale_id}")
def get_sale_detail(sale_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid).first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Venda não encontrada"})
    items_list = [
        {
            "nome": item.product.name,
            "tamanho": item.variation.size if item.variation else "",
            "cor": item.variation.color if item.variation else "",
            "qtd": item.quantity,
            "preco": item.unit_price,
            "total": item.quantity * item.unit_price,
        }
        for item in sale.items
    ]
    return {
        "id": sale.id,
        "date": sale.date.strftime("%d/%m/%Y %H:%M"),
        "total_amount": sale.total_amount,
        "discount": sale.discount,
        "delivery_fee": sale.delivery_fee,
        "payment_method": sale.payment_method,
        "delivery_type": sale.delivery_type,
        "status": sale.status,
        "notes": sale.notes,
        "coupon_code": sale.coupon_code,
        "customer": {"name": sale.customer.name, "phone": sale.customer.phone} if sale.customer else None,
        "items": items_list,
    }


@router.post("/api/sales/{sale_id}/confirm")
def confirm_sale(sale_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        raw_cid = request.session.get("company_id")
        if raw_cid is None:
            return JSONResponse(status_code=401, content={"error": "Sessão expirada. Faça login novamente."})
        cid = int(raw_cid)
        sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid).first()
        if not sale:
            return JSONResponse(status_code=404, content={"error": f"Venda {sale_id} não encontrada"})
        if sale.status != "pending":
            return {"success": True, "message": "Venda já processada"}

        for item in sale.items:
            if item.variation_id:
                var = db.query(ProductVariation).filter(ProductVariation.id == item.variation_id).first()
                if var:
                    var.stock_quantity -= item.quantity

        db.add(Transaction(
            company_id=cid,
            type="income",
            amount=sale.total_amount,
            description=f"Pedido #{sale.id} Confirmado",
            payment_method=sale.payment_method or "PIX",
        ))

        sale.status = "completed"

        if sale.customer_id:
            loyalty = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid, LoyaltyConfig.active == True).first()
            if loyalty:
                pontos = int(sale.total_amount * loyalty.points_per_real)
                customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
                if customer and pontos > 0:
                    customer.loyalty_points = (customer.loyalty_points or 0) + pontos
                    customer.total_points_earned = (customer.total_points_earned or 0) + pontos

        db.commit()

        try:
            if sale.customer and sale.customer.phone:
                from database import Company
                company = db.query(Company).filter(Company.id == cid).first()
                loja_nome = company.name.upper() if company else "LOJA"
                items_text = "".join(
                    f"  • {i.quantity}x {i.product.name if i.product else 'Produto'}"
                    f"{' (' + i.variation.size + ')' if i.variation else ''}"
                    f" - R$ {i.unit_price * i.quantity:.2f}\n"
                    for i in sale.items
                )
                msg = (
                    f"✅ *COMPROVANTE DE COMPRA*\n\n🏪 *{loja_nome}*\n"
                    f"📋 Pedido #{sale.id}\n"
                    f"📅 {sale.date.astimezone(manaus_tz).strftime('%d/%m/%Y %H:%M')}\n\n"
                    f"*Itens:*\n{items_text}\n"
                )
                if sale.discount and sale.discount > 0:
                    msg += f"💸 Desconto: -R$ {sale.discount:.2f}\n"
                if sale.delivery_fee and sale.delivery_fee > 0:
                    msg += f"🛵 Frete: R$ {sale.delivery_fee:.2f}\n"
                msg += f"\n💰 *TOTAL: R$ {sale.total_amount:.2f}*\n💳 Pagamento: {sale.payment_method}\n\nObrigado pela compra! 🎉"
                whatsapp_manager.send_message(cid, sale.customer.phone, msg)
        except Exception as wp_err:
            logger.warning(f"⚠️ Erro ao enviar comprovante WhatsApp: {wp_err}")

        return {"success": True}
    except Exception as e:
        db.rollback()
        logger.error(f"ERRO AO CONFIRMAR VENDA: {e}")
        return JSONResponse(status_code=500, content={"error": f"Erro interno: {str(e)}"})


@router.post("/api/sales/{sale_id}/cancel")
def cancel_sale(sale_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid).first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Venda não encontrada"})
    db.add(AuditLog(
        company_id=cid, username=request.session.get("user", ""),
        action="cancel_sale", entity="sale", entity_id=sale_id,
        detail=f'{{"total": {sale.total_amount}, "status_anterior": "{sale.status}"}}',
        ip=request.client.host if request.client else None,
    ))
    sale.status = "cancelled"
    db.commit()
    return {"success": True}
