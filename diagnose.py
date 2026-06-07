"""
diagnose.py — Run this in your backend folder with venv activated.
It tests every layer of the stack independently and tells you exactly
what is broken and why.

Usage:
    cd C:\Keji\backend
    venv\Scripts\activate
    python diagnose.py
"""

import sys
import os

print("\n" + "="*60)
print("  Keji Pharmacy — Backend Diagnostic Tool")
print("="*60 + "\n")

PASS = "  ✓"
FAIL = "  ✗"
WARN = "  ⚠"

all_ok = True

# ── 1. Python version ─────────────────────────────────────────────────────────
print("[ 1 ] Python version")
major, minor = sys.version_info[:2]
if major == 3 and minor >= 11:
    print(f"{PASS} Python {major}.{minor} — OK")
else:
    print(f"{FAIL} Python {major}.{minor} — Need 3.11+")
    all_ok = False

# ── 2. Required packages ──────────────────────────────────────────────────────
print("\n[ 2 ] Required packages")
required = {
    "fastapi":           "fastapi",
    "uvicorn":           "uvicorn",
    "sqlalchemy":        "sqlalchemy",
    "psycopg2":          "psycopg2",
    "jwt (PyJWT)":       "jwt",
    "bcrypt":            "bcrypt",
    "pydantic_settings": "pydantic_settings",
}
for label, module in required.items():
    try:
        m = __import__(module)
        version = getattr(m, "__version__", "unknown")
        print(f"{PASS} {label} ({version})")
    except ImportError:
        print(f"{FAIL} {label} — NOT INSTALLED")
        all_ok = False

# Check python-jose is NOT installed (it conflicts)
try:
    import jose
    print(f"{WARN} python-jose is still installed ({jose.__version__}) — this WILL break JWT decode")
    print(f"       Fix: pip uninstall python-jose -y")
    all_ok = False
except ImportError:
    print(f"{PASS} python-jose not installed — good")

# ── 3. .env file ──────────────────────────────────────────────────────────────
print("\n[ 3 ] .env file")
env_path = os.path.join(os.path.dirname(__file__), ".env")
if not os.path.exists(env_path):
    print(f"{FAIL} .env file not found at {env_path}")
    all_ok = False
else:
    print(f"{PASS} .env file exists")
    with open(env_path) as f:
        content = f.read()
    if "CHANGE_THIS" in content or "REPLACE_THIS" in content:
        print(f"{FAIL} SECRET_KEY is still the placeholder value — generate a real one")
        print(f"       Fix: python -c \"import secrets; print(secrets.token_hex(32))\"")
        all_ok = False
    else:
        print(f"{PASS} SECRET_KEY appears to be set")
    if "YOUR_ACTUAL" in content:
        print(f"{FAIL} DATABASE_URL still contains placeholder — update your postgres password")
        all_ok = False
    else:
        print(f"{PASS} DATABASE_URL appears to be set")

# ── 4. Config loads ───────────────────────────────────────────────────────────
print("\n[ 4 ] Config loading")
try:
    from config import settings
    print(f"{PASS} config.py loaded OK")
    print(f"       DATABASE_URL: {settings.DATABASE_URL[:40]}...")
    print(f"       ALGORITHM:    {settings.ALGORITHM}")
    print(f"       SECRET_KEY:   {settings.SECRET_KEY[:8]}... (truncated)")
except Exception as e:
    print(f"{FAIL} config.py failed to load: {e}")
    all_ok = False

# ── 5. Database connection ────────────────────────────────────────────────────
print("\n[ 5 ] Database connection")
try:
    from database import engine
    with engine.connect() as conn:
        from sqlalchemy import text
        result = conn.execute(text("SELECT current_database(), version()"))
        row = result.fetchone()
        print(f"{PASS} Connected to database: {row[0]}")
        print(f"       PostgreSQL: {row[1][:50]}")
except Exception as e:
    print(f"{FAIL} Cannot connect to database: {e}")
    print(f"       Check your DATABASE_URL and that PostgreSQL is running")
    all_ok = False

# ── 6. Tables exist ───────────────────────────────────────────────────────────
print("\n[ 6 ] Database tables")
expected_tables = [
    "users", "categories", "suppliers", "products",
    "inventory_batches", "customers", "sales", "sale_items",
    "payments", "customer_ledgers", "price_change_log",
    "sync_log", "sessions"
]
try:
    from database import engine
    from sqlalchemy import text, inspect
    inspector = inspect(engine)
    existing = inspector.get_table_names()
    for t in expected_tables:
        if t in existing:
            print(f"{PASS} {t}")
        else:
            print(f"{FAIL} {t} — MISSING (run schema.sql)")
            all_ok = False
except Exception as e:
    print(f"{FAIL} Could not inspect tables: {e}")
    all_ok = False

