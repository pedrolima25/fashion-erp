from typing import Optional, List
import sys, asyncio
from fastapi import FastAPI, Form, Depends, Request, Response
from pydantic import BaseModel
import httpx
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, insert, delete, case
from datetime import datetime, timedelta
import os
import json
import logging

from database import (
    Base, engine, SessionLocal, 
    get_manaus_time, manaus_tz, User, Company, 
    Subscription, pwd_context, Category, Product, ProductVariation, 
    Customer, Sale, SaleItem, Transaction, Neighborhood, run_migrations,
    populate_initial_data, ScheduledCampaign, DailyCash, DeliveryDriver, StockTransaction,
    Coupon, LoyaltyConfig, Seller, SalesGoal, Expense, Supplier
)
from ai_logic import process_message
from payments import generate_pix_payment
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
from whatsapp_service import whatsapp_manager

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- BACKGROUND TASKS ---

async def self_ping_task():
    """Mantém o servidor acordado em plataformas como Railway."""
    await asyncio.sleep(60)
    # Tenta pegar a URL do Railway se PUBLIC_URL não estiver definida
    url = os.environ.get("PUBLIC_URL") or os.environ.get("RAILWAY_STATIC_URL")
    if url and not url.startswith("http"):
        url = f"https://{url}/health"
    
    if not url:
        port = os.environ.get("PORT", "8001")
        url = f"http://127.0.0.1:{port}/health"
    
    logger.info(f"🌐 [Pinger] Iniciado para URL: {url}")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(url, timeout=10)
                logger.info(f"💓 [Pinger] Heartbeat: {response.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ [Pinger] Falha no heartbeat: {e}")
            await asyncio.sleep(300) # 5 minutos

async def whatsapp_keeper_task():
    """Monitora a saúde das sessões do WhatsApp e tenta reconectar automaticamente."""
    while True:
        try:
            await asyncio.sleep(600) # A cada 10 min
            logger.info("🔍 [Keeper] Verificando saúde das sessões WhatsApp...")
            with SessionLocal() as db:
                companies = db.query(Company).filter(Company.active == True, Company.is_archived == False).all()
                for c in companies:
                    status_info = whatsapp_manager.get_status(c.id)
                    if status_info.get("status") in ["DISCONNECTED", "ERROR", "UNLAUNCHED"]:
                        logger.warning(f"🚨 [Keeper] Empresa {c.id} ({c.name}) offline. Tentando reconectar...")
                        whatsapp_manager.start_session(c.id, force_new=False)
        except Exception as e:
            logger.error(f"❌ [Keeper] Erro: {e}")
            await asyncio.sleep(60)

async def auto_connect_whatsapp_task():
    """Conecta as empresas de forma escalonada para não travar o servidor."""
    logger.info("⏳ [Auto-Connect] Aguardando 15s para estabilização do sistema...")
    await asyncio.sleep(15) 
    
    try:
        with SessionLocal() as db:
            companies = db.query(Company).filter(Company.active == True, Company.is_archived == False).all()
            logger.info(f"⚡ [Auto-Connect] Encontradas {len(companies)} empresas ativas. Iniciando em fila...")
            
            for c in companies:
                status_info = whatsapp_manager.get_status(c.id)
                if status_info.get("status") in ["DISCONNECTED", "UNLAUNCHED", "ERROR"]:
                    logger.info(f"📲 [Auto-Connect] Iniciando sessão para {c.name} (ID {c.id})...")
                    whatsapp_manager.start_session(c.id, force_new=False)
                    
                    # Espera 30 segundos entre UM Início e OUTRO
                    # Isso é o SEGREDO para não travar o Windows/CPU
                    await asyncio.sleep(30)
                    
    except Exception as e:
        logger.error(f"❌ [Auto-Connect] Falha na fila de conexão: {e}")

async def marketing_worker_task():
    """Processa agendamentos de marketing nos grupos."""
    while True:
        try:
            await asyncio.sleep(60) # Verifica a cada minuto
            now = get_manaus_time()
            
            with SessionLocal() as db:
                # Busca campanhas pendentes que deveriam ter saído até agora
                campaigns = db.query(ScheduledCampaign).filter(
                    ScheduledCampaign.status == "pending",
                    ScheduledCampaign.scheduled_at <= now
                ).all()
                
                for camp in campaigns:
                    logger.info(f"🚀 [Marketing Worker] Iniciando campanha {camp.id} para empresa {camp.company_id}")
                    camp.status = "sending"
                    db.commit()
                    
                    try:
                        jids = json.loads(camp.group_jids)
                        pids = json.loads(camp.product_ids)
                        
                        for jid in jids:
                            # 1. Mensagem de Abertura
                            if camp.opening_msg:
                                await asyncio.to_thread(whatsapp_manager.send_message, camp.company_id, jid, camp.opening_msg)
                                await asyncio.sleep(2) 
                            
                        # Loop de Produtos
                        for pid in pids:
                            prod = db.query(Product).filter(Product.id == pid).first()
                            if prod:
                                company = db.query(Company).filter(Company.id == camp.company_id).first()
                                comp_name = company.name.upper() if company else "NOVIDADE"
                                msg = f"✨ *{comp_name}* ✨\n\n👗 {prod.name}\n\n💰 *VALOR:* R$ {prod.base_price:.2f}"
                                if prod.description: msg += f"\n\n📝 {prod.description}"
                                
                                # --- ENVIAR PARA GRUPOS ---
                                for jid in jids:
                                    logger.info(f"📤 [Marketing Worker] Enviando {prod.name} para grupo {jid}...")
                                    await asyncio.to_thread(whatsapp_manager.send_image, camp.company_id, jid, prod.image_base64, msg)
                                    await asyncio.sleep(4) 
                                
                                # --- POSTAR NO STATUS (se marcado) ---
                                if camp.post_to_status:
                                    logger.info(f"📱 [Marketing Worker] Postando {prod.name} no STATUS...")
                                    await asyncio.to_thread(whatsapp_manager.send_status_image, camp.company_id, prod.image_base64, msg)
                                    await asyncio.sleep(3)

                        # Pequeno respiro final da campanha
                        await asyncio.sleep(3)
                            
                        # Lógica de Recorrência
                        if camp.frequency == "daily":
                            camp.scheduled_at = camp.scheduled_at + timedelta(days=1)
                            camp.status = "pending"
                            logger.info(f"🔄 [Marketing Worker] Campanha {camp.id} re-agendada para {camp.scheduled_at}")
                        else:
                            camp.status = "sent"
                            
                    except Exception as e:
                        logger.error(f"❌ Erro na campanha {camp.id}: {e}")
                        camp.status = "failed"
                    finally:
                        db.commit()
                        
        except Exception as e:
            logger.error(f"❌ [Marketing Worker] Erro crítico: {e}")
            await asyncio.sleep(60)

async def crm_worker_task():
    """CRM Automático: Aniversários e Reativação."""
    while True:
        try:
            # Executa uma vez por dia (às 09:00 de Manaus ou na inicialização)
            logger.info("🤖 [CRM Worker] Verificando aniversariantes e clientes inativos...")
            now = get_manaus_time()
            today_str = now.strftime("%d/%m")
            
            with SessionLocal() as db:
                companies = db.query(Company).filter(Company.active == True).all()
                for co in companies:
                    # 1. ANIVERSARIANTES
                    bday_customers = db.query(Customer).filter(
                        Customer.company_id == co.id,
                        Customer.birthday.like(f"{today_str}%")
                    ).all()
                    
                    for cust in bday_customers:
                        msg = f"🎉 Parabéns, *{cust.name}*! 🎂\n\nA equipe da *{co.name}* deseja um dia incrível! Como presente, você ganhou um desconto especial em sua próxima compra. Use o cupom: *PARABENS10* 🎈"
                        await asyncio.to_thread(whatsapp_manager.send_message, co.id, f"{cust.phone}@s.whatsapp.net", msg)
                        await asyncio.sleep(2)

                    # 2. REATIVAÇÃO (Inativos há 45 dias)
                    forty_five_days_ago = now - timedelta(days=45)
                    # Busca clientes que a última venda foi há mais de 45 dias
                    # Simplificado: clientes que não tiveram vendas registradas no período
                    inactive_customers = db.query(Customer).filter(
                        Customer.company_id == co.id
                    ).all()
                    
                    for cust in inactive_customers:
                        last_sale = db.query(Sale).filter(Sale.customer_id == cust.id).order_by(Sale.date.desc()).first()
                        if last_sale and last_sale.date < forty_five_days_ago:
                            msg = f"Oi *{cust.name}*! 🤗 Sentimos sua falta aqui na *{co.name}*.\n\nFaz um tempinho que você não nos visita... Que tal conferir as novidades da semana? 👗✨"
                            await asyncio.to_thread(whatsapp_manager.send_message, co.id, f"{cust.phone}@s.whatsapp.net", msg)
                            await asyncio.sleep(2)

            # Espera 24 horas para a próxima verificação
            await asyncio.sleep(86400) 
        except Exception as e:
            logger.error(f"❌ [CRM Worker] Erro: {e}")
            await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando Fashion ERP Pro...")
    try:
        # Tenta sincronizar o banco. Em ambientes com múltiplos workers (como Railway/Gunicorn),
        # um worker terá sucesso e os outros podem falhar silenciosamente se o trabalho já estiver feito.
        Base.metadata.create_all(bind=engine)
        run_migrations()
        with SessionLocal() as session:
            from database import populate_initial_data
            populate_initial_data(session)
        logger.info("✅ Banco de dados sincronizado e pronto!")
    except Exception as db_err:
        logger.warning(f"⚠️ Aviso na sincronização do banco (pode ser concorrência): {db_err}")

    # Inicia tarefas em background (sempre, independente do banco)
    asyncio.create_task(self_ping_task())
    asyncio.create_task(whatsapp_keeper_task())
    asyncio.create_task(marketing_worker_task())
    asyncio.create_task(crm_worker_task())
    asyncio.create_task(auto_connect_whatsapp_task())

    yield
    logger.info("Desligando Fashion ERP...")

