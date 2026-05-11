import os
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Boolean, UniqueConstraint, text, inspect, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from passlib.context import CryptContext
import logging

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
load_dotenv()

# Prioriza DATABASE_PUBLIC_URL (Railway) sobre DATABASE_URL
DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")

if not DATABASE_URL or DATABASE_URL.strip() == "":
    logger.info("💻 Usando SQLite local para Fashion ERP.")
    DATABASE_URL = "sqlite:///./fashion.db"
else:
    DATABASE_URL = DATABASE_URL.strip()
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if "postgresql" in DATABASE_URL and "sslmode" not in DATABASE_URL:
        DATABASE_URL += "&sslmode=require" if "?" in DATABASE_URL else "?sslmode=require"

if "sqlite" in DATABASE_URL:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

manaus_tz = pytz.timezone('America/Manaus')

def get_manaus_time():
    return datetime.now(manaus_tz)

# --- MODELS CORE (SaaS) ---

class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    category = Column(String, default="Loja de Roupas")
    active = Column(Boolean, default=True)
    is_archived = Column(Boolean, default=False)
    
    whatsapp_number = Column(String, nullable=True)
    pix_key = Column(String, nullable=True)
    mercado_pago_token = Column(String, nullable=True)
    
    logo_base64 = Column(Text, nullable=True)
    address = Column(Text, nullable=True)
    location_link = Column(Text, nullable=True)
    delivery_fee = Column(Float, default=0.0)
    delivery_mode = Column(String, default="fixed")
    slug = Column(String, nullable=True, unique=True, index=True)
    monthly_price = Column(Float, default=80.0)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)
    
    products = relationship("Product", back_populates="company")
    users = relationship("User", back_populates="company")
    subscriptions = relationship("Subscription", back_populates="company")
    sales = relationship("Sale", back_populates="company")

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    plan_type = Column(String) # mensal, trimestral, anual
    start_date = Column(DateTime(timezone=True), default=get_manaus_time)
    end_date = Column(DateTime(timezone=True))
    status = Column(String, default="active")
    saas_reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    
    company = relationship("Company", back_populates="subscriptions")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_master = Column(Boolean, default=False)
    
    company = relationship("Company", back_populates="users")

# --- MODELS FASHION ERP ---

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, index=True)
    
    products = relationship("Product", back_populates="category")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    base_price = Column(Float, default=0.0)
    cost_price = Column(Float, default=0.0)
    image_base64 = Column(Text, nullable=True)
    active = Column(Boolean, default=True)
    show_on_whatsapp = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)
    
    company = relationship("Company", back_populates="products")
    category = relationship("Category", back_populates="products")
    variations = relationship("ProductVariation", back_populates="product", cascade="all, delete-orphan")

class ProductVariation(Base):
    __tablename__ = "product_variations"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    size = Column(String)
    color = Column(String, nullable=True)
    sku = Column(String, nullable=True, index=True)
    stock_quantity = Column(Integer, default=0)
    price_override = Column(Float, nullable=True)
    cost_price_override = Column(Float, nullable=True)
    
    product = relationship("Product", back_populates="variations")

class Neighborhood(Base):
    __tablename__ = "neighborhoods"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, index=True)
    fee = Column(Float, default=0.0)
    
    company = relationship("Company")

class DeliveryDriver(Base):
    __tablename__ = "delivery_drivers"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, index=True)
    phone = Column(String, index=True)
    vehicle = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)
    
    company = relationship("Company")

class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint('company_id', 'phone', name='ux_customer_company_phone'),)
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, default="Cliente")
    phone = Column(String, index=True)
    email = Column(String, nullable=True)
    birthday = Column(String, nullable=True)
    loyalty_points = Column(Integer, default=0)
    total_points_earned = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)

class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    total_amount = Column(Float, default=0.0)
    discount = Column(Float, default=0.0)
    delivery_type = Column(String, nullable=True)
    delivery_fee = Column(Float, default=0.0)
    delivery_address = Column(Text, nullable=True)
    delivery_reference = Column(Text, nullable=True)
    delivery_location_link = Column(Text, nullable=True)
    delivery_driver_id = Column(Integer, ForeignKey("delivery_drivers.id"), nullable=True)
    delivery_status = Column(String, default="waiting")
    delivery_assigned_at = Column(DateTime(timezone=True), nullable=True)
    delivery_dispatched_at = Column(DateTime(timezone=True), nullable=True)
    delivery_completed_at = Column(DateTime(timezone=True), nullable=True)
    payment_method = Column(String, default="PIX")
    status = Column(String, default="pending") # pending, paid, completed, cancelled
    date = Column(DateTime(timezone=True), default=get_manaus_time)
    seller_id = Column(Integer, ForeignKey("sellers.id"), nullable=True)
    
    company = relationship("Company", back_populates="sales")
    customer = relationship("Customer")
    items = relationship("SaleItem", back_populates="sale")
    delivery_driver = relationship("DeliveryDriver")
    seller = relationship("Seller")

