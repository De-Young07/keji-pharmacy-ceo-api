# ceo_remote_backend/routers/inventory_ceo.py
from typing import List, Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db
from auth_utils import require_ceo
import models
import uuid as _uuid

router = APIRouter()

class ExpiryAlert(BaseModel):
    batch_id: str; batch_number: str; product_id: str
    brand_name: str; generic_name: str; strength: Optional[str]
    expiry_date: str; days_until_expiry: int
    quantity_remaining: int; value_at_risk: Decimal; selling_price: Decimal

class ProductStockRow(BaseModel):
    product_id: str; brand_name: str; generic_name: str
    strength: Optional[str]; form: str; category: str
    total_stock: int; stock_status: str
    active_batch_id: Optional[str]; active_batch_number: Optional[str]
    current_selling_price: Optional[Decimal]; current_cost_price: Optional[Decimal]
    earliest_expiry: Optional[str]

class PriceUpdateRequest(BaseModel):
    new_selling_price: Decimal
    reason: Optional[str] = None

class ReceiveBatchRequest(BaseModel):
    product_id: str; supplier_id: str; batch_number: str
    cost_price: Decimal; selling_price: Decimal
    quantity_received: int; expiry_date: str
    manufacture_date: Optional[str] = None; notes: Optional[str] = None

    @validator("quantity_received")
    def positive_qty(cls, v):
        if v <= 0: raise ValueError("Quantity must be greater than zero")
        return v

    @validator("selling_price")
    def price_above_cost(cls, v, values):
        if "cost_price" in values and v < values["cost_price"]:
            raise ValueError("Selling price cannot be lower than cost price")
        return v

class SupplierRow(BaseModel):
    id: str; company_name: str

class ProductCatalogRow(BaseModel):
    id: str; brand_name: str; generic_name: str
    strength: Optional[str]; form: str

class ProfitSummary(BaseModel):
    total_revenue: Decimal; total_cost: Decimal; total_profit: Decimal
    overall_margin_pct: float; best_category: Optional[str]
    best_category_profit: Optional[Decimal]; total_transactions: int


@router.get("/expiry-alerts", response_model=List[ExpiryAlert])
def get_expiry_alerts(days: int = Query(default=90), db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text("""
        SELECT b.id::text AS batch_id, b.batch_number, b.product_id::text,
               p.brand_name, p.generic_name, p.strength, b.expiry_date::text,
               (b.expiry_date - CURRENT_DATE)::INTEGER AS days_until_expiry,
               b.quantity_remaining,
               (b.selling_price * b.quantity_remaining) AS value_at_risk,
               b.selling_price
        FROM inventory_batches b JOIN products p ON p.id = b.product_id
        WHERE b.is_active = TRUE AND b.quantity_remaining > 0
          AND (b.expiry_date - CURRENT_DATE) <= :days
        ORDER BY b.expiry_date ASC
    """), {"days": days}).mappings().all()
    return [ExpiryAlert(**dict(r)) for r in rows]


@router.get("/products", response_model=List[ProductStockRow])
def get_products(q: str = Query(default=""), db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text("""
        SELECT product_id::text, brand_name, generic_name, strength, form, category,
               total_stock, stock_status, active_batch_id::text, active_batch_number,
               current_selling_price, current_cost_price, earliest_expiry::text
        FROM product_stock_summary
        WHERE :q = '' OR brand_name ILIKE '%' || :q || '%' OR generic_name ILIKE '%' || :q || '%'
        ORDER BY brand_name ASC LIMIT 100
    """), {"q": q}).mappings().all()
    return [ProductStockRow(**dict(r)) for r in rows]


@router.get("/suppliers", response_model=List[SupplierRow])
def get_suppliers(db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text(
        "SELECT id::text, company_name FROM suppliers WHERE is_active = TRUE ORDER BY company_name"
    )).mappings().all()
    return [SupplierRow(**dict(r)) for r in rows]


@router.get("/catalog", response_model=List[ProductCatalogRow])
def get_catalog(db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text(
        "SELECT id::text, brand_name, generic_name, strength, form FROM products WHERE is_active = TRUE ORDER BY brand_name"
    )).mappings().all()
    return [ProductCatalogRow(**dict(r)) for r in rows]