# ── 7. Seed data ──────────────────────────────────────────────────────────────
print("\n[ 7 ] Seed data")
try:
    from database import SessionLocal
    db = SessionLocal()
    import models
    users      = db.query(models.User).count()
    products   = db.query(models.Product).count()
    batches    = db.query(models.InventoryBatch).count()
    categories = db.query(models.Category).count()
    db.close()
    print(f"{PASS} Users:      {users}     (expect 3)")
    print(f"{PASS} Categories: {categories} (expect 15)")
    print(f"{PASS} Products:   {products}   (expect 10)")
    print(f"{PASS} Batches:    {batches}   (expect 11)")
    if users == 0:
        print(f"{FAIL} No users found — run seed.sql")
        all_ok = False
except Exception as e:
    print(f"{FAIL} Could not query seed data: {e}")
    all_ok = False

# ── 8. JWT encode + decode roundtrip ─────────────────────────────────────────
print("\n[ 8 ] JWT encode → decode roundtrip")
try:
    from auth_utils import create_access_token, decode_token
    token = create_access_token(data={"sub": "test-user-id", "role": "ceo"})
    print(f"{PASS} Token created: {token[:40]}...")
    payload = decode_token(token)
    assert payload["sub"] == "test-user-id", "sub mismatch"
    assert payload["role"] == "ceo", "role mismatch"
    print(f"{PASS} Token decoded successfully")
    print(f"       sub:  {payload['sub']}")
    print(f"       role: {payload['role']}")
except Exception as e:
    print(f"{FAIL} JWT roundtrip failed: {e}")
    print(f"       This is why every protected route returns 401")
    all_ok = False

# ── 9. Password verify ────────────────────────────────────────────────────────
print("\n[ 9 ] Password hashing")
try:
    from auth_utils import hash_password, verify_password
    test_pwd = "TestPassword123"
    hashed = hash_password(test_pwd)
    assert verify_password(test_pwd, hashed), "verify failed"
    assert not verify_password("wrong", hashed), "should reject wrong password"
    print(f"{PASS} hash_password works")
    print(f"{PASS} verify_password works")
except Exception as e:
    print(f"{FAIL} Password hashing failed: {e}")
    all_ok = False

# ── 10. Real user login simulation ────────────────────────────────────────────
print("\n[ 10 ] Real user login simulation")
try:
    from database import SessionLocal
    from auth_utils import verify_password, create_access_token, decode_token
    import models as m

    db   = SessionLocal()
    user = db.query(m.User).filter(m.User.is_active == True).first()
    db.close()

    if not user:
        print(f"{FAIL} No active users in database")
        all_ok = False
    else:
        print(f"{PASS} Found user: {user.email} (role: {user.role})")
        print(f"       password_hash starts with: {user.password_hash[:20]}...")
        if not user.password_hash.startswith("$2b$"):
            print(f"{FAIL} Hash does not look like a bcrypt hash")
            print(f"       Run: python -c \"import bcrypt; print(bcrypt.hashpw(b'YourPassword', bcrypt.gensalt()).decode())\"")
            print(f"       Then: UPDATE users SET password_hash='...' WHERE email='{user.email}';")
            all_ok = False
        else:
            print(f"{PASS} Hash format looks correct (bcrypt)")
            # Issue a real token for this user
            token = create_access_token({"sub": str(user.id), "role": user.role})
            payload = decode_token(token)
            print(f"{PASS} Full login simulation succeeded for {user.email}")
            print(f"       Token sub: {payload['sub']}")
except Exception as e:
    print(f"{FAIL} Login simulation failed: {e}")
    all_ok = False

# ── 11. Routers importable ────────────────────────────────────────────────────
print("\n[ 11 ] Router imports")
router_names = ["auth", "products", "inventory", "sales", "customers", "payments", "reports"]
for name in router_names:
    try:
        mod = __import__(f"routers.{name}", fromlist=[name])
        print(f"{PASS} routers/{name}.py")
    except Exception as e:
        print(f"{FAIL} routers/{name}.py — {e}")
        all_ok = False

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
if all_ok:
    print("  ALL CHECKS PASSED — backend should be working")
    print("  If you still get 401s, the issue is in the frontend.")
    print("  Follow the browser Network tab instructions below.")
else:
    print("  SOME CHECKS FAILED — fix the ✗ items above first")
print("="*60)

print("""
── Browser Network Tab Diagnosis ────────────────────────────────
1. Open Chrome → F12 → Network tab
2. Check 'Preserve log'
3. Log in to the app
4. Click on Sales History (the tab that logs you out)
5. In the Network tab, find the failing request (red row)
6. Click it → look at:
     - Request URL  (what endpoint was called)
     - Status code  (401? 403? 422? 500?)
     - Response tab (what did the server actually say?)
7. Paste that information — it will identify the exact problem.
─────────────────────────────────────────────────────────────────
""")
