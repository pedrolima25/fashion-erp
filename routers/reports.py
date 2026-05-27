import csv
import io
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func

from database import (
    get_db, get_manaus_time, manaus_tz,
    Sale, SaleItem, Product, ProductVariation, Seller, Expense,
)

router = APIRouter()


def _parse_date_range(start_date: Optional[str], end_date: Optional[str]):
    dt_start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    dt_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)) if end_date else None
    return dt_start, dt_end


@router.get("/api/reports/summary")
def get_reports_summary(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    query = (
        db.query(Sale)
        .options(selectinload(Sale.customer))
        .filter(Sale.company_id == cid, Sale.status != "cancelled")
    )
    dt_start, dt_end = _parse_date_range(start_date, end_date)
    if dt_start:
        query = query.filter(Sale.date >= dt_start)
    if dt_end:
        query = query.filter(Sale.date < dt_end)
    sales = query.all()
    total_revenue = sum(s.total_amount for s in sales)
    total_discount = sum(s.discount for s in sales)
    total_freight = sum(s.delivery_fee or 0 for s in sales)
    pay_map = {}
    for s in sales:
        m = s.payment_method or "Outro"
        pay_map[m] = pay_map.get(m, 0) + s.total_amount
    payment_methods = [{"metodo": k, "total": v} for k, v in sorted(pay_map.items(), key=lambda x: x[1], reverse=True)]
    daily_map = {}
    for s in sales:
        dia = s.date.astimezone(manaus_tz).strftime("%d/%m")
        daily_map[dia] = daily_map.get(dia, 0) + s.total_amount
    return {
        "total_orders": len(sales),
        "total_revenue": total_revenue,
        "ticket_medio": total_revenue / len(sales) if sales else 0,
        "total_discount": total_discount,
        "total_freight": total_freight,
        "payment_methods": payment_methods,
        "delivery_count": sum(1 for s in sales if s.delivery_type == "delivery"),
        "pickup_count": sum(1 for s in sales if s.delivery_type != "delivery"),
        "daily": [{"dia": k, "total": v} for k, v in daily_map.items()],
    }


@router.get("/api/reports/sales")
def get_sales_report(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    query = db.query(Sale).options(selectinload(Sale.customer)).filter(Sale.company_id == cid)
    dt_start, dt_end = _parse_date_range(start_date, end_date)
    if dt_start:
        query = query.filter(Sale.date >= dt_start)
    if dt_end:
        query = query.filter(Sale.date < dt_end)
    sales = query.order_by(Sale.date.desc()).all()
    return [
        {
            "id": s.id,
            "data": s.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
            "cliente": s.customer.name if s.customer else "Consumidor",
            "total": s.total_amount,
            "metodo": s.payment_method,
            "logistica": s.delivery_type or "pickup",
            "status": s.status,
        }
        for s in sales
    ]


@router.get("/api/reports/sales/csv")
def export_sales_csv(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    query = db.query(Sale).filter(Sale.company_id == cid)
    dt_start, dt_end = _parse_date_range(start_date, end_date)
    if dt_start:
        query = query.filter(Sale.date >= dt_start)
    if dt_end:
        query = query.filter(Sale.date < dt_end)
    sales = query.order_by(Sale.date.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Data", "Cliente", "Total", "Pagamento", "Logistica", "Status"])
    for s in sales:
        writer.writerow([
            s.id,
            s.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
            s.customer.name if s.customer else "Consumidor",
            f"{s.total_amount:.2f}",
            s.payment_method or "",
            s.delivery_type or "pickup",
            s.status,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vendas.csv"},
    )


@router.get("/api/reports/products")
def get_products_report(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    query = (
        db.query(SaleItem)
        .options(selectinload(SaleItem.product), selectinload(SaleItem.variation))
        .join(Sale)
        .filter(Sale.company_id == cid, Sale.status != "cancelled")
    )
    dt_start, dt_end = _parse_date_range(start_date, end_date)
    if dt_start:
        query = query.filter(Sale.date >= dt_start)
    if dt_end:
        query = query.filter(Sale.date < dt_end)
    items = query.all()
    product_map = {}
    for item in items:
        key = f"{item.product_id}_{item.variation_id or 0}"
        if key not in product_map:
            product_map[key] = {
                "nome": item.product.name if item.product else "Produto",
                "tamanho": item.variation.size if item.variation else "-",
                "qtd": 0, "receita": 0, "custo": 0,
            }
        product_map[key]["qtd"] += item.quantity
        product_map[key]["receita"] += item.unit_price * item.quantity
        product_map[key]["custo"] += (item.cost_price or 0) * item.quantity
    result = []
    for p in product_map.values():
        lucro = p["receita"] - p["custo"]
        margem = (lucro / p["receita"] * 100) if p["receita"] > 0 else 0
        result.append({**p, "lucro": lucro, "margem": margem})
    return sorted(result, key=lambda x: x["receita"], reverse=True)


@router.get("/api/reports/products/csv")
def export_products_csv(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    data = get_products_report(request, start_date, end_date, db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Produto", "Tamanho", "Qtd Vendida", "Receita", "Custo", "Lucro", "Margem %"])
    for p in data:
        writer.writerow([
            p["nome"], p["tamanho"], p["qtd"],
            f"{p['receita']:.2f}", f"{p['custo']:.2f}",
            f"{p['lucro']:.2f}", f"{p['margem']:.1f}",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=produtos.csv"},
    )


@router.get("/api/reports/customers")
def get_customers_report(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    query = (
        db.query(Sale)
        .options(selectinload(Sale.customer))
        .filter(Sale.company_id == cid, Sale.status != "cancelled", Sale.customer_id.isnot(None))
    )
    dt_start, dt_end = _parse_date_range(start_date, end_date)
    if dt_start:
        query = query.filter(Sale.date >= dt_start)
    if dt_end:
        query = query.filter(Sale.date < dt_end)
    sales = query.all()
    cust_map = {}
    for s in sales:
        cid_c = s.customer_id
        if cid_c not in cust_map:
            cust_map[cid_c] = {
                "nome": s.customer.name if s.customer else "?",
                "telefone": s.customer.phone if s.customer else "-",
                "pedidos": 0, "total": 0, "ultima_data": s.date,
            }
        cust_map[cid_c]["pedidos"] += 1
        cust_map[cid_c]["total"] += s.total_amount
        if s.date > cust_map[cid_c]["ultima_data"]:
            cust_map[cid_c]["ultima_data"] = s.date
    result = []
    for c in cust_map.values():
        c["ticket_medio"] = c["total"] / c["pedidos"] if c["pedidos"] else 0
        c["ultima_compra"] = c["ultima_data"].astimezone(manaus_tz).strftime("%d/%m/%Y")
        del c["ultima_data"]
        result.append(c)
    return sorted(result, key=lambda x: x["total"], reverse=True)


@router.get("/api/reports/stock")
def get_stock_report(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    products = db.query(Product).filter(Product.company_id == cid, Product.active == True).all()
    res = []
    for p in products:
        cat_name = p.category.name if p.category else "-"
        for v in p.variations:
            price = v.price_override or p.base_price
            cost = v.cost_price_override or p.cost_price or 0
            stk = v.stock_quantity
            status = "esgotado" if stk == 0 else ("baixo" if stk <= 3 else "ok")
            res.append({
                "produto": p.name, "categoria": cat_name,
                "tamanho": v.size or "-", "cor": v.color or "-",
                "estoque": stk, "preco": price, "custo": cost,
                "valor_estoque": price * stk, "status": status,
            })
    return res


@router.get("/api/reports/stock/csv")
def export_stock_csv(request: Request, db: Session = Depends(get_db)):
    data = get_stock_report(request, db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Produto", "Categoria", "Tamanho", "Cor", "Estoque", "Preco", "Custo", "Valor em Estoque", "Status"])
    for r in data:
        writer.writerow([
            r["produto"], r["categoria"], r["tamanho"], r["cor"],
            r["estoque"], f"{r['preco']:.2f}", f"{r['custo']:.2f}",
            f"{r['valor_estoque']:.2f}", r["status"],
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=estoque.csv"},
    )


@router.get("/api/reports/low-stock")
def get_low_stock(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    items = (
        db.query(ProductVariation)
        .join(Product)
        .filter(Product.company_id == cid, ProductVariation.stock_quantity <= 3, Product.active == True)
        .all()
    )
    return [
        {"id": i.id, "product": i.product.name, "size": i.size, "color": i.color, "stock": i.stock_quantity}
        for i in items
    ]


@router.get("/api/reports/financial")
def get_financial_report(
    request: Request,
    start_date: str,
    end_date: str,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    sales = (
        db.query(Sale)
        .options(
            selectinload(Sale.items).selectinload(SaleItem.product),
            selectinload(Sale.items).selectinload(SaleItem.variation),
            selectinload(Sale.seller),
        )
        .filter(Sale.company_id == cid, Sale.date >= s_dt, Sale.date <= e_dt, Sale.status != "cancelled")
        .all()
    )
    gross_revenue = sum(s.total_amount for s in sales)
    total_discounts = sum(s.discount for s in sales)
    total_fees = sum(s.delivery_fee for s in sales)
    total_cost = 0.0
    total_commissions = 0.0
    for s in sales:
        for item in s.items:
            total_cost += (item.cost_price or 0.0) * item.quantity
        if s.seller_id and s.seller:
            total_commissions += s.total_amount * (s.seller.commission_rate / 100)
    expenses = db.query(Expense).filter(
        Expense.company_id == cid, Expense.date >= s_dt, Expense.date <= e_dt
    ).all()
    total_expenses = sum(e.amount for e in expenses)
    net_profit = gross_revenue - total_cost - total_commissions - total_expenses
    return {
        "revenue": gross_revenue,
        "discounts": total_discounts,
        "delivery_fees": total_fees,
        "cost_of_goods": total_cost,
        "commissions": total_commissions,
        "expenses": total_expenses,
        "net_profit": net_profit,
        "margin": (net_profit / gross_revenue * 100) if gross_revenue > 0 else 0,
    }


@router.get("/api/reports/commissions")
def get_commissions_report(
    request: Request,
    start_date: str,
    end_date: str,
    db: Session = Depends(get_db),
):
    cid = request.session.get("company_id")
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    sellers = db.query(Seller).filter(Seller.company_id == cid).all()
    report = []
    for sel in sellers:
        sales = db.query(Sale).filter(
            Sale.seller_id == sel.id, Sale.date >= s_dt, Sale.date <= e_dt, Sale.status != "cancelled"
        ).all()
        total_sold = sum(s.total_amount for s in sales)
        commission = total_sold * (sel.commission_rate / 100)
        report.append({
            "id": sel.id, "name": sel.name, "rate": sel.commission_rate,
            "count": len(sales), "total_sold": total_sold, "commission": commission,
        })
    return report