app = FastAPI(title="Fashion ERP & WhatsApp Bot", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key="fashion_secret_key_998")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# --- DEPENDENCIES ---

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_company(request: Request):
    company_id = request.session.get("company_id")
    if not company_id:
        return None
    return company_id

# --- AUTH ROUTES ---

@app.get("/")
def root():
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    username_clean = username.strip().lower()
    user = db.query(User).filter(User.username == username_clean).first()
    
    if not user or not pwd_context.verify(password, user.hashed_password):
        return JSONResponse(status_code=401, content={"success": False, "error": "Usuário ou senha incorretos."})
    
    if not user.is_master:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if not company or not company.active:
            return JSONResponse(status_code=403, content={"success": False, "error": "Unidade inativa."})
        
        # --- VERIFICAÇÃO DE ASSINATURA (Carência 3 dias) ---
        sub = db.query(Subscription).filter(Subscription.company_id == company.id).order_by(Subscription.end_date.desc()).first()
        if sub and sub.end_date:
            grace_date = sub.end_date + timedelta(days=3)
            if get_manaus_time().date() > grace_date.date():
                return JSONResponse(status_code=403, content={"success": False, "error": f"Assinatura vencida em {sub.end_date.strftime('%d/%m/%Y')}. Regularize para acessar."})

    request.session["user"] = username_clean
    request.session["company_id"] = user.company_id
    request.session["is_master"] = user.is_master
    request.session["role"] = user.role or "admin"
    return {"success": True}

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

# --- DASHBOARD ---

@app.get("/painel", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    
    # Se for Master, redireciona para painel administrativo global
    if request.session.get("is_master"):
        return templates.TemplateResponse(request, "master_admin.html", {"request": request})
        
    return templates.TemplateResponse(request, "dashboard.html", {"request": request, "active_page": "dashboard", "role": request.session.get("role")})

@app.get("/vendas", response_class=HTMLResponse)
def sales_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "vendas.html", {"request": request, "active_page": "sales", "role": request.session.get("role")})

@app.get("/produtos", response_class=HTMLResponse)
def products_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "produtos.html", {"request": request, "active_page": "products", "role": request.session.get("role")})

@app.get("/estoque", response_class=HTMLResponse)
def inventory_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "estoque.html", {"request": request, "active_page": "inventory", "role": request.session.get("role")})

@app.get("/clientes", response_class=HTMLResponse)
def customers_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "clientes.html", {"request": request, "active_page": "customers", "role": request.session.get("role")})

@app.get("/financeiro", response_class=HTMLResponse)
def finance_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "financeiro.html", {"request": request, "active_page": "finance", "role": request.session.get("role")})

@app.get("/marketing", response_class=HTMLResponse)
def marketing_page(request: Request, db: Session = Depends(get_db)):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    cid = request.session.get("company_id")
    company = db.query(Company).filter(Company.id == cid).first()
    return templates.TemplateResponse(request, "marketing.html", {"request": request, "active_page": "marketing", "company": company, "role": request.session.get("role")})

@app.get("/configuracoes", response_class=HTMLResponse)
def settings_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "configuracoes.html", {"request": request, "active_page": "settings", "role": request.session.get("role")})

@app.get("/entregas", response_class=HTMLResponse)
def deliveries_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "entregas.html", {"request": request, "active_page": "deliveries"})

@app.get("/api/dashboard/stats")
def get_dashboard_stats(request: Request, db: Session = Depends(get_db)):
    company_id = request.session.get("company_id")
    if not company_id: return {}
    
    company_id = int(company_id)
    today_start = get_manaus_time().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    
    vendas_hoje = db.query(func.sum(Sale.total_amount)).filter(
        Sale.company_id == company_id,
        Sale.date >= today_start,
        Sale.date < today_end
    ).scalar() or 0.0
    
    total_produtos = db.query(Product).filter(Product.company_id == company_id, Product.active == True).count()
    total_clientes = db.query(Customer).filter(Customer.company_id == company_id).count()
    pedidos_pendentes = db.query(Sale).filter(Sale.company_id == company_id, Sale.status == "pending").count()
    
    # Priorizamos pedidos Pendentes no topo, depois ordenamos por data
    recentes = db.query(Sale).filter(Sale.company_id == company_id).order_by(
        case({Sale.status == "pending": 0}, else_=1),
        Sale.date.desc()
    ).limit(20).all()
    # Debug log (remover depois)
    print(f"DEBUG: Company {company_id} - Found {len(recentes)} recent sales. Total Hoje: {vendas_hoje}")
    
    vendas_list = []
    for r in recentes:
        # Garantimos que a data do banco (que pode vir em UTC) seja convertida para Manaus
        horario = r.date.astimezone(manaus_tz).strftime("%H:%M")
            
        vendas_list.append({
            "id": r.id,
            "data": horario,
            "cliente": r.customer.name if r.customer else "Final Consumidor",
            "total": r.total_amount,
            "pagamento": r.payment_method,
            "logistica": r.delivery_type if r.delivery_type else "Presencial",
            "endereco": r.delivery_address if r.delivery_address else "",
            "status": r.status
        })
        
    return {
        "vendas_hoje": vendas_hoje,
        "total_produtos": total_produtos,
        "total_clientes": total_clientes,
        "pedidos_pendentes": pedidos_pendentes,
        "vendas_recentes": vendas_list,
        "last_sale_id": recentes[0].id if recentes else 0
    }

# --- PRODUCTS & CATEGORIES ---

class VariationSchema(BaseModel):
    size: str
    color: Optional[str] = None
    stock: int = 0
    price: Optional[float] = None
    cost: Optional[float] = None

class SalesGoalCreate(BaseModel):
    month: int
    year: int
    target_value: float

class ExpenseCreate(BaseModel):
    category: str
    description: str
    amount: float
    date: str # ISO date

class UserCreate(BaseModel):
    username: str
    password: str
    role: str # admin, seller

class CategoryCreate(BaseModel):
    name: str

class ProductCreate(BaseModel):
    name: str
    base_price: float
    cost_price: float = 0.0
    description: Optional[str] = None
    category_id: Optional[int] = None
    image_base64: Optional[str] = None
    show_on_whatsapp: bool = True
    variations: list[VariationSchema]

