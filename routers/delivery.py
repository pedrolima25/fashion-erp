from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import case

from database import get_db, get_manaus_time, manaus_tz, Sale, DeliveryDriver
from whatsapp_service import whatsapp_manager

router = APIRouter()

DELIVERY_STATUSES = {"waiting", "assigned", "out_for_delivery", "delivered", "failed"}


class DeliveryDriverSchema(BaseModel):
    name: str
    phone: str
    vehicle: Optional[str] = None


class DeliveryAssignSchema(BaseModel):
    driver_id: int
    send_receipt: bool = True


class DeliveryStatusSchema(BaseModel):
    status: str


def delivery_order_payload(sale: Sale):
    return {
        "id": sale.id,
        "date": sale.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
        "customer": sale.customer.name if sale.customer else "Consumidor",
        "phone": sale.customer.phone if sale.customer else "",
        "total": sale.total_amount,
        "payment_method": sale.payment_method,
        "sale_status": sale.status,
        "delivery_status": sale.delivery_status or "waiting",
        "address": sale.delivery_address or "",
        "reference": sale.delivery_reference or "",
        "location": sale.delivery_location_link or "",
        "fee": sale.delivery_fee or 0.0,
        "driver": {
            "id": sale.delivery_driver.id,
            "name": sale.delivery_driver.name,
            "phone": sale.delivery_driver.phone,
            "vehicle": sale.delivery_driver.vehicle or "",
        } if sale.delivery_driver else None,
        "assigned_at": sale.delivery_assigned_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_assigned_at else "",
        "dispatched_at": sale.delivery_dispatched_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_dispatched_at else "",
        "completed_at": sale.delivery_completed_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_completed_at else "",
        "items": [
            {
                "name": item.product.name if item.product else "Produto",
                "size": item.variation.size if item.variation else "",
                "color": item.variation.color if item.variation and item.variation.color else "",
                "quantity": item.quantity,
                "price": item.unit_price,
            }
            for item in sale.items
        ],
    }


@router.get("/api/delivery/drivers")
def list_delivery_drivers(request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    drivers = (
        db.query(DeliveryDriver)
        .filter(DeliveryDriver.company_id == cid, DeliveryDriver.active == True)
        .order_by(DeliveryDriver.name.asc())
        .all()
    )
    return [{"id": d.id, "name": d.name, "phone": d.phone, "vehicle": d.vehicle or ""} for d in drivers]


@router.post("/api/delivery/drivers")
def create_delivery_driver(data: DeliveryDriverSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    phone = "".join(filter(str.isdigit, data.phone))
    if not data.name.strip() or not phone:
        return JSONResponse(status_code=400, content={"error": "Nome e WhatsApp obrigatorios."})
    existing = db.query(DeliveryDriver).filter(
        DeliveryDriver.company_id == cid, DeliveryDriver.phone == phone, DeliveryDriver.active == True
    ).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Entregador ja cadastrado."})
    driver = DeliveryDriver(
        company_id=cid,
        name=data.name.strip(),
        phone=phone,
        vehicle=(data.vehicle or "").strip() or None,
    )
    db.add(driver)
    db.commit()
    return {"success": True, "id": driver.id}


@router.delete("/api/delivery/drivers/{driver_id}")
def delete_delivery_driver(driver_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    driver = db.query(DeliveryDriver).filter(
        DeliveryDriver.id == driver_id, DeliveryDriver.company_id == cid, DeliveryDriver.active == True
    ).first()
    if not driver:
        return JSONResponse(status_code=404, content={"error": "Entregador nao encontrado."})
    driver.active = False
    db.commit()
    return {"success": True}


@router.get("/api/delivery/orders")
def list_delivery_orders(request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sales = (
        db.query(Sale)
        .filter(Sale.company_id == cid, Sale.delivery_type == "delivery", Sale.status != "cancelled")
        .order_by(
            case((Sale.delivery_status == "delivered", 1), else_=0),
            Sale.date.desc(),
        )
        .limit(80)
        .all()
    )
    return [delivery_order_payload(s) for s in sales]


@router.post("/api/delivery/orders/{sale_id}/assign")
def assign_delivery_driver(sale_id: int, data: DeliveryAssignSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    driver = db.query(DeliveryDriver).filter(
        DeliveryDriver.id == data.driver_id, DeliveryDriver.company_id == cid, DeliveryDriver.active == True
    ).first()
    if not driver:
        return JSONResponse(status_code=404, content={"error": "Entregador nao encontrado."})
    now = get_manaus_time()
    sale.delivery_driver_id = driver.id
    sale.delivery_status = "assigned"
    sale.delivery_assigned_at = now
    db.commit()
    db.refresh(sale)
    sent = False
    if data.send_receipt:
        msg = (
            f"*ENTREGA #{sale.id}*\n\n"
            f"Cliente: {sale.customer.name if sale.customer else 'Consumidor'}\n"
            f"Total: R$ {sale.total_amount:.2f}\n"
            f"Pagamento: {sale.payment_method}\n\n"
            f"*Endereco:*\n{sale.delivery_address or '-'}\n"
            f"Referencia: {sale.delivery_reference or '-'}"
        )
        sent = whatsapp_manager.send_message(cid, driver.phone, msg)
    return {"success": True, "sent": sent, "order": delivery_order_payload(sale)}


@router.post("/api/delivery/orders/{sale_id}/send-driver")
def send_delivery_receipt_to_driver(sale_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    if not sale.delivery_driver:
        return JSONResponse(status_code=400, content={"error": "Atribua um entregador primeiro."})
    msg = (
        f"*ENTREGA #{sale.id}*\n\n"
        f"Cliente: {sale.customer.name if sale.customer else 'Consumidor'}\n"
        f"Total: R$ {sale.total_amount:.2f}\n"
        f"Pagamento: {sale.payment_method}\n\n"
        f"*Endereco:*\n{sale.delivery_address or '-'}\n"
        f"Referencia: {sale.delivery_reference or '-'}"
    )
    sent = whatsapp_manager.send_message(cid, sale.delivery_driver.phone, msg)
    return {"success": True, "sent": sent}


@router.post("/api/delivery/orders/{sale_id}/status")
def update_delivery_status(sale_id: int, data: DeliveryStatusSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    if data.status not in DELIVERY_STATUSES:
        return JSONResponse(status_code=400, content={"error": "Status invalido."})
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale:
        return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    now = get_manaus_time()
    sale.delivery_status = data.status
    if data.status == "assigned" and not sale.delivery_assigned_at:
        sale.delivery_assigned_at = now
    elif data.status == "out_for_delivery":
        sale.delivery_dispatched_at = now
    elif data.status == "delivered":
        sale.delivery_completed_at = now
    db.commit()
    return {"success": True, "order": delivery_order_payload(sale)}
