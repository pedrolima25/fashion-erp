import csv
import io
from typing import Optional
from fastapi import APIRouter, Depends, Request, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import json as _json
from database import get_db, Category, Product, ProductVariation, AuditLog

router = APIRouter()


class CategoryCreate(BaseModel):
    name: str


class VariationSchema(BaseModel):
    size: str
    color: Optional[str] = None
    stock: int = 0
    price: Optional[float] = None
    cost: Optional[float] = None


class ProductCreate(BaseModel):
    name: str
    base_price: float
    cost_price: float = 0.0
    description: Optional[str] = None
    category_id: Optional[int] = None
    image_base64: Optional[str] = None
    show_on_whatsapp: bool = True
    variations: list[VariationSchema]


@router.get("/api/categories")
def get_categories(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    return db.query(Category).filter(Category.company_id == cid).all()


@router.post("/api/categories")
def create_category(data: CategoryCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    new_cat = Category(company_id=cid, name=data.name)
    db.add(new_cat)
    db.commit()
    return {"success": True, "id": new_cat.id}


@router.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    cat = db.query(Category).filter(Category.id == cat_id, Category.company_id == cid).first()
    if not cat:
        return JSONResponse(status_code=404, content={"error": "Categoria não encontrada"})
    db.delete(cat)
    db.commit()
    return {"success": True}


@router.get("/api/products")
def get_products(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 200,
    offset: int = 0,
    include_images: bool = True,
):
    cid = request.session.get("company_id")
    prods = (
        db.query(Product)
        .filter(Product.company_id == cid, Product.active == True)
        .offset(offset)
        .limit(limit)
        .all()
    )
    res = []
    for p in prods:
        vars_list = [
            {
                "id": v.id,
                "size": v.size,
                "color": v.color or "",
                "stock": v.stock_quantity,
                "price": v.price_override or p.base_price,
                "cost": v.cost_price_override or p.cost_price,
            }
            for v in p.variations
        ]
        res.append({
            "id": p.id,
            "name": p.name,
            "category": p.category.name if p.category else "Sem Categoria",
            "price": p.base_price,
            "cost": p.cost_price,
            "show_on_whatsapp": p.show_on_whatsapp,
            "image": p.image_base64 if include_images else None,
            "variations": vars_list,
        })
    return res


@router.get("/api/products/{product_id}/image")
def get_product_image(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Retorna apenas a imagem base64 de um produto — use este endpoint para evitar sobrecarregar a lista."""
    cid = request.session.get("company_id")
    product = db.query(Product).filter(Product.id == product_id, Product.company_id == cid).first()
    if not product:
        return JSONResponse(status_code=404, content={"error": "Produto não encontrado"})
    return {"id": product_id, "image": product.image_base64}


@router.post("/api/products")
def create_product(data: ProductCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    new_p = Product(
        company_id=cid,
        name=data.name,
        base_price=data.base_price,
        cost_price=data.cost_price,
        description=data.description,
        category_id=data.category_id,
        image_base64=data.image_base64,
        show_on_whatsapp=data.show_on_whatsapp,
    )
    db.add(new_p)
    db.flush()
    for v in data.variations:
        db.add(ProductVariation(
            product_id=new_p.id,
            size=v.size,
            color=v.color,
            stock_quantity=v.stock,
            price_override=v.price,
            cost_price_override=v.cost,
        ))
    db.commit()
    return {"success": True}


@router.put("/api/products/{product_id}")
def update_product(product_id: int, data: ProductCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    product = db.query(Product).filter(Product.id == product_id, Product.company_id == cid).first()
    if not product:
        return JSONResponse(status_code=404, content={"error": "Produto não encontrado"})
    product.name = data.name
    product.base_price = data.base_price
    product.cost_price = data.cost_price
    product.description = data.description
    product.category_id = data.category_id
    product.show_on_whatsapp = data.show_on_whatsapp
    if data.image_base64:
        product.image_base64 = data.image_base64
    db.query(ProductVariation).filter(ProductVariation.product_id == product_id).delete()
    for v in data.variations:
        db.add(ProductVariation(
            product_id=product.id,
            size=v.size,
            color=v.color,
            stock_quantity=v.stock,
            price_override=v.price,
            cost_price_override=v.cost,
        ))
    db.commit()
    return {"success": True}


@router.delete("/api/products/{product_id}")
def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    product = db.query(Product).filter(Product.id == product_id, Product.company_id == cid).first()
    if not product:
        return JSONResponse(status_code=404, content={"error": "Produto não encontrado"})
    db.add(AuditLog(
        company_id=cid, username=request.session.get("user", ""),
        action="delete_product", entity="product", entity_id=product_id,
        detail=_json.dumps({"name": product.name, "price": product.base_price}),
        ip=request.client.host if request.client else None,
    ))
    product.active = False
    db.commit()
    return {"success": True}


@router.post("/api/products/import-csv")
async def import_products_csv(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    cid = int(cid)
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    created = 0
    errors = []
    products_cache = {}
    for i, row in enumerate(reader, start=2):
        try:
            name = (row.get("nome") or row.get("name") or "").strip()
            price = float((row.get("preco_venda") or row.get("preco") or row.get("price") or "0").replace(",", "."))
            cost = float((row.get("preco_custo") or row.get("custo") or row.get("cost") or "0").replace(",", "."))
            size = (row.get("tamanho") or row.get("size") or "U").strip()
            color = (row.get("cor") or row.get("color") or "").strip()
            stock = int(float((row.get("estoque") or row.get("stock") or "0").replace(",", ".")))
            if not name:
                errors.append(f"Linha {i}: nome vazio")
                continue
            if name not in products_cache:
                p = db.query(Product).filter(Product.company_id == cid, Product.name == name).first()
                if not p:
                    p = Product(company_id=cid, name=name, base_price=price, cost_price=cost, active=True)
                    db.add(p)
                    db.flush()
                products_cache[name] = p
            prod = products_cache[name]
            existing_var = db.query(ProductVariation).filter(
                ProductVariation.product_id == prod.id,
                ProductVariation.size == size,
                ProductVariation.color == (color or None),
            ).first()
            if existing_var:
                existing_var.stock_quantity += stock
            else:
                db.add(ProductVariation(product_id=prod.id, size=size, color=color or None, stock_quantity=stock))
            created += 1
        except Exception as e:
            errors.append(f"Linha {i}: {str(e)}")
    db.commit()
    return {"success": True, "created": created, "errors": errors}


@router.get("/api/products/labels")
def generate_labels(product_id: int, qty: int = 1, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        return HTMLResponse("Produto não encontrado", status_code=404)
    label_html = f"""
    <div style="width:4cm;height:2.5cm;border:1px solid #ccc;padding:5px;margin:5px;float:left;font-family:sans-serif;text-align:center;">
        <div style="font-size:10px;font-weight:bold;margin-bottom:3px;">{product.company.name if product.company else 'FASHION ERP'}</div>
        <div style="font-size:12px;margin-bottom:5px;">{product.name}</div>
        <div style="font-size:16px;font-weight:800;color:#000;">R$ {product.base_price:.2f}</div>
        <div style="font-size:8px;margin-top:5px;">ID: {product.id} | FASHION ERP</div>
    </div>"""
    full_html = f"<html><body><div style='display:flex;flex-wrap:wrap;'>{''.join([label_html for _ in range(qty)])}</div><script>window.print()</script></body></html>"
    return HTMLResponse(full_html)
