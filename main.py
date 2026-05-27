import sys
import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, case
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from database import (
    Base, engine, SessionLocal,
    get_db, get_manaus_time, manaus_tz,
    User, Company, Subscription, pwd_context,
    Customer, Sale, SaleItem, Product, ProductVariation,
    run_migrations, populate_initial_data,
    ScheduledCampaign, LoginAttempt, ChatSession, AuditLog,
)
from ai_logic import process_message
from whatsapp_service import whatsapp_manager

from routers import (
    products, sales, customers, inventory, financial,
    delivery, whatsapp, marketing, reports, settings, sellers, master, catalog,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- RATE LIMIT CONFIG ---
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_MINUTES = 3


# --- BACKGROUND TASKS ---

async def self_ping_task():
    await asyncio.sleep(60)
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
            await asyncio.sleep(300)


async def whatsapp_keeper_task():
    while True:
        try:
            await asyncio.sleep(600)
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
                    await asyncio.sleep(30)
    except Exception as e:
        logger.error(f"❌ [Auto-Connect] Falha na fila de conexão: {e}")


async def marketing_worker_task():
    while True:
        try:
            await asyncio.sleep(60)
            now = get_manaus_time()
            with SessionLocal() as db:
                campaigns = db.query(ScheduledCampaign).filter(
                    ScheduledCampaign.status == "pending",
                    ScheduledCampaign.scheduled_at <= now,
                ).all()
                for camp in campaigns:
                    logger.info(f"🚀 [Marketing Worker] Iniciando campanha {camp.id} para empresa {camp.company_id}")
                    camp.status = "sending"
                    db.commit()
                    try:
                        jids = json.loads(camp.group_jids)
                        pids = json.loads(camp.product_ids)
                        for jid in jids:
                            if camp.opening_msg:
                                await asyncio.to_thread(whatsapp_manager.send_message, camp.company_id, jid, camp.opening_msg)
                                await asyncio.sleep(2)
                        for pid in pids:
                            prod = db.query(Product).filter(Product.id == pid).first()
                            if prod:
                                company = db.query(Company).filter(Company.id == camp.company_id).first()
                                comp_name = company.name.upper() if company else "NOVIDADE"
                                msg = f"✨ *{comp_name}* ✨\n\n👗 {prod.name}\n\n💰 *VALOR:* R$ {prod.base_price:.2f}"
                                if prod.description:
                                    msg += f"\n\n📝 {prod.description}"
                                for jid in jids:
                                    await asyncio.to_thread(whatsapp_manager.send_image, camp.company_id, jid, prod.image_base64, msg)
                                    await asyncio.sleep(4)
                                if camp.post_to_status:
                                    await asyncio.to_thread(whatsapp_manager.send_status_image, camp.company_id, prod.image_base64, msg)
                                    await asyncio.sleep(3)
                        await asyncio.sleep(3)
                        if camp.frequency == "daily":
                            camp.scheduled_at = camp.scheduled_at + timedelta(days=1)
                            camp.status = "pending"
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
    while True:
        try:
            logger.info("🤖 [CRM Worker] Verificando aniversariantes e clientes inativos...")
            now = get_manaus_time()
            today_str = now.strftime("%d/%m")
            with SessionLocal() as db:
                companies = db.query(Company).filter(Company.active == True).all()
                for co in companies:
                    bday_customers = db.query(Customer).filter(
                        Customer.company_id == co.id,
                        Customer.birthday.like(f"{today_str}%"),
                    ).all()
                    for cust in bday_customers:
                        msg = f"🎉 Parabéns, *{cust.name}*! 🎂\n\nA equipe da *{co.name}* deseja um dia incrível! Como presente, você ganhou um desconto especial em sua próxima compra. Use o cupom: *PARABENS10* 🎈"
                        await asyncio.to_thread(whatsapp_manager.send_message, co.id, f"{cust.phone}@s.whatsapp.net", msg)
                        await asyncio.sleep(2)
                    forty_five_days_ago = now - timedelta(days=45)
                    inactive_customers = db.query(Customer).filter(Customer.company_id == co.id).all()
                    for cust in inactive_customers:
                        last_sale = db.query(Sale).filter(Sale.customer_id == cust.id).order_by(Sale.date.desc()).first()
                        if last_sale and last_sale.date < forty_five_days_ago:
                            msg = f"Oi *{cust.name}*! 🤗 Sentimos sua falta aqui na *{co.name}*.\n\nFaz um tempinho que você não nos visita... Que tal conferir as novidades da semana? 👗✨"
                            await asyncio.to_thread(whatsapp_manager.send_message, co.id, f"{cust.phone}@s.whatsapp.net", msg)
                            await asyncio.sleep(2)
            await asyncio.sleep(86400)
        except Exception as e:
            logger.error(f"❌ [CRM Worker] Erro: {e}")
            await asyncio.sleep(3600)


async def cleanup_task():
    """Remove sessões de chat expiradas e tentativas de login antigas."""
    while True:
        try:
            await asyncio.sleep(3600)
            cutoff_chat = get_manaus_time() - timedelta(hours=24)
            cutoff_login = get_manaus_time() - timedelta(hours=1)
            with SessionLocal() as db:
                deleted_chat = db.query(ChatSession).filter(ChatSession.updated_at < cutoff_chat).delete()
                deleted_login = db.query(LoginAttempt).filter(LoginAttempt.attempted_at < cutoff_login).delete()
                db.commit()
                if deleted_chat or deleted_login:
                    logger.info(f"🧹 [Cleanup] Removidas {deleted_chat} sessões e {deleted_login} tentativas de login expiradas.")
        except Exception as e:
            logger.error(f"❌ [Cleanup] Erro: {e}")


async def low_stock_alert_task():
    """Envia alerta diário via WhatsApp quando estoque de variações está baixo (≤3)."""
    await asyncio.sleep(3600)  # espera 1h após startup
    while True:
        try:
            with SessionLocal() as db:
                companies = db.query(Company).filter(Company.active == True).all()
                for co in companies:
                    low = (
                        db.query(ProductVariation)
                        .join(Product)
                        .filter(
                            Product.company_id == co.id,
                            Product.active == True,
                            ProductVariation.stock_quantity <= 3,
                            ProductVariation.stock_quantity >= 0,
                        )
                        .all()
                    )
                    if low and co.whatsapp_number:
                        lines = "\n".join(
                            f"• {v.product.name} ({v.size or '-'}): *{v.stock_quantity}* unid."
                            for v in low[:15]
                        )
                        msg = f"⚠️ *Alerta de Estoque Baixo — {co.name}*\n\n{lines}"
                        whatsapp_manager.send_message(co.id, co.whatsapp_number, msg)
                        logger.info(f"📦 [Estoque] Alerta enviado para {co.name} ({len(low)} itens)")
        except Exception as e:
            logger.error(f"❌ [Estoque Alert] Erro: {e}")
        await asyncio.sleep(86400)  # repete a cada 24h


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando Fashion ERP Pro...")
    try:
        Base.metadata.create_all(bind=engine)
        run_migrations()
        with SessionLocal() as session:
            populate_initial_data(session)
        logger.info("✅ Banco de dados sincronizado e pronto!")
    except Exception as db_err:
        logger.warning(f"⚠️ Aviso na sincronização do banco (pode ser concorrência): {db_err}")

    asyncio.create_task(self_ping_task())
    asyncio.create_task(whatsapp_keeper_task())
    asyncio.create_task(marketing_worker_task())
    asyncio.create_task(crm_worker_task())
    asyncio.create_task(auto_connect_whatsapp_task())
    asyncio.create_task(cleanup_task())
    asyncio.create_task(low_stock_alert_task())

    yield
    logger.info("Desligando Fashion ERP...")


app = FastAPI(title="Fashion ERP & WhatsApp Bot", lifespan=lifespan)

_secret_key = os.environ.get("SECRET_KEY", "dev_insecure_key_change_in_production")
app.add_middleware(SessionMiddleware, secret_key=_secret_key)

_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# --- ROUTERS ---
app.include_router(products.router)
app.include_router(sales.router)
app.include_router(customers.router)
app.include_router(inventory.router)
app.include_router(financial.router)
app.include_router(delivery.router)
app.include_router(whatsapp.router)
app.include_router(marketing.router)
app.include_router(reports.router)
app.include_router(settings.router)
app.include_router(sellers.router)
app.include_router(master.router)
app.include_router(catalog.router)


# --- AUTH ---

@app.get("/")
def root():
    return RedirectResponse(url="/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    window_start = get_manaus_time() - timedelta(minutes=LOGIN_WINDOW_MINUTES)

    failed_count = db.query(LoginAttempt).filter(
        LoginAttempt.ip == client_ip,
        LoginAttempt.attempted_at >= window_start,
        LoginAttempt.success == False,
    ).count()

    if failed_count >= LOGIN_MAX_ATTEMPTS:
        return JSONResponse(
            status_code=429,
            content={"success": False, "error": f"Muitas tentativas. Tente novamente em {LOGIN_WINDOW_MINUTES} minutos."},
        )

    username_clean = username.strip().lower()
    user = db.query(User).filter(User.username == username_clean).first()
    success = bool(user and pwd_context.verify(password, user.hashed_password))

    db.add(LoginAttempt(ip=client_ip, attempted_at=get_manaus_time(), success=success))
    db.commit()

    if not success:
        return JSONResponse(status_code=401, content={"success": False, "error": "Usuário ou senha incorretos."})

    if not user.is_master:
        company = db.query(Company).filter(Company.id == user.company_id).first()
        if not company or not company.active:
            return JSONResponse(status_code=403, content={"success": False, "error": "Unidade inativa."})
        sub = db.query(Subscription).filter(Subscription.company_id == company.id).order_by(Subscription.end_date.desc()).first()
        if sub and sub.end_date:
            grace_date = sub.end_date + timedelta(days=3)
            if get_manaus_time().date() > grace_date.date():
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "error": f"Assinatura vencida em {sub.end_date.strftime('%d/%m/%Y')}. Regularize para acessar."},
                )

    request.session["user"] = username_clean
    request.session["company_id"] = user.company_id
    request.session["is_master"] = user.is_master
    request.session["role"] = user.role or "admin"
    return {"success": True}


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


