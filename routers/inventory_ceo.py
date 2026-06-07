# ceo_remote_backend/routers/inventory_ceo.py
from typing import List, Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db
from auth_utils import require_ceo
import models

router = APIRouter()

class ExpiryAlert(BaseModel):
    batch_id:           str
    batch_number:       str
    product_id:         str
    brand_name:         str
    generic_name:       str
    strength:           Optional[str]
    expiry_date:        str
    days_until_expiry:  int
    quantity_remaining: int
    value_at_risk:      Decimal
    selling_price:      Decimal

class PriceUpdateRequest(BaseModel):
    new_selling_price: Decimal
    reason:            Optional[str] = None

class ProductStockRow(BaseModel):
    product_id:            str
    brand_name:            str
    generic_name:          str
    strength:              Optional[str]
    form:                  str
    category:              str
    total_stock:           int
    stock_status:          str
    active_batch_id:       Optional[str]
    active_batch_number:   Optional[str]
    current_selling_price: Optional[Decimal]
    current_cost_price:    Optional[Decimal]
    earliest_expiry:       Optional[str]


@router.get("/expiry-alerts", response_model=List[ExpiryAlert])
def get_expiry_alerts(
    days: int = Query(default=90),
    db:   Session = Depends(get_db),
    _:    models.User = Depends(require_ceo),
):
    rows = db.execute(text("""
        SELECT
            b.id AS batch_id, b.batch_number, b.product_id,
            p.brand_name, p.generic_name, p.strength,
            b.expiry_date::text,
            (b.expiry_date - CURRENT_DATE)::INTEGER AS days_until_expiry,
            b.quantity_remaining,
            (b.selling_price * b.quantity_remaining) AS value_at_risk,
            b.selling_price
        FROM inventory_batches b
        JOIN products p ON p.id = b.product_id
        WHERE b.is_active = TRUE AND b.quantity_remaining > 0
          AND (b.expiry_date - CURRENT_DATE) <= :days
        ORDER BY b.expiry_date ASC
    """), {"days": days}).mappings().all()
    return [ExpiryAlert(**dict(r)) for r in rows]


@router.get("/products", response_model=List[ProductStockRow])
def get_products(
    q:  str = Query(default=""),
    db: Session = Depends(get_db),
    _:  models.User = Depends(require_ceo),
):
    rows = db.execute(text("""
        SELECT
            product_id, brand_name, generic_name, strength, form,
            category, total_stock, stock_status,
            active_batch_id::text, active_batch_number,
            current_selling_price, current_cost_price,
            earliest_expiry::text
        FROM product_stock_summary
        WHERE :q = '' OR brand_name ILIKE '%' || :q || '%'
           OR generic_name ILIKE '%' || :q || '%'
        ORDER BY brand_name ASC
        LIMIT 100
    """), {"q": q}).mappings().all()
    return [ProductStockRow(**dict(r)) for r in rows]


@router.put("/batches/{batch_id}/price")
def update_price(
    batch_id:     str,
    body:         PriceUpdateRequest,
    db:           Session = Depends(get_db),
    current_user: models.User = Depends(require_ceo),
):
    """
    CEO updates a price on the remote dashboard.
    Written to Supabase price_change_log with synced=FALSE.
    The sync worker on the store PC picks this up and applies it locally.
    """
    batch = db.query(models.InventoryBatch).filter(
        models.InventoryBatch.id == batch_id
    ).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    # Log the price change — sync worker reads this and applies to local DB
    log_entry = models.PriceChangeLog(
        batch_id=batch_id,
        product_id=str(batch.product_id),
        changed_by=str(current_user.id),
        old_selling_price=batch.selling_price,
        new_selling_price=body.new_selling_price,
        reason=body.reason,
        synced=False,   # sync worker will pick this up
    )
    db.add(log_entry)

    # Also update the cloud copy immediately
    batch.selling_price = body.new_selling_price
    db.commit()

    return {
        "message": "Price updated on cloud. Will sync to store within 60 seconds.",
        "batch_id": batch_id,
        "new_price": float(body.new_selling_price),
    }