class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    variation_id = Column(Integer, ForeignKey("product_variations.id"), nullable=True)
    quantity = Column(Integer, default=1)
    unit_price = Column(Float)
    cost_price = Column(Float, default=0.0)
    
    sale = relationship("Sale", back_populates="items")
    product = relationship("Product")
    variation = relationship("ProductVariation")

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    type = Column(String) # 'income' ou 'expense'
    amount = Column(Float)
    description = Column(String)
    payment_method = Column(String, default="PIX")
    date = Column(DateTime(timezone=True), default=get_manaus_time)

class StockTransaction(Base):
    __tablename__ = "stock_transactions"
    id = Column(Integer, primary_key=True, index=True)
    variation_id = Column(Integer, ForeignKey("product_variations.id"))
    type = Column(String) # 'entry', 'sale', 'adjustment'
    quantity = Column(Integer)
    description = Column(String, nullable=True)
    date = Column(DateTime(timezone=True), default=get_manaus_time)

class DailyCash(Base):
    __tablename__ = "daily_cash"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    status = Column(String, default="open")
    opening_balance = Column(Float, default=0.0)
    closing_balance = Column(Float, nullable=True)
    opened_at = Column(DateTime(timezone=True), default=get_manaus_time)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    date = Column(DateTime(timezone=True), default=get_manaus_time)

class ScheduledCampaign(Base):
    __tablename__ = "scheduled_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    group_jids = Column(Text)
    product_ids = Column(Text)
    opening_msg = Column(Text, nullable=True)
    scheduled_at = Column(DateTime(timezone=True))
    frequency = Column(String, default="once")
    post_to_status = Column(Boolean, default=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)

class Coupon(Base):
    __tablename__ = "coupons"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    code = Column(String, index=True)
    discount_type = Column(String, default="percent")
    discount_value = Column(Float, default=0.0)
    max_uses = Column(Integer, default=0)
    current_uses = Column(Integer, default=0)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)
    company = relationship("Company")

class LoyaltyConfig(Base):
    __tablename__ = "loyalty_configs"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), unique=True)
    points_per_real = Column(Float, default=1.0)
    redemption_threshold = Column(Integer, default=100)
    redemption_value = Column(Float, default=10.0)
    active = Column(Boolean, default=True)

class Seller(Base):
    """Vendedores e Comissões"""
    __tablename__ = "sellers"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, index=True)
    phone = Column(String, nullable=True)
    commission_rate = Column(Float, default=0.0)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)

class Supplier(Base):
    """Fornecedores"""
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    name = Column(String, index=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    cnpj = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, default=True)

class PurchaseOrder(Base):
    """Pedidos de Compra (Entrada de Estoque)"""
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    items_json = Column(Text)
    total_cost = Column(Float, default=0.0)
    status = Column(String, default="pending") # pending, received, cancelled
    received_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)
    supplier = relationship("Supplier")

class Exchange(Base):
    """Trocas e Devoluções"""
    __tablename__ = "exchanges"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    sale_id = Column(Integer, ForeignKey("sales.id"))
    items_json = Column(Text)
    type = Column(String, default="exchange") # exchange, refund
    reason = Column(Text, nullable=True)
    refund_amount = Column(Float, default=0.0)
    date = Column(DateTime(timezone=True), default=get_manaus_time)
    sale = relationship("Sale")

class SalesGoal(Base):
    """Metas de Vendas Mensais"""
    __tablename__ = "sales_goals"
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    month = Column(Integer) # 1-12
    year = Column(Integer)
    target_value = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), default=get_manaus_time)

# --- UTILS & INITIAL DATA ---

def populate_initial_data(db):
    if db.query(User).filter(User.username == "alessandro").count() == 0:
        master_user = User(
            username="alessandro", 
            hashed_password=pwd_context.hash("master123"),
            is_master=True
        )
        db.add(master_user)
        db.commit()
    
    if db.query(Company).count() == 0:
        demo_co = Company(name="Fashion Store Premium", category="Loja de Roupas")
        db.add(demo_co)
        db.commit()
        db.refresh(demo_co)
        
        admin_user = User(
            username="admin", 
            hashed_password=pwd_context.hash("123456"),
            company_id=demo_co.id
        )
        db.add(admin_user)
        db.commit()
        
        new_sub = Subscription(
            company_id=demo_co.id,
            plan_type="mensal",
            start_date=get_manaus_time(),
            end_date=get_manaus_time() + timedelta(days=30),
            status="active"
        )
        db.add(new_sub)
        db.commit()
        
        cat1 = Category(company_id=demo_co.id, name="Masculino")
        cat2 = Category(company_id=demo_co.id, name="Feminino")
        db.add(cat1)
        db.add(cat2)
        db.commit()