# --- PAGE ROUTES ---

def _auth_redirect(request: Request):
    if "user" not in request.session:
        return RedirectResponse(url="/login")
    return None


@app.get("/painel", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    if redir := _auth_redirect(request):
        return redir
    if request.session.get("is_master"):
        return templates.TemplateResponse(request, "master_admin.html", {"request": request})
    return templates.TemplateResponse(request, "dashboard.html", {"request": request, "active_page": "dashboard", "role": request.session.get("role")})


@app.get("/vendas", response_class=HTMLResponse)
def sales_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "vendas.html", {"request": request, "active_page": "sales", "role": request.session.get("role")})


@app.get("/produtos", response_class=HTMLResponse)
def products_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "produtos.html", {"request": request, "active_page": "products", "role": request.session.get("role")})


@app.get("/estoque", response_class=HTMLResponse)
def inventory_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "estoque.html", {"request": request, "active_page": "inventory", "role": request.session.get("role")})


@app.get("/clientes", response_class=HTMLResponse)
def customers_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "clientes.html", {"request": request, "active_page": "customers", "role": request.session.get("role")})


@app.get("/financeiro", response_class=HTMLResponse)
def finance_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "financeiro.html", {"request": request, "active_page": "finance", "role": request.session.get("role")})


@app.get("/marketing", response_class=HTMLResponse)
def marketing_page(request: Request, db: Session = Depends(get_db)):
    if redir := _auth_redirect(request):
        return redir
    cid = request.session.get("company_id")
    company = db.query(Company).filter(Company.id == cid).first()
    return templates.TemplateResponse(request, "marketing.html", {"request": request, "active_page": "marketing", "company": company, "role": request.session.get("role")})


