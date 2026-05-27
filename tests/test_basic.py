"""
Basic smoke tests for Fashion ERP endpoints.
Run with: pytest tests/test_basic.py -v
Requires: pip install pytest httpx
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from main import app

# --- Test DB (SQLite in-memory) ---

TEST_DATABASE_URL = "sqlite:///./test.db"
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# --- AUTH TESTS ---

def test_login_page_returns_html(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_root_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert "/login" in response.headers.get("location", "")


def test_login_invalid_credentials(client):
    response = client.post("/login", data={"username": "naoexiste", "password": "errada"})
    assert response.status_code == 401
    assert response.json()["success"] is False


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/login", data={"username": "x", "password": "x"})
    response = client.post("/login", data={"username": "x", "password": "x"})
    assert response.status_code == 429


# --- UNAUTHENTICATED API TESTS ---

def test_unauthenticated_products_returns_empty(client):
    response = client.get("/api/products")
    assert response.status_code == 200
    assert response.json() == []


def test_unauthenticated_customers_returns_empty(client):
    response = client.get("/api/customers")
    assert response.status_code == 200
    assert response.json() == []


def test_unauthenticated_cash_status(client):
    response = client.get("/api/cash/status")
    assert response.status_code == 200
    assert response.json()["is_open"] is False


def test_unauthenticated_dashboard_returns_empty(client):
    response = client.get("/api/dashboard/stats")
    assert response.status_code == 200
    assert response.json() == {}


# --- HEALTH ---

def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- PUBLIC CATALOG ---

def test_catalog_not_found_for_unknown_slug(client):
    response = client.get("/catalogo/loja-que-nao-existe")
    assert response.status_code == 404


# --- PAGE AUTH GUARD (#11) ---

def test_provador_requires_auth(client):
    response = client.get("/provador", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert "/login" in response.headers.get("location", "")


def test_provador_ar_requires_auth(client):
    response = client.get("/provador-ar", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert "/login" in response.headers.get("location", "")
