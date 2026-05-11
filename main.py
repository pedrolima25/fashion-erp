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
    populate_initial_data, ScheduledCampaign, DailyCash, DeliveryDriver, StockTransaction
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
        
    return templates.TemplateResponse(request, "dashboard.html", {"request": request, "active_page": "dashboard"})

@app.get("/vendas", response_class=HTMLResponse)
def sales_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "vendas.html", {"request": request, "active_page": "sales"})

@app.get("/produtos", response_class=HTMLResponse)
def products_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "produtos.html", {"request": request, "active_page": "products"})

@app.get("/estoque", response_class=HTMLResponse)
def inventory_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "estoque.html", {"request": request, "active_page": "inventory"})

@app.get("/clientes", response_class=HTMLResponse)
def customers_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "clientes.html", {"request": request, "active_page": "customers"})

@app.get("/financeiro", response_class=HTMLResponse)
def finance_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "financeiro.html", {"request": request, "active_page": "finance"})

@app.get("/marketing", response_class=HTMLResponse)
def marketing_page(request: Request, db: Session = Depends(get_db)):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    cid = request.session.get("company_id")
    company = db.query(Company).filter(Company.id == cid).first()
    return templates.TemplateResponse(request, "marketing.html", {"request": request, "active_page": "marketing", "company": company})

@app.get("/configuracoes", response_class=HTMLResponse)
def settings_page(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "configuracoes.html", {"request": request, "active_page": "settings"})

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
        db.commit()
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
        "delivery_mode": comp.delivery_mode or "fixed"
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

# --- HEALTH CHECK ---

@app.get("/health")
def health():
    return {"status": "ok", "app": "Fashion ERP"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