@app.get("/provador", response_class=HTMLResponse)
def view_provador(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "provador_medidas.html", {"request": request, "active_page": "provador"})


@app.get("/provador-ar", response_class=HTMLResponse)
def view_provador_ar(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "ar_tryon.html", {"request": request, "active_page": "provador"})


@app.get("/configuracoes", response_class=HTMLResponse)
def settings_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "configuracoes.html", {"request": request, "active_page": "settings", "role": request.session.get("role")})


@app.get("/entregas", response_class=HTMLResponse)
def deliveries_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "entregas.html", {"request": request, "active_page": "deliveries"})


@app.get("/relatorios", response_class=HTMLResponse)
def reports_page(request: Request):
    if redir := _auth_redirect(request):
        return redir
    return templates.TemplateResponse(request, "relatorios.html", {"request": request, "active_page": "reports"})


# --- DASHBOARD API ---

@app.get("/api/dashboard/stats")
def get_dashboard_stats(request: Request, db: Session = Depends(get_db)):
    company_id = request.session.get("company_id")
    if not company_id:
        return {}
    company_id = int(company_id)
    today_start = get_manaus_time().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    vendas_hoje = db.query(func.sum(Sale.total_amount)).filter(
        Sale.company_id == company_id, Sale.date >= today_start, Sale.date < today_end,
    ).scalar() or 0.0
    total_produtos = db.query(Product).filter(Product.company_id == company_id, Product.active == True).count()
    total_clientes = db.query(Customer).filter(Customer.company_id == company_id).count()
    pedidos_pendentes = db.query(Sale).filter(Sale.company_id == company_id, Sale.status == "pending").count()
    recentes = (
        db.query(Sale)
        .options(selectinload(Sale.customer))
        .filter(Sale.company_id == company_id)
        .order_by(case((Sale.status == "pending", 0), else_=1), Sale.date.desc())
        .limit(20)
        .all()
    )
    vendas_list = [
        {
            "id": r.id,
            "data": r.date.astimezone(manaus_tz).strftime("%H:%M"),
            "cliente": r.customer.name if r.customer else "Final Consumidor",
            "total": r.total_amount,
            "pagamento": r.payment_method,
            "logistica": r.delivery_type if r.delivery_type else "Presencial",
            "endereco": r.delivery_address if r.delivery_address else "",
            "status": r.status,
        }
        for r in recentes
    ]
    return {
        "vendas_hoje": vendas_hoje,
        "total_produtos": total_produtos,
        "total_clientes": total_clientes,
        "pedidos_pendentes": pedidos_pendentes,
        "vendas_recentes": vendas_list,
        "last_sale_id": recentes[0].id if recentes else 0,
    }


