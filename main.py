# ceo_remote_backend/main.py
# ═══════════════════════════════════════════════════════════════════════════
# Keji Pharmacy — CEO Remote Backend
# Deployed to Railway. Reads from Supabase (cloud PostgreSQL).
# Serves ONLY the CEO dashboard — no POS, no store manager routes.
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

from routers import auth, reports, inventory_ceo


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("✓ Keji Pharmacy CEO Remote Backend starting...")
    yield
    print("✓ Keji Pharmacy CEO Remote Backend shutting down.")


app = FastAPI(
    title="Keji Pharmacy CEO API",
    description="Remote CEO dashboard backend — reads from Supabase",
    version="1.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS — only allow the Vercel frontend domain
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,          prefix="/api/auth",      tags=["Auth"])
app.include_router(reports.router,       prefix="/api/reports",   tags=["Reports"])
app.include_router(inventory_ceo.router, prefix="/api/inventory", tags=["Inventory"])


@app.get("/api/health")
def health():
    return {
        "status":  "ok",
        "service": "Keji Pharmacy CEO Remote API",
        "version": "1.0.0",
    }