def run_migrations():
    """Garante que todas as tabelas e colunas existam."""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    
    with engine.connect() as conn:
        cols_co = [c['name'] for c in inspector.get_columns('companies')]
        if 'active' not in cols_co:
            conn.execute(text("ALTER TABLE companies ADD COLUMN active BOOLEAN DEFAULT TRUE"))
        if 'delivery_fee' not in cols_co:
            conn.execute(text("ALTER TABLE companies ADD COLUMN delivery_fee FLOAT DEFAULT 0.0"))
        if 'delivery_mode' not in cols_co:
            conn.execute(text("ALTER TABLE companies ADD COLUMN delivery_mode VARCHAR DEFAULT 'fixed'"))
        if 'slug' not in cols_co:
            conn.execute(text("ALTER TABLE companies ADD COLUMN slug VARCHAR"))
        
        cols_sales = [c['name'] for c in inspector.get_columns('sales')]
        if 'delivery_type' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_type VARCHAR"))
        if 'delivery_fee' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_fee FLOAT DEFAULT 0.0"))
        if 'delivery_address' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_address TEXT"))
        if 'delivery_reference' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_reference TEXT"))
        if 'delivery_location_link' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_location_link TEXT"))
        if 'delivery_driver_id' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_driver_id INTEGER"))
        if 'delivery_status' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_status VARCHAR DEFAULT 'waiting'"))
        if 'delivery_assigned_at' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_assigned_at DATETIME"))
        if 'delivery_dispatched_at' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_dispatched_at DATETIME"))
        if 'delivery_completed_at' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_completed_at DATETIME"))
        if 'seller_id' not in cols_sales:
            conn.execute(text("ALTER TABLE sales ADD COLUMN seller_id INTEGER"))

        cols_sub = [c['name'] for c in inspector.get_columns('subscriptions')]
        if 'saas_reminder_sent_at' not in cols_sub:
            conn.execute(text("ALTER TABLE subscriptions ADD COLUMN saas_reminder_sent_at DATETIME"))
        
        cols_prod = [c['name'] for c in inspector.get_columns('products')]
        if 'cost_price' not in cols_prod:
            conn.execute(text("ALTER TABLE products ADD COLUMN cost_price FLOAT DEFAULT 0.0"))
        if 'show_on_whatsapp' not in cols_prod:
            conn.execute(text("ALTER TABLE products ADD COLUMN show_on_whatsapp BOOLEAN DEFAULT TRUE"))

        cols_vars = [c['name'] for c in inspector.get_columns('product_variations')]
        if 'cost_price_override' not in cols_vars:
            conn.execute(text("ALTER TABLE product_variations ADD COLUMN cost_price_override FLOAT"))
        
        cols_sched = [c['name'] for c in inspector.get_columns('scheduled_campaigns')]
        if 'frequency' not in cols_sched:
            conn.execute(text("ALTER TABLE scheduled_campaigns ADD COLUMN frequency VARCHAR DEFAULT 'once'"))
        if 'post_to_status' not in cols_sched:
            conn.execute(text("ALTER TABLE scheduled_campaigns ADD COLUMN post_to_status BOOLEAN DEFAULT 0"))
        
        cols_sale_items = [c['name'] for c in inspector.get_columns('sale_items')]
        if 'cost_price' not in cols_sale_items:
            conn.execute(text("ALTER TABLE sale_items ADD COLUMN cost_price FLOAT DEFAULT 0.0"))
        
        cols_cust = [c['name'] for c in inspector.get_columns('customers')]
        if 'birthday' not in cols_cust:
            conn.execute(text("ALTER TABLE customers ADD COLUMN birthday VARCHAR"))
        if 'loyalty_points' not in cols_cust:
            conn.execute(text("ALTER TABLE customers ADD COLUMN loyalty_points INTEGER DEFAULT 0"))
        if 'total_points_earned' not in cols_cust:
            conn.execute(text("ALTER TABLE customers ADD COLUMN total_points_earned INTEGER DEFAULT 0"))

        conn.commit()
    logger.info("✨ Banco de dados sincronizado.")