@app.get("/api/categories")
def get_categories(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    return db.query(Category).filter(Category.company_id == cid).all()

@app.post("/api/categories")
def create_category(data: CategoryCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    new_cat = Category(company_id=cid, name=data.name)
    db.add(new_cat)
    db.commit()
    return {"success": True, "id": new_cat.id}

@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    cat = db.query(Category).filter(Category.id == cat_id, Category.company_id == cid).first()
    if not cat: return JSONResponse(status_code=404, content={"error": "Categoria não encontrada"})
    db.delete(cat)
    db.commit()
    return {"success": True}

@app.get("/api/products")
def get_products(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    prods = db.query(Product).filter(Product.company_id == cid, Product.active == True).all()
    res = []
    for p in prods:
        vars_list = []
        for v in p.variations:
            vars_list.append({
                "id": v.id, 
                "size": v.size, 
                "color": v.color or "", 
                "stock": v.stock_quantity, 
                "price": v.price_override or p.base_price,
                "cost": v.cost_price_override or p.cost_price
            })
        res.append({
            "id": p.id,
            "name": p.name,
            "category": p.category.name if p.category else "Sem Categoria",
            "price": p.base_price,
            "cost": p.cost_price,
            "show_on_whatsapp": p.show_on_whatsapp,
            "image": p.image_base64,
            "variations": vars_list
        })
    return res

@app.post("/api/products")
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
        show_on_whatsapp=data.show_on_whatsapp
    )
    db.add(new_p)
    db.flush()
    
    for v in data.variations:
        new_v = ProductVariation(
            product_id=new_p.id if 'new_p' in locals() else product.id,
            size=v.size,
            color=v.color,
            stock_quantity=v.stock,
            price_override=v.price,
            cost_price_override=v.cost
        )
        db.add(new_v)
    
    db.commit()
    return {"success": True}

@app.put("/api/products/{product_id}")
def update_product(product_id: int, data: ProductCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    product = db.query(Product).filter(Product.id == product_id, Product.company_id == cid).first()
    if not product: return JSONResponse(status_code=404, content={"error": "Produto não encontrado"})
    
    product.name = data.name
    product.base_price = data.base_price
    product.cost_price = data.cost_price
    product.description = data.description
    product.category_id = data.category_id
    product.show_on_whatsapp = data.show_on_whatsapp
    if data.image_base64:
        product.image_base64 = data.image_base64
    
    # Remove variações antigas e adiciona novas
    db.query(ProductVariation).filter(ProductVariation.product_id == product_id).delete()
    for v in data.variations:
        new_v = ProductVariation(
            product_id=product.id,
            size=v.size,
            color=v.color,
            stock_quantity=v.stock,
            price_override=v.price,
            cost_price_override=v.cost
        )
        db.add(new_v)
        
    db.commit()
    return {"success": True}

@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    product = db.query(Product).filter(Product.id == product_id, Product.company_id == cid).first()
    if not product: return JSONResponse(status_code=404, content={"error": "Produto não encontrado"})
    
    product.active = False
    db.commit()
    return {"success": True}

# --- SALES & PDV ---

class SaleItemSchema(BaseModel):
    product_id: int
    variation_id: int
    quantity: int
    price: float

class SaleCreate(BaseModel):
    items: list[SaleItemSchema]
    payment_method: str
    discount: float = 0.0
    delivery_type: Optional[str] = "pickup"
    delivery_address: Optional[str] = None

@app.post("/api/sales")
def finalize_sale(data: SaleCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    
    total = sum(item.price * item.quantity for item in data.items) - data.discount
    
    new_sale = Sale(
        company_id=cid,
        total_amount=total,
        discount=data.discount,
        payment_method=data.payment_method,
        delivery_type=data.delivery_type,
        delivery_address=data.delivery_address,
        status="pending"
    )
    db.add(new_sale)
    db.flush()
    
    for item in data.items:
        # Busca o custo atual para registro histórico
        item_cost = 0.0
        if item.variation_id:
            var = db.query(ProductVariation).filter(ProductVariation.id == item.variation_id).first()
            if var:
                item_cost = var.cost_price_override if var.cost_price_override is not None else var.product.cost_price
        else:
            prod = db.query(Product).filter(Product.id == item.product_id).first()
            if prod:
                item_cost = prod.cost_price

        sale_item = SaleItem(
            sale_id=new_sale.id,
            product_id=item.product_id,
            variation_id=item.variation_id,
            quantity=item.quantity,
            unit_price=item.price,
            cost_price=item_cost
        )
        db.add(sale_item)
        # Note: Stock deduction and Transaction creation now happen in Confirm Sale
    
    db.commit()
    return {"success": True}

@app.post("/api/sales/{sale_id}/confirm")
def confirm_sale(sale_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        raw_cid = request.session.get("company_id")
        if raw_cid is None:
            return JSONResponse(status_code=401, content={"error": "Sessão expirada. Faça login novamente."})
        
        cid = int(raw_cid)
        sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid).first()
        
        if not sale: 
            return JSONResponse(status_code=404, content={"error": f"Venda {sale_id} não encontrada para empresa {cid}"})
        
        if sale.status != "pending": 
            return {"success": True, "message": "Venda já processada"}
        
        # 1. Deduzir Estoque dos Itens
        if sale.items:
            for item in sale.items:
                if item.variation_id:
                    var = db.query(ProductVariation).filter(ProductVariation.id == item.variation_id).first()
                    if var:
                        var.stock_quantity -= item.quantity
                
        # 2. Criar Transação Financeira
        new_tx = Transaction(
            company_id=cid,
            type="income",
            amount=sale.total_amount,
            description=f"Pedido #{sale.id} Confirmado",
            payment_method=sale.payment_method if sale.payment_method else "PIX"
        )
        db.add(new_tx)
        
        # 3. Atualizar Status
        sale.status = "completed"
        
        # 4. Acumular Pontos de Fidelidade
        if sale.customer_id:
            loyalty = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid, LoyaltyConfig.active == True).first()
            if loyalty:
                pontos = int(sale.total_amount * loyalty.points_per_real)
                customer = db.query(Customer).filter(Customer.id == sale.customer_id).first()
                if customer and pontos > 0:
                    customer.loyalty_points = (customer.loyalty_points or 0) + pontos
                    customer.total_points_earned = (customer.total_points_earned or 0) + pontos
        
        db.commit()
        
        # 5. Enviar Comprovante via WhatsApp (async, não bloqueia)
        try:
            if sale.customer and sale.customer.phone:
                items_text = ""
                for item in sale.items:
                    pname = item.product.name if item.product else "Produto"
                    vsize = f" ({item.variation.size})" if item.variation else ""
                    items_text += f"  • {item.quantity}x {pname}{vsize} - R$ {item.unit_price * item.quantity:.2f}\n"
                
                company = db.query(Company).filter(Company.id == cid).first()
                loja_nome = company.name.upper() if company else "LOJA"
                
                msg = f"✅ *COMPROVANTE DE COMPRA*\n\n"
                msg += f"🏪 *{loja_nome}*\n"
                msg += f"📋 Pedido #{sale.id}\n"
                msg += f"📅 {sale.date.astimezone(manaus_tz).strftime('%d/%m/%Y %H:%M')}\n\n"
                msg += f"*Itens:*\n{items_text}\n"
                if sale.discount and sale.discount > 0:
                    msg += f"💸 Desconto: -R$ {sale.discount:.2f}\n"
                if sale.delivery_fee and sale.delivery_fee > 0:
                    msg += f"🛵 Frete: R$ {sale.delivery_fee:.2f}\n"
                msg += f"\n💰 *TOTAL: R$ {sale.total_amount:.2f}*\n"
                msg += f"💳 Pagamento: {sale.payment_method}\n\n"
                msg += f"Obrigado pela compra! 🎉"
                
                whatsapp_manager.send_message(cid, sale.customer.phone, msg)
        except Exception as wp_err:
            logger.warning(f"⚠️ Erro ao enviar comprovante WhatsApp: {wp_err}")
        
        return {"success": True}
    except Exception as e:
        db.rollback()
        print(f"ERRO AO CONFIRMAR VENDA: {e}")
        return JSONResponse(status_code=500, content={"error": f"Erro interno: {str(e)}"})

@app.post("/api/sales/{sale_id}/cancel")
def cancel_sale(sale_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid).first()
    if not sale: return JSONResponse(status_code=404, content={"error": "Venda não encontrada"})
    
    sale.status = "cancelled"
    db.commit()
    return {"success": True}

# --- CLIENTS API ---

class CustomerCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None

@app.get("/api/customers")
def get_customers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    return db.query(Customer).filter(Customer.company_id == cid).all()

@app.post("/api/customers")
def create_customer(data: CustomerCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    # Limpeza básica de telefone
    phone_clean = "".join(filter(str.isdigit, data.phone))
    
    existing = db.query(Customer).filter(Customer.company_id == cid, Customer.phone == phone_clean).first()
    if existing:
        return {"success": False, "error": "Cliente já cadastrado."}
        
    new_c = Customer(
        company_id=cid,
        name=data.name,
        phone=phone_clean,
        email=data.email
    )
    db.add(new_c)
    db.commit()
    return {"success": True}

# --- INVENTORY API ---

class AdjustmentCreate(BaseModel):
    variation_id: int
    type: str # entry, exit, adjustment
    quantity: int
    description: Optional[str] = None

@app.post("/api/inventory/adjust")
def adjust_inventory(data: AdjustmentCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    var = db.query(ProductVariation).filter(ProductVariation.id == data.variation_id).first()
    if not var: return {"error": "Variação não encontrada"}
    
    # Verifica se a variação pertence a um produto da empresa
    if var.product.company_id != cid: return {"error": "Acesso negado"}
    
    qty_change = data.quantity
    if data.type == "exit":
        qty_change = -abs(data.quantity)
    
    var.stock_quantity += qty_change
    
    from database import StockTransaction
    new_st = StockTransaction(
        variation_id=var.id,
        type=data.type,
        quantity=qty_change,
        description=data.description
    )
    db.add(new_st)
    db.commit()
    return {"success": True}

# --- FINANCIAL API ---

class TransactionCreate(BaseModel):
    type: str # income, expense
    amount: float
    description: str
    payment_method: str = "PIX"

@app.get("/api/financial/transactions")
def get_transactions(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    txs = db.query(Transaction).filter(Transaction.company_id == cid).order_by(Transaction.date.desc()).all()
    res = []
    for t in txs:
        res.append({
            "id": t.id,
            "data": t.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
            "descricao": t.description,
            "tipo": t.type, # income / expense
            "metodo": t.payment_method,
            "valor": t.amount
        })
    return res

@app.get("/api/financial/stats")
def get_financial_stats(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    now = get_manaus_time()
    start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # 1. Faturamento Total (Entradas do Mês)
    entradas = db.query(func.sum(Transaction.amount)).filter(
        Transaction.company_id == cid,
        Transaction.type == "income",
        Transaction.date >= start_month
    ).scalar() or 0.0
    
    # 2. Outras Despesas (Saídas do Mês)
    saidas = db.query(func.sum(Transaction.amount)).filter(
        Transaction.company_id == cid,
        Transaction.type == "expense",
        Transaction.date >= start_month
    ).scalar() or 0.0
    
    # 3. Custo das Mercadorias Vendidas (CMV)
    # Buscamos o custo acumulado de todos os itens vendidos em vendas concluídas no mês
    custo_vendas = db.query(func.sum(SaleItem.cost_price * SaleItem.quantity)).join(Sale).filter(
        Sale.company_id == cid,
        Sale.status == "completed",
        Sale.date >= start_month
    ).scalar() or 0.0
    
    lucro_bruto = entradas - custo_vendas
    lucro_liquido = lucro_bruto - saidas
    
    return {
        "entradas": entradas,
        "saidas": saidas,
        "custo_vendas": custo_vendas,
        "lucro_bruto": lucro_bruto,
        "lucro_liquido": lucro_liquido,
        "saldo": lucro_liquido # Mantendo compatibilidade se algum lugar usar 'saldo'
    }

@app.post("/api/financial/transactions")
def create_transaction(data: TransactionCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    new_tx = Transaction(
        company_id=cid,
        type=data.type,
        amount=data.amount,
        description=data.description,
        payment_method=data.payment_method
    )
    db.add(new_tx)
    db.commit()
    return {"success": True}

# --- SETTINGS API ---

class SettingsUpdate(BaseModel):
    name: str
    whatsapp_number: Optional[str] = None
    pix_key: Optional[str] = None
    address: Optional[str] = None
    location_link: Optional[str] = None
    delivery_fee: float = 0.0
    delivery_mode: str = "fixed"

@app.get("/api/config")
def get_settings(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp: return {"error": "Empresa não encontrada"}
    return {
        "name": comp.name,
        "whatsapp_number": comp.whatsapp_number,
        "pix_key": comp.pix_key,
        "address": comp.address,
        "location_link": comp.location_link,
        "delivery_fee": comp.delivery_fee,
        "delivery_mode": comp.delivery_mode or "fixed",
        "slug": comp.slug or ""
    }

@app.post("/api/config/save")
def save_settings(data: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp: return {"error": "Não encontrado"}
    
    comp.name = data.name
    comp.whatsapp_number = data.whatsapp_number
    comp.pix_key = data.pix_key
    comp.address = data.address
    comp.location_link = data.location_link
    comp.delivery_fee = data.delivery_fee
    comp.delivery_mode = data.delivery_mode
    
    db.commit()
    return {"success": True}

# --- NEIGHBORHOODS ---

class NeighborhoodSchema(BaseModel):
    id: Optional[int] = None
    name: str
    fee: float

@app.get("/api/neighborhoods")
def list_neighborhoods(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    items = db.query(Neighborhood).filter(Neighborhood.company_id == cid).all()
    return items

@app.post("/api/neighborhoods")
def save_neighborhood(data: NeighborhoodSchema, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if data.id:
        item = db.query(Neighborhood).filter(Neighborhood.id == data.id, Neighborhood.company_id == cid).first()
        if item:
            item.name = data.name
            item.fee = data.fee
    else:
        item = Neighborhood(company_id=cid, name=data.name, fee=data.fee)
        db.add(item)
    db.commit()
    return {"success": True}

@app.delete("/api/neighborhoods/{nid}")
def delete_neighborhood(nid: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    item = db.query(Neighborhood).filter(Neighborhood.id == nid, Neighborhood.company_id == cid).first()
    if item:
        db.delete(item)
        db.commit()
    return {"success": True}

# --- MASTER ADMIN API ---

@app.get("/api/master/stats")
def get_master_stats(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    total_lojas = db.query(Company).filter(Company.is_archived == False).count()
    ativas = db.query(Company).filter(Company.active == True, Company.is_archived == False).count()
    return {
        "total_lojas": total_lojas,
        "ativas": ativas,
        "receita_mensal": total_lojas * 99.90
    }

class CompanyCreateMaster(BaseModel):
    name: str
    category: Optional[str] = "Loja de Roupas"
    admin_username: str
    admin_password: str

@app.post("/api/master/companies")
def create_master_company(data: CompanyCreateMaster, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    
    # 1. Verifica se usuário já existe
    from database import Subscription
    if db.query(User).filter(User.username == data.admin_username).first():
        return JSONResponse(status_code=400, content={"error": "Usuário já em uso"})
        
    # 2. Cria Empresa
    new_co = Company(name=data.name, category=data.category)
    db.add(new_co)
    db.flush()
    
    # 3. Cria Admin
    new_user = User(
        username=data.admin_username,
        hashed_password=pwd_context.hash(data.admin_password),
        company_id=new_co.id
    )
    db.add(new_user)
    
    # 4. Criar Assinatura Inicial (30 dias)
    new_sub = Subscription(
        company_id=new_co.id,
        plan_type="mensal",
        start_date=get_manaus_time(),
        end_date=get_manaus_time() + timedelta(days=30),
        status="active"
    )
    db.add(new_sub)
    
    db.commit()
    return {"success": True, "company_id": new_co.id}

class CompanyUpdateMaster(BaseModel):
    name: Optional[str]
    category: Optional[str]
    expiry_date: Optional[str] # YYYY-MM-DD

@app.put("/api/master/companies/{cid}")
def update_master_company(cid: int, data: CompanyUpdateMaster, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp: return JSONResponse(status_code=404, content={"error": "Empresa não encontrada"})
    
    if data.name is not None: comp.name = data.name
    if data.category is not None: comp.category = data.category
    
    if data.expiry_date:
        from database import Subscription
        sub = db.query(Subscription).filter(Subscription.company_id == cid).order_by(Subscription.end_date.desc()).first()
        # Converte string YYYY-MM-DD para datetime Manaus
        new_dt = datetime.strptime(data.expiry_date, "%Y-%m-%d").replace(tzinfo=manaus_tz)
        
        if not sub:
            sub = Subscription(company_id=cid, start_date=get_manaus_time(), end_date=new_dt, status="active")
            db.add(sub)
        else:
            sub.end_date = new_dt
            
    db.commit()
    return {"success": True}

@app.get("/api/master/companies")
def get_master_companies(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    companies = db.query(Company).filter(Company.is_archived == False).all()
    res = []
    for c in companies:
        sub = db.query(Subscription).filter(Subscription.company_id == c.id).order_by(Subscription.end_date.desc()).first()
        
        # Auto-Heal: Se não existir assinatura, cria uma de cortesia (30 dias)
        if not sub:
            sub = Subscription(
                company_id=c.id, 
                start_date=get_manaus_time(), 
                end_date=get_manaus_time() + timedelta(days=30), 
                status="active"
            )
            db.add(sub)
            db.commit()
            db.refresh(sub)
            
        status_wpp = whatsapp_manager.get_status(c.id).get("status", "OFFLINE")
        res.append({
            "id": c.id,
            "name": c.name,
            "category": c.category,
            "active": c.active,
            "expiry": sub.end_date.strftime("%d/%m/%Y") if sub.end_date else "N/A",
            "whatsapp_status": status_wpp
        })
    return res

@app.post("/api/master/companies/{cid}/extend")
def extend_subscription(cid: int, request: Request, days: int = Form(...), db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    sub = db.query(Subscription).filter(Subscription.company_id == cid).order_by(Subscription.end_date.desc()).first()
    if not sub:
        sub = Subscription(company_id=cid, start_date=get_manaus_time(), end_date=get_manaus_time() + timedelta(days=days), status="active")
        db.add(sub)
    else:
        base_date = max(sub.end_date.date(), get_manaus_time().date())
        sub.end_date = datetime.combine(base_date + timedelta(days=days), datetime.min.time()).replace(tzinfo=manaus_tz)
        sub.status = "active"
    comp = db.query(Company).filter(Company.id == cid).first()
    if comp: comp.active = True
    db.commit()
    return {"success": True}

@app.post("/api/master/companies/{cid}/toggle")
def toggle_company(cid: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("is_master"): return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    comp = db.query(Company).filter(Company.id == cid).first()
    if not comp: return {"error": "Não encontrado"}
    comp.active = not comp.active
    db.commit()
    return {"success": True, "is_active": comp.active}

# --- WHATSAPP API ---

@app.get("/api/whatsapp/status")
def whatsapp_status(request: Request):
    company_id = request.session.get("company_id")
    if not company_id:
        return {"status": "DISCONNECTED", "qr_code": None}
    return whatsapp_manager.get_status(company_id)

@app.post("/api/whatsapp/start")
def whatsapp_start(request: Request, force: bool = False):
    company_id = request.session.get("company_id")
    if not company_id: return {"error": "Unauthorized"}
    whatsapp_manager.start_session(company_id, force_new=force)
    return {"success": True}

@app.post("/api/whatsapp/clean")
def whatsapp_clean(request: Request):
    company_id = request.session.get("company_id")
    if not company_id: return {"error": "Unauthorized"}
    whatsapp_manager.clean_session(company_id)
    return {"success": True}

@app.get("/api/whatsapp/groups")
async def get_whatsapp_groups(request: Request, refresh: bool = False):
    cid = request.session.get("company_id")
    if not cid: return []
    try:
        # Offloading para evitar "Result is not set" bloqueando o main loop
        return await asyncio.to_thread(whatsapp_manager.get_groups, cid, force_refresh=refresh)
    except Exception as e:
        logger.error(f"❌ Erro ao buscar grupos (API): {e}")
        return []

class BroadcastRequest(BaseModel):
    jids: list[str]
    message: str
    image_base64: Optional[str] = None

@app.post("/api/whatsapp/broadcast")
async def whatsapp_broadcast(data: BroadcastRequest, request: Request):
    cid = request.session.get("company_id")
    if not cid: return {"error": "Unauthorized"}
    
    success_count = 0
    for jid in data.jids:
        if data.image_base64:
            res = await asyncio.to_thread(whatsapp_manager.send_image, cid, jid, data.image_base64, data.message)
        else:
            res = await asyncio.to_thread(whatsapp_manager.send_message, cid, jid, data.message)
        
        if res: success_count += 1
        await asyncio.sleep(0.5)
        
    return {"success": True, "sent": success_count}

@app.get("/api/marketing/products")
def get_marketing_products(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    prods = db.query(Product).filter(Product.company_id == cid, Product.active == True).all()
    return [{
        "id": p.id,
        "name": p.name,
        "price": p.base_price,
        "description": p.description,
        "image": p.image_base64
    } for p in prods]

class ScheduleCampaignCreate(BaseModel):
    product_ids: List[int]
    group_jids: List[str]
    opening_msg: Optional[str] = ""
    scheduled_at: str # ISO string
    frequency: str = "once" # once, daily
    post_to_status: bool = False

@app.post("/api/marketing/schedule")
def schedule_campaign(data: ScheduleCampaignCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    try:
        dt = datetime.fromisoformat(data.scheduled_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=manaus_tz)
        
        new_camp = ScheduledCampaign(
            company_id=cid,
            product_ids=json.dumps(data.product_ids or []),
            group_jids=json.dumps(data.group_jids or []),
            opening_msg=data.opening_msg,
            scheduled_at=dt,
            frequency=data.frequency,
            post_to_status=data.post_to_status,
            status="pending"
        )
        db.add(new_camp)
        db.commit()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.get("/api/marketing/scheduled")
def list_scheduled(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    camps = db.query(ScheduledCampaign).filter(
        ScheduledCampaign.company_id == cid,
        ScheduledCampaign.status == "pending"
    ).order_by(ScheduledCampaign.scheduled_at.asc()).all()
    
    res = []
    for c in camps:
        pids = json.loads(c.product_ids)
        res.append({
            "id": c.id,
            "scheduled_at": c.scheduled_at.strftime("%d/%m/%Y %H:%M"),
            "frequency": "Diário" if c.frequency == "daily" else "Uma vez",
            "groups_count": len(json.loads(c.group_jids)),
            "products_count": len(pids),
            "opening": c.opening_msg[:30] + "..." if c.opening_msg and len(c.opening_msg) > 30 else (c.opening_msg or "N/A")
        })
    return res

@app.delete("/api/marketing/scheduled/{camp_id}")
def delete_scheduled(camp_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    camp = db.query(ScheduledCampaign).filter(ScheduledCampaign.id == camp_id, ScheduledCampaign.company_id == cid).first()
    if not camp: return JSONResponse(status_code=404, content={"error": "Não encontrado"})
    db.delete(camp)
    db.commit()
    return {"success": True}

# --- ROTA DE RELATORIOS ---

@app.get("/relatorios", response_class=HTMLResponse)
def reports_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "relatorios.html", {"request": request, "active_page": "reports"})

# --- CASH (CAIXA) API ---

import secrets
import hmac
import hashlib
from html import escape

def get_open_cash(db: Session, company_id: int):
    return db.query(DailyCash).filter(
        DailyCash.company_id == company_id,
        DailyCash.status == "open"
    ).first()

class CashOpen(BaseModel):
    amount: float = 0.0

@app.get("/api/cash/status")
def get_cash_status(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"is_open": False}
    cash = get_open_cash(db, int(cid))
    if not cash: return {"is_open": False}
    return {
        "is_open": True,
        "id": cash.id,
        "opening_balance": cash.opening_balance,
        "opened_at": cash.opened_at.astimezone(manaus_tz).strftime("%H:%M")
    }

@app.post("/api/cash/open")
def open_cash(request: Request, data: CashOpen, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"success": False, "error": "Sessao expirada."}
    cid = int(cid)
    if get_open_cash(db, cid): return {"success": False, "error": "Ja existe um caixa aberto."}
    new_cash = DailyCash(company_id=cid, opening_balance=data.amount, status="open")
    db.add(new_cash); db.commit()
    return {"success": True}

@app.post("/api/cash/close")
def close_cash(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    if not cid: return {"success": False, "error": "Sessao expirada."}
    cash = get_open_cash(db, int(cid))
    if not cash: return {"success": False, "error": "Caixa nao encontrado."}
    total_sales = db.query(func.sum(Sale.total_amount)).filter(
        Sale.company_id == int(cid), Sale.status == "completed", Sale.date >= cash.opened_at
    ).scalar() or 0.0
    cash.closing_balance = cash.opening_balance + total_sales
    cash.status = "closed"
    cash.closed_at = get_manaus_time()
    db.commit()
    return {"success": True, "final_balance": cash.closing_balance}

# --- SALES CHART ---

@app.get("/api/stats/sales-chart")
def get_sales_chart_data(request: Request, db: Session = Depends(get_db)):
    company_id = request.session.get("company_id")
    if not company_id: return []
    res = []
    for i in range(6, -1, -1):
        day = get_manaus_time() - timedelta(days=i)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        total = db.query(func.sum(Sale.total_amount)).filter(
            Sale.company_id == company_id,
            Sale.date >= start, Sale.date < end,
            Sale.status != "cancelled"
        ).scalar() or 0.0
        res.append({"label": day.strftime("%d/%m"), "value": total})
    return res

# --- CUSTOMER HISTORY ---

@app.get("/api/customers/{customer_id}/history")
def get_customer_history(customer_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sales = db.query(Sale).filter(Sale.customer_id == customer_id, Sale.company_id == cid).order_by(Sale.date.desc()).all()
    return [{"id": s.id, "date": s.date.strftime("%d/%m/%Y %H:%M"), "total": s.total_amount, "status": s.status} for s in sales]

# --- REPORTS API ---

@app.get("/api/reports/summary")
def get_reports_summary(request: Request, start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    query = db.query(Sale).filter(Sale.company_id == cid, Sale.status != "cancelled")
    dt_start = None
    dt_end = None
    if start_date:
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        query = query.filter(Sale.date >= dt_start)
    if end_date:
        dt_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(Sale.date < dt_end)
    sales = query.all()
    total_revenue = sum(s.total_amount for s in sales)
    total_discount = sum(s.discount for s in sales)
    total_freight = sum(s.delivery_fee or 0 for s in sales)
    # Metodos de pagamento
    pay_map = {}
    for s in sales:
        m = s.payment_method or "Outro"
        pay_map[m] = pay_map.get(m, 0) + s.total_amount
    payment_methods = [{"metodo": k, "total": v} for k, v in sorted(pay_map.items(), key=lambda x: x[1], reverse=True)]
    # Logistica
    delivery_count = sum(1 for s in sales if s.delivery_type == "delivery")
    pickup_count = sum(1 for s in sales if s.delivery_type != "delivery")
    # Dados diarios
    daily_map = {}
    for s in sales:
        dia = s.date.astimezone(manaus_tz).strftime("%d/%m")
        daily_map[dia] = daily_map.get(dia, 0) + s.total_amount
    daily = [{"dia": k, "total": v} for k, v in daily_map.items()]
    return {
        "total_orders": len(sales),
        "total_revenue": total_revenue,
        "ticket_medio": total_revenue / len(sales) if sales else 0,
        "total_discount": total_discount,
        "total_freight": total_freight,
        "payment_methods": payment_methods,
        "delivery_count": delivery_count,
        "pickup_count": pickup_count,
        "daily": daily
    }

@app.get("/api/reports/sales")
def get_sales_report(request: Request, start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    query = db.query(Sale).filter(Sale.company_id == cid)
    if start_date:
        query = query.filter(Sale.date >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        query = query.filter(Sale.date < datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1))
    sales = query.order_by(Sale.date.desc()).all()
    return [{
        "id": s.id,
        "data": s.date.astimezone(manaus_tz).strftime("%d/%m/%Y %H:%M"),
        "cliente": s.customer.name if s.customer else "Consumidor",
        "total": s.total_amount,
        "metodo": s.payment_method,
        "logistica": s.delivery_type or "pickup",
        "status": s.status
    } for s in sales]

@app.get("/api/reports/products")
def get_products_report(request: Request, start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    query = db.query(SaleItem).join(Sale).filter(Sale.company_id == cid, Sale.status != "cancelled")
    if start_date:
        query = query.filter(Sale.date >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        query = query.filter(Sale.date < datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1))
    items = query.all()
    product_map = {}
    for item in items:
        key = f"{item.product_id}_{item.variation_id or 0}"
        if key not in product_map:
            product_map[key] = {
                "nome": item.product.name if item.product else "Produto",
                "tamanho": item.variation.size if item.variation else "-",
                "qtd": 0, "receita": 0, "custo": 0
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

@app.get("/api/reports/customers")
def get_customers_report(request: Request, start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    query = db.query(Sale).filter(Sale.company_id == cid, Sale.status != "cancelled", Sale.customer_id.isnot(None))
    if start_date:
        query = query.filter(Sale.date >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        query = query.filter(Sale.date < datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1))
    sales = query.all()
    cust_map = {}
    for s in sales:
        cid_c = s.customer_id
        if cid_c not in cust_map:
            cust_map[cid_c] = {
                "nome": s.customer.name if s.customer else "?",
                "telefone": s.customer.phone if s.customer else "-",
                "pedidos": 0, "total": 0, "ultima_data": s.date
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

@app.get("/api/reports/stock")
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
                "valor_estoque": price * stk, "status": status
            })
    return res

# --- DELIVERY API ---

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
            "vehicle": sale.delivery_driver.vehicle or ""
        } if sale.delivery_driver else None,
        "assigned_at": sale.delivery_assigned_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_assigned_at else "",
        "dispatched_at": sale.delivery_dispatched_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_dispatched_at else "",
        "completed_at": sale.delivery_completed_at.astimezone(manaus_tz).strftime("%d/%m %H:%M") if sale.delivery_completed_at else "",
        "items": [{
            "name": item.product.name if item.product else "Produto",
            "size": item.variation.size if item.variation else "",
            "color": item.variation.color if item.variation and item.variation.color else "",
            "quantity": item.quantity,
            "price": item.unit_price
        } for item in sale.items]
    }

@app.get("/api/delivery/drivers")
def list_delivery_drivers(request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    drivers = db.query(DeliveryDriver).filter(DeliveryDriver.company_id == cid, DeliveryDriver.active == True).order_by(DeliveryDriver.name.asc()).all()
    return [{"id": d.id, "name": d.name, "phone": d.phone, "vehicle": d.vehicle or ""} for d in drivers]

@app.post("/api/delivery/drivers")
def create_delivery_driver(data: DeliveryDriverSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    phone = "".join(filter(str.isdigit, data.phone))
    if not data.name.strip() or not phone:
        return JSONResponse(status_code=400, content={"error": "Nome e WhatsApp obrigatorios."})
    existing = db.query(DeliveryDriver).filter(DeliveryDriver.company_id == cid, DeliveryDriver.phone == phone, DeliveryDriver.active == True).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Entregador ja cadastrado."})
    driver = DeliveryDriver(company_id=cid, name=data.name.strip(), phone=phone, vehicle=(data.vehicle or "").strip() or None)
    db.add(driver); db.commit()
    return {"success": True, "id": driver.id}

@app.delete("/api/delivery/drivers/{driver_id}")
def delete_delivery_driver(driver_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    driver = db.query(DeliveryDriver).filter(DeliveryDriver.id == driver_id, DeliveryDriver.company_id == cid, DeliveryDriver.active == True).first()
    if not driver: return JSONResponse(status_code=404, content={"error": "Entregador nao encontrado."})
    driver.active = False; db.commit()
    return {"success": True}

@app.get("/api/delivery/orders")
def list_delivery_orders(request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sales = db.query(Sale).filter(
        Sale.company_id == cid, Sale.delivery_type == "delivery", Sale.status != "cancelled"
    ).order_by(
        case((Sale.delivery_status == "delivered", 1), else_=0),
        Sale.date.desc()
    ).limit(80).all()
    return [delivery_order_payload(s) for s in sales]

@app.post("/api/delivery/orders/{sale_id}/assign")
def assign_delivery_driver(sale_id: int, data: DeliveryAssignSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale: return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    driver = db.query(DeliveryDriver).filter(DeliveryDriver.id == data.driver_id, DeliveryDriver.company_id == cid, DeliveryDriver.active == True).first()
    if not driver: return JSONResponse(status_code=404, content={"error": "Entregador nao encontrado."})
    now = get_manaus_time()
    sale.delivery_driver_id = driver.id
    sale.delivery_status = "assigned"
    sale.delivery_assigned_at = now
    db.commit(); db.refresh(sale)
    sent = False
    if data.send_receipt:
        msg = f"*ENTREGA #{sale.id}*\n\nCliente: {sale.customer.name if sale.customer else 'Consumidor'}\nTotal: R$ {sale.total_amount:.2f}\nPagamento: {sale.payment_method}\n\n*Endereco:*\n{sale.delivery_address or '-'}\nReferencia: {sale.delivery_reference or '-'}"
        sent = whatsapp_manager.send_message(cid, driver.phone, msg)
    return {"success": True, "sent": sent, "order": delivery_order_payload(sale)}

@app.post("/api/delivery/orders/{sale_id}/send-driver")
def send_delivery_receipt_to_driver(sale_id: int, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale: return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    if not sale.delivery_driver: return JSONResponse(status_code=400, content={"error": "Atribua um entregador primeiro."})
    msg = f"*ENTREGA #{sale.id}*\n\nCliente: {sale.customer.name if sale.customer else 'Consumidor'}\nTotal: R$ {sale.total_amount:.2f}\nPagamento: {sale.payment_method}\n\n*Endereco:*\n{sale.delivery_address or '-'}\nReferencia: {sale.delivery_reference or '-'}"
    sent = whatsapp_manager.send_message(cid, sale.delivery_driver.phone, msg)
    return {"success": True, "sent": sent}

@app.post("/api/delivery/orders/{sale_id}/status")
def update_delivery_status(sale_id: int, data: DeliveryStatusSchema, request: Request, db: Session = Depends(get_db)):
    cid = int(request.session.get("company_id"))
    if data.status not in DELIVERY_STATUSES:
        return JSONResponse(status_code=400, content={"error": "Status invalido."})
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.company_id == cid, Sale.delivery_type == "delivery").first()
    if not sale: return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado."})
    now = get_manaus_time()
    sale.delivery_status = data.status
    if data.status == "assigned" and not sale.delivery_assigned_at: sale.delivery_assigned_at = now
    elif data.status == "out_for_delivery": sale.delivery_dispatched_at = now
    elif data.status == "delivered": sale.delivery_completed_at = now
    db.commit()
    return {"success": True, "order": delivery_order_payload(sale)}

# --- CUPONS DE DESCONTO ---

class CouponCreate(BaseModel):
    code: str
    discount_type: str = "percent"
    discount_value: float = 10.0
    max_uses: int = 0
    valid_until: Optional[str] = None

@app.get("/api/coupons")
def list_coupons(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    coupons = db.query(Coupon).filter(Coupon.company_id == cid, Coupon.active == True).order_by(Coupon.created_at.desc()).all()
    return [{
        "id": c.id, "code": c.code,
        "discount_type": c.discount_type, "discount_value": c.discount_value,
        "max_uses": c.max_uses, "current_uses": c.current_uses,
        "valid_until": c.valid_until.astimezone(manaus_tz).strftime("%d/%m/%Y") if c.valid_until else None
    } for c in coupons]

@app.post("/api/coupons")
def create_coupon(data: CouponCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    code = data.code.strip().upper()
    if not code: return JSONResponse(status_code=400, content={"error": "Codigo obrigatorio."})
    existing = db.query(Coupon).filter(Coupon.company_id == cid, Coupon.code == code, Coupon.active == True).first()
    if existing: return JSONResponse(status_code=400, content={"error": "Cupom ja existe."})
    valid_dt = None
    if data.valid_until:
        try: valid_dt = datetime.fromisoformat(data.valid_until).replace(tzinfo=manaus_tz)
        except: pass
    coupon = Coupon(company_id=cid, code=code, discount_type=data.discount_type,
                    discount_value=data.discount_value, max_uses=data.max_uses, valid_until=valid_dt)
    db.add(coupon); db.commit()
    return {"success": True, "id": coupon.id}

@app.delete("/api/coupons/{coupon_id}")
def delete_coupon(coupon_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.company_id == cid).first()
    if not coupon: return JSONResponse(status_code=404, content={"error": "Cupom nao encontrado."})
    coupon.active = False; db.commit()
    return {"success": True}

@app.post("/api/coupons/validate")
def validate_coupon(request: Request, db: Session = Depends(get_db), code: str = ""):
    cid = request.session.get("company_id")
    code = code.strip().upper()
    coupon = db.query(Coupon).filter(Coupon.company_id == cid, Coupon.code == code, Coupon.active == True).first()
    if not coupon: return JSONResponse(status_code=404, content={"error": "Cupom invalido."})
    if coupon.max_uses > 0 and coupon.current_uses >= coupon.max_uses:
        return JSONResponse(status_code=400, content={"error": "Cupom esgotado."})
    if coupon.valid_until and get_manaus_time() > coupon.valid_until:
        return JSONResponse(status_code=400, content={"error": "Cupom expirado."})
    return {
        "valid": True, "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value, "code": coupon.code
    }

# --- PROGRAMA DE FIDELIDADE ---

class LoyaltyConfigCreate(BaseModel):
    points_per_real: float = 1.0
    redemption_threshold: int = 100
    redemption_value: float = 10.0
    active: bool = True

@app.get("/api/loyalty/config")
def get_loyalty_config(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid).first()
    if not config: return {"active": False, "points_per_real": 1.0, "redemption_threshold": 100, "redemption_value": 10.0}
    return {
        "active": config.active, "points_per_real": config.points_per_real,
        "redemption_threshold": config.redemption_threshold, "redemption_value": config.redemption_value
    }

@app.post("/api/loyalty/config")
def save_loyalty_config(data: LoyaltyConfigCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid).first()
    if not config:
        config = LoyaltyConfig(company_id=cid)
        db.add(config)
    config.points_per_real = data.points_per_real
    config.redemption_threshold = data.redemption_threshold
    config.redemption_value = data.redemption_value
    config.active = data.active
    db.commit()
    return {"success": True}

@app.post("/api/loyalty/redeem")
def redeem_loyalty_points(request: Request, db: Session = Depends(get_db), customer_id: int = 0):
    cid = request.session.get("company_id")
    config = db.query(LoyaltyConfig).filter(LoyaltyConfig.company_id == cid, LoyaltyConfig.active == True).first()
    if not config: return JSONResponse(status_code=400, content={"error": "Programa de fidelidade nao ativo."})
    customer = db.query(Customer).filter(Customer.id == customer_id, Customer.company_id == cid).first()
    if not customer: return JSONResponse(status_code=404, content={"error": "Cliente nao encontrado."})
    if (customer.loyalty_points or 0) < config.redemption_threshold:
        return JSONResponse(status_code=400, content={"error": f"Pontos insuficientes. Necessario: {config.redemption_threshold}, Atual: {customer.loyalty_points or 0}"})
    customer.loyalty_points -= config.redemption_threshold
    db.commit()
    return {"success": True, "discount": config.redemption_value, "remaining_points": customer.loyalty_points}

# --- CATALOGO ONLINE PUBLICO ---

@app.get("/catalogo/{slug}", response_class=HTMLResponse)
def public_catalog(slug: str, request: Request, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.slug == slug, Company.active == True).first()
    if not company: return HTMLResponse("<h1>Loja não encontrada</h1>", status_code=404)
    products = db.query(Product).filter(Product.company_id == company.id, Product.active == True, Product.show_on_whatsapp == True).all()
    
    prod_list = []
    for p in products:
        variations = [{"size": v.size, "color": v.color, "stock": v.stock_quantity, "price": v.price_override or p.base_price} for v in p.variations if v.stock_quantity > 0]
        if variations:
            prod_list.append({"name": p.name, "description": p.description or "", "price": p.base_price, "image": p.image_base64, "variations": variations})
    
    wa_num = company.whatsapp_number or ""
    wa_link = f"https://wa.me/{wa_num}" if wa_num else "#"
    
    prods_html = ""
    for p in prod_list:
        img = f'<img src="{p["image"]}" style="width:100%;height:220px;object-fit:cover;border-radius:12px 12px 0 0;">' if p["image"] else '<div style="height:220px;background:#1e293b;border-radius:12px 12px 0 0;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:3rem;">👗</div>'
        sizes = ", ".join(set(v["size"] for v in p["variations"]))
        msg_text = f"Oi! Tenho interesse no produto *{p['name']}*"
        prods_html += f'''
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
        </div>'''
    
    html = f'''<!DOCTYPE html>
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
    </body></html>'''
    return HTMLResponse(html)

@app.post("/api/config/slug")
def save_company_slug(request: Request, db: Session = Depends(get_db), slug: str = ""):
    cid = request.session.get("company_id")
    slug = slug.strip().lower().replace(" ", "-")
    if not slug: return JSONResponse(status_code=400, content={"error": "Slug obrigatorio."})
    existing = db.query(Company).filter(Company.slug == slug, Company.id != cid).first()
    if existing: return JSONResponse(status_code=400, content={"error": "Este slug ja esta em uso."})
    company = db.query(Company).filter(Company.id == cid).first()
    company.slug = slug; db.commit()
    return {"success": True, "url": f"/catalogo/{slug}"}

# --- VENDEDORES E COMISSÕES ---

class SellerCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    commission_rate: float = 0.0

@app.get("/api/sellers")
def list_sellers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sellers = db.query(Seller).filter(Seller.company_id == cid, Seller.active == True).all()
    return [{"id": s.id, "name": s.name, "phone": s.phone, "commission_rate": s.commission_rate} for s in sellers]

@app.post("/api/sellers")
def create_seller(data: SellerCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    seller = Seller(company_id=cid, name=data.name, phone=data.phone, commission_rate=data.commission_rate)
    db.add(seller); db.commit()
    return {"success": True, "id": seller.id}

@app.delete("/api/sellers/{seller_id}")
def delete_seller(seller_id: int, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    seller = db.query(Seller).filter(Seller.id == seller_id, Seller.company_id == cid).first()
    if not seller: return JSONResponse(status_code=404, content={"error": "Vendedor nao encontrado."})
    seller.active = False; db.commit()
    return {"success": True}

# --- FORNECEDORES E COMPRAS ---

class SupplierCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    cnpj: Optional[str] = None
    notes: Optional[str] = None

@app.get("/api/suppliers")
def list_suppliers(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    suppliers = db.query(Supplier).filter(Supplier.company_id == cid, Supplier.active == True).all()
    return [{"id": sup.id, "name": sup.name, "phone": sup.phone, "email": sup.email, "cnpj": sup.cnpj} for sup in suppliers]

@app.post("/api/suppliers")
def create_supplier(data: SupplierCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    supplier = Supplier(company_id=cid, name=data.name, phone=data.phone, email=data.email, cnpj=data.cnpj, notes=data.notes)
    db.add(supplier); db.commit()
    return {"success": True, "id": supplier.id}

# --- TROCAS E DEVOLUÇÕES ---

class ExchangeCreate(BaseModel):
    sale_id: int
    items: List[dict] # [{"variation_id": 1, "qty": 1}]
    type: str = "exchange" # exchange, refund
    reason: str = ""
    refund_amount: float = 0.0

@app.post("/api/exchanges")
def register_exchange(data: ExchangeCreate, request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    sale = db.query(Sale).filter(Sale.id == data.sale_id, Sale.company_id == cid).first()
    if not sale: return JSONResponse(status_code=404, content={"error": "Venda nao encontrada."})
    
    # Restaura estoque dos itens devolvidos
    for item in data.items:
        var = db.query(ProductVariation).filter(ProductVariation.id == item["variation_id"]).first()
        if var:
            var.stock_quantity += item["qty"]
            # Log de estoque
            log = StockTransaction(variation_id=var.id, type="entry", quantity=item["qty"], description=f"Devolucao/Troca ref. Venda #{sale.id}")
            db.add(log)
    
    exchange = Exchange(
        company_id=cid, sale_id=data.sale_id, 
        items_json=json.dumps(data.items), type=data.type, 
        reason=data.reason, refund_amount=data.refund_amount
    )
    db.add(exchange)
    
    if data.type == "refund" and data.refund_amount > 0:
        tx = Transaction(company_id=cid, type="expense", amount=data.refund_amount, description=f"Reembolso Venda #{sale.id}", payment_method="OUTRO")
        db.add(tx)
        
    db.commit()
    return {"success": True}

# --- ALERTAS E ETIQUETAS ---

@app.get("/api/reports/low-stock")
def get_low_stock(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    # Busca variacoes com estoque <= 3
    items = db.query(ProductVariation).join(Product).filter(Product.company_id == cid, ProductVariation.stock_quantity <= 3, Product.active == True).all()
    return [{"id": i.id, "product": i.product.name, "size": i.size, "color": i.color, "stock": i.stock_quantity} for i in items]

@app.get("/api/products/labels")
def generate_labels(product_id: int, qty: int = 1, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product: return HTMLResponse("Produto nao encontrado", status_code=404)
    
    label_html = f"""
    <div style="width: 4cm; height: 2.5cm; border: 1px solid #ccc; padding: 5px; margin: 5px; float: left; font-family: sans-serif; text-align: center;">
        <div style="font-size: 10px; font-weight: bold; margin-bottom: 3px;">{product.company.name if product.company else 'FASHION ERP'}</div>
        <div style="font-size: 12px; margin-bottom: 5px;">{product.name}</div>
        <div style="font-size: 16px; font-weight: 800; color: #000;">R$ {product.base_price:.2f}</div>
        <div style="font-size: 8px; margin-top: 5px;">ID: {product.id} | FASHION ERP</div>
    </div>
    """
    full_html = f"<html><body><div style='display:flex; flex-wrap:wrap;'>{''.join([label_html for _ in range(qty)])}</div><script>window.print()</script></body></html>"
    return HTMLResponse(full_html)

# --- FASE 3: METAS E FINANCEIRO ---

@app.get("/api/goals/current")
def get_current_goal(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    now = get_manaus_time()
    goal = db.query(SalesGoal).filter(
        SalesGoal.company_id == cid,
        SalesGoal.month == now.month,
        SalesGoal.year == now.year
    ).first()
    
    # Progresso atual
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    current_revenue = db.query(func.sum(Sale.total_amount)).filter(
        Sale.company_id == cid,
        Sale.date >= month_start,
        Sale.status != "cancelled"
    ).scalar() or 0.0
    
    return {
        "target": goal.target_value if goal else 0.0,
        "current": current_revenue,
        "percent": (current_revenue / goal.target_value * 100) if goal and goal.target_value > 0 else 0
    }

@app.post("/api/goals")
def save_goal(request: Request, data: SalesGoalCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    goal = db.query(SalesGoal).filter(
        SalesGoal.company_id == cid,
        SalesGoal.month == data.month,
        SalesGoal.year == data.year
    ).first()
    
    if goal:
        goal.target_value = data.target_value
    else:
        goal = SalesGoal(
            company_id=cid,
            month=data.month,
            year=data.year,
            target_value=data.target_value
        )
        db.add(goal)
    
    db.commit()
    return {"success": True}

@app.get("/api/reports/financial")
def get_financial_report(request: Request, start_date: str, end_date: str, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    # Filtro de data
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    
    sales = db.query(Sale).filter(
        Sale.company_id == cid,
        Sale.date >= s_dt,
        Sale.date <= e_dt,
        Sale.status != "cancelled"
    ).all()
    
    gross_revenue = sum(s.total_amount for s in sales)
    total_discounts = sum(s.discount for s in sales)
    total_fees = sum(s.delivery_fee for s in sales)
    
    total_cost = 0.0
    total_commissions = 0.0
    
    for s in sales:
        # Soma custos dos itens
        for item in s.items:
            total_cost += (item.cost_price or 0.0) * item.quantity
        
        # Calcula comissão se houver vendedor
        if s.seller_id and s.seller:
            total_commissions += (s.total_amount * (s.seller.commission_rate / 100))
            
    # SOMA DESPESAS DO PERÍODO
    expenses = db.query(Expense).filter(Expense.company_id == cid, Expense.date >= s_dt, Expense.date <= e_dt).all()
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
        "margin": (net_profit / gross_revenue * 100) if gross_revenue > 0 else 0
    }

@app.get("/api/reports/commissions")
def get_commissions_report(request: Request, start_date: str, end_date: str, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    
    sellers = db.query(Seller).filter(Seller.company_id == cid).all()
    report = []
    
    for sel in sellers:
        sales = db.query(Sale).filter(
            Sale.seller_id == sel.id,
            Sale.date >= s_dt,
            Sale.date <= e_dt,
            Sale.status != "cancelled"
        ).all()
        
        total_sold = sum(s.total_amount for s in sales)
        commission = total_sold * (sel.commission_rate / 100)
        
        report.append({
            "id": sel.id,
            "name": sel.name,
            "rate": sel.commission_rate,
            "count": len(sales),
            "total_sold": total_sold,
            "commission": commission
        })
        
    return report

@app.get("/api/expenses")
def get_expenses(request: Request, start_date: str, end_date: str, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    s_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0)
    e_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    return db.query(Expense).filter(Expense.company_id == cid, Expense.date >= s_dt, Expense.date <= e_dt).all()

@app.post("/api/expenses")
def create_expense(request: Request, data: ExpenseCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    dt = datetime.fromisoformat(data.date).replace(tzinfo=manaus_tz)
    expense = Expense(
        company_id=cid,
        category=data.category,
        description=data.description,
        amount=data.amount,
        date=dt
    )
    db.add(expense)
    db.commit()
    return {"success": True}

@app.delete("/api/expenses/{id}")
def delete_expense(request: Request, id: int, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    exp = db.query(Expense).filter(Expense.id == id, Expense.company_id == cid).first()
    if exp:
        db.delete(exp)
        db.commit()
    return {"success": True}

@app.get("/api/users")
def get_company_users(request: Request, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    users = db.query(User).filter(User.company_id == cid).all()
    return [{"id": u.id, "username": u.username, "role": u.role} for u in users]

@app.post("/api/users")
def create_company_user(request: Request, data: UserCreate, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    # Verifica se já existe
    existing = db.query(User).filter(User.username == data.username.strip().lower()).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Usuário já existe."})
    
    new_user = User(
        company_id=cid,
        username=data.username.strip().lower(),
        hashed_password=pwd_context.hash(data.password),
        role=data.role
    )
    db.add(new_user)
    db.commit()
    return {"success": True}

@app.delete("/api/users/{id}")
def delete_company_user(request: Request, id: int, db: Session = Depends(get_db)):
    cid = request.session.get("company_id")
    user = db.query(User).filter(User.id == id, User.company_id == cid).first()
    if user:
        db.delete(user)
        db.commit()
    return {"success": True}

# --- HEALTH CHECK ---

@app.get("/health")
def health():
    return {"status": "ok", "app": "Fashion ERP"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
