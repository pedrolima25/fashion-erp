import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import (
    get_db, get_manaus_time, Company, Product, ProductVariation, Customer, Sale, SaleItem,
)
from whatsapp_service import whatsapp_manager

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _clean_phone(phone: str) -> str:
    return "".join(filter(str.isdigit, phone or ""))


def _get_active_company(db: Session, slug: str):
    return db.query(Company).filter(Company.slug == slug, Company.active == True).first()


@router.get("/catalogo/{slug}", response_class=HTMLResponse)
def public_catalog(slug: str, request: Request, db: Session = Depends(get_db)):
    company = _get_active_company(db, slug)
    if not company:
        return HTMLResponse("<h1>Loja não encontrada</h1>", status_code=404)

    products = (
        db.query(Product)
        .filter(Product.company_id == company.id, Product.active == True, Product.show_on_whatsapp == True)
        .all()
    )

    prod_list = []
    for p in products:
        variations = [
            {
                "id": v.id,
                "size": v.size,
                "color": v.color or "",
                "stock": v.stock_quantity,
                "price": float(v.price_override or p.base_price),
                "wholesale": float(v.wholesale_price_override or p.wholesale_price or 0),
            }
            for v in p.variations if v.stock_quantity > 0
        ]
        if not variations:
            continue
        prod_list.append({
            "id": p.id,
            "name": p.name,
            "brand": p.brand or "",
            "description": p.description or "",
            "price": float(p.base_price),
            "wholesale_price": float(p.wholesale_price or 0),
            "image": p.image_base64,
            "category": p.category.name if p.category else "Outros",
            "variations": variations,
        })

    categories = sorted({p["category"] for p in prod_list})

    return templates.TemplateResponse(request, "catalogo_publico.html", {
        "request": request,
        "company": company,
        "products_json": json.dumps(prod_list, ensure_ascii=False),
        "categories": categories,
        "show_retail_price": company.show_retail_price if company.show_retail_price is not None else True,
        "show_wholesale_price": company.show_wholesale_price if company.show_wholesale_price is not None else True,
    })


@router.get("/api/catalogo/{slug}/customer-lookup")
def catalog_customer_lookup(slug: str, phone: str, db: Session = Depends(get_db)):
    company = _get_active_company(db, slug)
    if not company:
        return JSONResponse(status_code=404, content={"error": "Loja não encontrada"})
    phone_clean = _clean_phone(phone)
    if not phone_clean:
        return JSONResponse(status_code=400, content={"error": "Telefone inválido"})
    customer = db.query(Customer).filter(Customer.company_id == company.id, Customer.phone == phone_clean).first()
    if not customer:
        return {"found": False, "name": None, "customer_type": "varejo"}
    return {"found": True, "name": customer.name, "customer_type": customer.customer_type or "varejo"}


class CatalogOrderItem(BaseModel):
    product_id: int
    variation_id: Optional[int] = None
    quantity: int
    price: float


class CatalogOrderCreate(BaseModel):
    items: list[CatalogOrderItem]
    customer_name: str
    customer_phone: str
    customer_type: Optional[str] = "varejo"
    delivery_type: Optional[str] = "pickup"
    delivery_address: Optional[str] = None
    notes: Optional[str] = None


@router.post("/api/catalogo/{slug}/order")
def create_catalog_order(slug: str, data: CatalogOrderCreate, db: Session = Depends(get_db)):
    company = _get_active_company(db, slug)
    if not company:
        return JSONResponse(status_code=404, content={"error": "Loja não encontrada"})
    if not data.items:
        return JSONResponse(status_code=400, content={"error": "Carrinho vazio"})

    phone_clean = _clean_phone(data.customer_phone)
    name_clean = (data.customer_name or "").strip()
    if not phone_clean or not name_clean:
        return JSONResponse(status_code=400, content={"error": "Nome e telefone são obrigatórios"})

    customer_type = "atacado" if data.customer_type == "atacado" else "varejo"

    customer = db.query(Customer).filter(Customer.company_id == company.id, Customer.phone == phone_clean).first()
    if not customer:
        customer = Customer(company_id=company.id, name=name_clean, phone=phone_clean, customer_type=customer_type)
        db.add(customer)
        db.flush()
    elif name_clean and customer.name != name_clean and (customer.name or "").strip().lower() in ("", "cliente"):
        customer.name = name_clean

    subtotal = sum(item.price * item.quantity for item in data.items)
    is_delivery = data.delivery_type == "delivery"

    new_sale = Sale(
        company_id=company.id,
        customer_id=customer.id,
        total_amount=subtotal,
        delivery_type=data.delivery_type,
        delivery_address=data.delivery_address if is_delivery else None,
        delivery_status="waiting" if is_delivery else None,
        payment_method="A combinar",
        status="pending",
        notes=data.notes or None,
        date=get_manaus_time(),
    )
    db.add(new_sale)
    db.flush()

    order_lines = []
    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id, Product.company_id == company.id).first()
        if not product:
            continue
        variation = None
        if item.variation_id:
            variation = db.query(ProductVariation).filter(
                ProductVariation.id == item.variation_id, ProductVariation.product_id == product.id
            ).first()
        cost_price = product.cost_price
        if variation and variation.cost_price_override is not None:
            cost_price = variation.cost_price_override
        db.add(SaleItem(
            sale_id=new_sale.id,
            product_id=item.product_id,
            variation_id=item.variation_id,
            quantity=item.quantity,
            unit_price=item.price,
            cost_price=cost_price,
        ))
        size_txt = f" ({variation.size})" if variation else ""
        order_lines.append(f"• {item.quantity}x {product.name}{size_txt} — R$ {item.price * item.quantity:.2f}")

    db.commit()

    try:
        if company.whatsapp_number:
            tipo_txt = "🏷️ ATACADO" if customer_type == "atacado" else "🛍️ Varejo"
            msg = (
                f"🛒 *NOVO PEDIDO PELO CATÁLOGO*\n\n"
                f"👤 {customer.name} ({tipo_txt})\n📱 {phone_clean}\n\n"
                + "\n".join(order_lines)
                + f"\n\n💰 *Total: R$ {subtotal:.2f}*\n"
                f"🚚 {'Entrega' if is_delivery else 'Retirada na loja'}"
            )
            if is_delivery and data.delivery_address:
                msg += f"\n📍 {data.delivery_address}"
            if data.notes:
                msg += f"\n📝 {data.notes}"
            msg += f"\n\nPedido #{new_sale.id} — confirme no Painel."
            whatsapp_manager.send_message(company.id, company.whatsapp_number, msg)
    except Exception as e:
        logger.warning(f"⚠️ Falha ao notificar loja sobre pedido do catálogo: {e}")

    return {"success": True, "order_id": new_sale.id}