@app.get("/api/stats/sales-chart")
def get_sales_chart_data(request: Request, db: Session = Depends(get_db)):
    company_id = request.session.get("company_id")
    if not company_id:
        return []
    res = []
    for i in range(6, -1, -1):
        day = get_manaus_time() - timedelta(days=i)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        total = db.query(func.sum(Sale.total_amount)).filter(
            Sale.company_id == company_id,
            Sale.date >= start, Sale.date < end,
            Sale.status != "cancelled",
        ).scalar() or 0.0
        res.append({"label": day.strftime("%d/%m"), "value": total})
    return res


# --- WHATSAPP WEBHOOK ---

_WPP_WEBHOOK_SECRET = os.environ.get("WPP_WEBHOOK_SECRET", "")


@app.post("/webhook/whatsapp/{company_id}")
async def whatsapp_webhook(company_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        raw_body = await request.body()

        if _WPP_WEBHOOK_SECRET:
            sig_header = request.headers.get("X-Webhook-Signature", "")
            expected = hmac.new(_WPP_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                logger.warning(f"❌ Webhook rejeitado (assinatura inválida) empresa={company_id}")
                return JSONResponse(status_code=403, content={"error": "Assinatura inválida"})

        body = json.loads(raw_body)
        message = body.get("message", "")
        sender = body.get("sender", "")
        if not message or not sender:
            return {"ok": True}
        response_text = process_message(company_id, sender, message, db)
        if response_text:
            whatsapp_manager.send_message(company_id, sender, response_text)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return {"ok": True}


# --- HEALTH ---

@app.get("/health")
def health():
    return {"status": "ok", "app": "Fashion ERP"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
