from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import get_db, Company, Product

router = APIRouter()


@router.get("/catalogo/{slug}", response_class=HTMLResponse)
def public_catalog(slug: str, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.slug == slug, Company.active == True).first()
    if not company:
        return HTMLResponse("<h1>Loja não encontrada</h1>", status_code=404)
    products = db.query(Product).filter(
        Product.company_id == company.id, Product.active == True, Product.show_on_whatsapp == True
    ).all()

    prod_list = []
    for p in products:
        variations = [
            {"size": v.size, "color": v.color, "stock": v.stock_quantity, "price": v.price_override or p.base_price}
            for v in p.variations if v.stock_quantity > 0
        ]
        if variations:
            prod_list.append({
                "name": p.name, "description": p.description or "",
                "price": p.base_price, "image": p.image_base64, "variations": variations,
            })

    wa_num = company.whatsapp_number or ""
    wa_link = f"https://wa.me/{wa_num}" if wa_num else "#"

    prods_html = ""
    for p in prod_list:
        img = (
            f'<img src="{p["image"]}" style="width:100%;height:220px;object-fit:cover;border-radius:12px 12px 0 0;">'
            if p["image"]
            else '<div style="height:220px;background:#1e293b;border-radius:12px 12px 0 0;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:3rem;">👗</div>'
        )
        sizes = ", ".join(set(v["size"] for v in p["variations"]))
        msg_text = f"Oi! Tenho interesse no produto *{p['name']}*"
        prods_html += f"""
        <div style="background:#1e293b;border-radius:12px;overflow:hidden;border:1px solid #334155;">
            {img}
            <div style="padding:1rem;">
                <div style="font-weight:700;font-size:1rem;margin-bottom:0.3rem;">{p["name"]}</div>
                <div style="font-size:0.8rem;color:#94a3b8;margin-bottom:0.5rem;">{p["description"][:80]}</div>
                <div style="font-size:0.75rem;color:#64748b;margin-bottom:0.75rem;">Tamanhos: {sizes}</div>
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-size:1.3rem;font-weight:800;color:#14b8a6;">R$ {p["price"]:.2f}</span>
                    <a href="{wa_link}?text={msg_text}" target="_blank" style="background:#25d366;color:white;padding:0.5rem 1rem;border-radius:8px;text-decoration:none;font-weight:700;font-size:0.85rem;">💬 Comprar</a>
                </div>
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
    <html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{company.name} - Catálogo</title>
    <style>
        *{{margin:0;padding:0;box-sizing:border-box;}}
        body{{font-family:'Segoe UI',Arial,sans-serif;background:#0f172a;color:white;}}
        .header{{background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:1.5rem;text-align:center;}}
        .header h1{{font-size:1.5rem;font-weight:800;}}
        .header p{{font-size:0.85rem;opacity:0.8;margin-top:0.3rem;}}
        .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem;padding:1rem;max-width:1000px;margin:auto;}}
        .footer{{text-align:center;padding:2rem;color:#64748b;font-size:0.75rem;}}
    </style></head><body>
    <div class="header">
        <h1>{company.name}</h1>
        <p>Catálogo de Produtos</p>
    </div>
    <div class="grid">{prods_html}</div>
    <div class="footer">Powered by Fashion ERP Pro</div>
    </body></html>"""
    return HTMLResponse(html)
