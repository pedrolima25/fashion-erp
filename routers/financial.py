from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import (
    get_db, get_manaus_time, manaus_tz,
    Transaction, Sale, SaleItem, DailyCash, Expense, SalesGoal,
)

router = APIRouter()


class TransactionCreate(BaseModel):
    type: str
    amount: float
    description: str
    payment_method: str = "PIX"


class CashOpen(BaseModel):
    amount: float = 0.0


class CashMovement(BaseModel):
    type: str  # "sangria" ou "suprimento"
    amount: float
    description: str = ""


class ExpenseCreate(BaseModel):
    category: str
    description: str
    amount: float
    date: str


class SalesGoalCreate(BaseModel):
    month: int
    year: int
    target_value: float


def get_open_cash(db: Session, company_id: int):
    return db.query(DailyCash).filter(DailyCash.company_id == company_id, DailyCash.status == "open").first()


# --- TRANSACTIONS ---

@router.get("/api/financial/transactions")
def get_transactions(request: Request, db: Session = Depends(get_db), limit: int = 100, offset: int = 0):
    cid = request.session.get("company_id")
    txs = (
        db.query(Transaction)
        .filter(Transaction.company_id == cid)
        .order_by(Transaction.date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": t.id,
            "data": t.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
            "descricao": t.description,
            "tipo": t.type,
            "metodo": t.payment_method,
            "valor": t.amount,
        }
        for t in txs
    ]


@router.post("/api/financial/transactions")
def create_transaction(data: TransactionCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    db.add(Transaction(company_id=cid, type=data.type, amount=data.amount, description=data.description, payment_method=data.payment_method))
    db.commit()
    return {"success": True}


@router.get("/api/financial/stats")
def get_financial_stats(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    now = get_manaus_time()
    start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    entradas = db.query(func.sum(Transaction.amount)).filter(Transaction.company_id == cid, Transaction.type == "income", Transaction.date >= start_month).scalar() or 0.0
    saidas = db.query(func.sum(Transaction.amount)).filter(Transaction.company_id == cid, Transaction.type == "expense", Transaction.date >= start_month).scalar() or 0.0
    custo_vendas = db.query(func.sum(SaleItem.cost_price * SaleItem.quantity)).join(Sale).filter(Sale.company_id == cid, Sale.status == "completed", Sale.date >= start_month).scalar() or 0.0
    lucro_bruto = entradas - custo_vendas
    lucro_liquido = lucro_bruto - saidas
    return {
        "entradas": entradas, "saidas": saidas,
        "custo_vendas": custo_vendas, "lucro_bruto": lucro_bruto,
        "lucro_liquido": lucro_liquido, "saldo": lucro_liquido,
    }


# --- CASH (CAIXA) ---

@router.get("/api/cash/status")
def get_cash_status(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"is_open": False}
    cash = get_open_cash(db, int(cid))
    if not cash: return {"is_open": False}
    return {"is_open": True, "id": cash.id, "opening_balance": cash.opening_balance, "opened_at": cash.opened_at.astimezone(manaus_tz).strftime("%H:%M")}


@router.post("/api/cash/open")
def open_cash(request: Request, data: CashOpen, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"success": False, "error": "Sessao expirada."}
    cid = int(cid)
    if get_open_cash(db, cid): return {"success": False, "error": "Ja existe um caixa aberto."}
    db.add(DailyCash(company_id=cid, opening_balance=data.amount, status="open"))
    db.commit()
    return {"success": True}


@router.post("/api/cash/close")
def close_cash(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"success": False, "error": "Sessao expirada."}
    cash = get_open_cash(db, int(cid))
    if not cash: return {"success": False, "error": "Caixa nao encontrado."}
    total_sales = db.query(func.sum(Sale.total_amount)).filter(Sale.company_id == int(cid), Sale.status == "completed", Sale.date >= cash.opened_at).scalar() or 0.0
    cash.closing_balance = cash.opening_balance + total_sales
    cash.status = "closed"
    cash.closed_at = get_manaus_time()
    db.commit()
    return {"success": True, "final_balance": cash.closing_balance}


@router.post("/api/cash/movement")
def cash_movement(request: Request, data: CashMovement, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid:
        return JSONResponse(status_code=401, content={"error": "Sessão expirada."})
    cid = int(cid)
    cash = get_open_cash(db, cid)
    if not cash:
        return JSONResponse(status_code=400, content={"error": "Caixa não está aberto."})
    if data.amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Valor deve ser maior que zero."})
    label = "Sangria" if data.type == "sangria" else "Suprimento"
    desc = data.description.strip() or f"{label} de caixa"
    db.add(Transaction(
        company_id=cid,
        type="despesa" if data.type == "sangria" else "receita",
        category="Caixa",
        amount=data.amount,
        description=desc,
        payment_method="Dinheiro",
        date=get_manaus_time(),
    ))
    db.commit()
    return {"success": True}


# --- EXPENSES ---

@router.get("/api/expenses")
def get_expenses(request: Request, start_date: str, end_date: str, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    return db.query(Expense).filter(Expense.company_id == cid, Expense.date >= s_dt, Expense.date <= e_dt).all()


@router.post("/api/expenses")
def create_expense(request: Request, data: ExpenseCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    dt = datetime.fromisoformat(data.date).replace(tzinfo=manaus_tz)
    db.add(Expense(company_id=cid, category=data.category, description=data.description, amount=data.amount, date=dt))
    db.commit()
    return {"success": True}


@router.delete("/api/expenses/{expense_id}")
def delete_expense(request: Request, expense_id: int, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    exp = db.query(Expense).filter(Expense.id == expense_id, Expense.company_id == cid).first()
    if exp:
        db.delete(exp)
        db.commit()
    return {"success": True}


# --- GOALS ---

@router.get("/api/goals/current")
def get_current_goal(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    now = get_manaus_time()
    goal = db.query(SalesGoal).filter(SalesGoal.company_id == cid, SalesGoal.month == now.month, SalesGoal.year == now.year).first()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    current_revenue = db.query(func.sum(Sale.total_amount)).filter(Sale.company_id == cid, Sale.date >= month_start, Sale.status != "cancelled").scalar() or 0.0
    return {
        "target": goal.target_value if goal else 0.0,
        "current": current_revenue,
        "percent": (current_revenue / goal.target_value * 100) if goal and goal.target_value > 0 else 0,
    }


@router.post("/api/goals")
def save_goal(request: Request, data: SalesGoalCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    goal = db.query(SalesGoal).filter(SalesGoal.company_id == cid, SalesGoal.month == data.month, SalesGoal.year == data.year).first()
    if goal:
        goal.target_value = data.target_value
    else:
        db.add(SalesGoal(company_id=cid, month=data.month, year=data.year, target_value=data.target_value))
    db.commit()
    return {"success": True}