@router.put("/batches/{batch_id}/price")
def update_price(batch_id: str, body: PriceUpdateRequest, db: Session = Depends(get_db), current_user: models.User = Depends(require_ceo)):
    batch = db.query(models.InventoryBatch).filter(models.InventoryBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    db.add(models.PriceChangeLog(
        batch_id=batch_id, product_id=str(batch.product_id),
        changed_by=str(current_user.id), old_selling_price=batch.selling_price,
        new_selling_price=body.new_selling_price, reason=body.reason, synced=False,
    ))
    batch.selling_price = body.new_selling_price
    db.commit()
    return {"message": "Price updated. Syncing to store within 60s.", "new_price": float(body.new_selling_price)}


@router.post("/batches/receive")
def receive_batch(body: ReceiveBatchRequest, db: Session = Depends(get_db), current_user: models.User = Depends(require_ceo)):
    if not db.execute(text("SELECT id FROM products WHERE id = :pid"), {"pid": body.product_id}).fetchone():
        raise HTTPException(status_code=404, detail="Product not found.")
    if not db.execute(text("SELECT id FROM suppliers WHERE id = :sid"), {"sid": body.supplier_id}).fetchone():
        raise HTTPException(status_code=404, detail="Supplier not found.")
    if db.execute(text("SELECT id FROM inventory_batches WHERE product_id = :pid AND batch_number = :bn"),
                  {"pid": body.product_id, "bn": body.batch_number}).fetchone():
        raise HTTPException(status_code=409, detail=f"Batch '{body.batch_number}' already exists.")

    new_id = str(_uuid.uuid4())
    db.execute(text("""
        INSERT INTO inventory_batches (
            id, product_id, supplier_id, batch_number, cost_price, selling_price,
            quantity_received, quantity_remaining, expiry_date, manufacture_date,
            received_by, notes, is_active, synced, synced_at
        ) VALUES (
            :id, :product_id, :supplier_id, :batch_number, :cost_price, :selling_price,
            :qty, :qty, :expiry_date, :manufacture_date,
            :received_by, :notes, TRUE, FALSE, NULL
        )
    """), {
        "id": new_id, "product_id": body.product_id, "supplier_id": body.supplier_id,
        "batch_number": body.batch_number, "cost_price": float(body.cost_price),
        "selling_price": float(body.selling_price), "qty": body.quantity_received,
        "expiry_date": body.expiry_date, "manufacture_date": body.manufacture_date,
        "received_by": str(current_user.id), "notes": body.notes,
    })
    db.commit()
    margin = round(((body.selling_price - body.cost_price) / body.selling_price) * 100, 1)
    return {"message": "Batch received. Syncing to store within 60s.", "batch_id": new_id, "margin_percent": margin}


@router.get("/profit-summary", response_model=ProfitSummary)
def get_profit_summary(db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    row = db.execute(text("""
        SELECT COALESCE(SUM(si.line_total), 0) AS total_revenue,
               COALESCE(SUM(si.quantity_sold * si.unit_cost_price), 0) AS total_cost,
               COALESCE(SUM(si.line_profit), 0) AS total_profit,
               COUNT(DISTINCT s.id) AS total_transactions
        FROM sale_items si JOIN sales s ON s.id = si.sale_id
    """)).mappings().one()
    revenue = row["total_revenue"] or Decimal("0")
    profit  = row["total_profit"]  or Decimal("0")
    margin  = float(profit / revenue * 100) if revenue > 0 else 0.0
    cat = db.execute(text("""
        SELECT cat.name, SUM(si.line_profit) AS p
        FROM sale_items si JOIN products p ON p.id = si.product_id
        JOIN categories cat ON cat.id = p.category_id
        GROUP BY cat.name ORDER BY p DESC LIMIT 1
    """)).fetchone()
    return ProfitSummary(
        total_revenue=revenue, total_cost=row["total_cost"] or Decimal("0"),
        total_profit=profit, overall_margin_pct=round(margin, 2),
        best_category=cat[0] if cat else None,
        best_category_profit=Decimal(str(cat[1])) if cat else None,
        total_transactions=row["total_transactions"] or 0,
    )
