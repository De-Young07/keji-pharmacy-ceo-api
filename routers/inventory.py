# ═══════════════════════════════════════════════════════════════════════════
# routers/inventory.py
# ═══════════════════════════════════════════════════════════════════════════

from typing import List, Optional
from decimal import Decimal
from datetime import date, datetime

from uuid import UUID
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
from sqlalchemy import text

from database import get_db
from auth_utils import get_current_user, require_ceo
import models

router = APIRouter()


class BatchCreateRequest(BaseModel):
    product_id:         UUID
    supplier_id:        UUID
    batch_number:       str
    cost_price:         Decimal
    selling_price:      Decimal
    quantity_received:  int
    expiry_date:        date
    manufacture_date:   Optional[date] = None
    notes:              Optional[str] = None

    @validator("quantity_received")
    def positive_qty(cls, v):
        if v <= 0:
            raise ValueError("quantity_received must be greater than 0")
        return v

    @validator("selling_price")
    def price_above_cost(cls, v, values):
        if "cost_price" in values and v < values["cost_price"]:
            raise ValueError("selling_price cannot be lower than cost_price")
        return v


class BatchResponse(BaseModel):
    id:                 UUID
    product_id:         UUID
    supplier_id:        UUID
    batch_number:       str
    cost_price:         Decimal
    selling_price:      Decimal
    quantity_received:  int
    quantity_remaining: int
    expiry_date:        date
    is_active:          bool
    received_date:      Optional[date]
    notes:              Optional[str]

    class Config:
        from_attributes = True


class PriceUpdateRequest(BaseModel):
    new_selling_price:  Decimal
    reason:             Optional[str] = None

    @validator("new_selling_price")
    def positive_price(cls, v):
        if v <= 0:
            raise ValueError("Price must be greater than zero")
        return v


class ExpiryAlertResponse(BaseModel):
    batch_id:           UUID
    batch_number:       str
    product_id:         UUID
    brand_name:         str
    generic_name:       str
    strength:           Optional[str]
    expiry_date:        date
    days_until_expiry:  int
    quantity_remaining: int
    value_at_risk:      Decimal
    selling_price:      Decimal


@router.post("/batches", response_model=BatchResponse, status_code=201)
def receive_batch(
    body:           BatchCreateRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(get_current_user),
):
    """
    Receive a new batch of stock. Available to all store managers.
    This is how all new stock enters the system.
    """
    product = db.query(models.Product).filter(models.Product.id == body.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")

    supplier = db.query(models.Supplier).filter(models.Supplier.id == body.supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found.")

    existing = db.query(models.InventoryBatch).filter(
        models.InventoryBatch.product_id == body.product_id,
        models.InventoryBatch.batch_number == body.batch_number,
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Batch {body.batch_number} already exists for this product."
        )

    batch = models.InventoryBatch(
        **body.model_dump(),
        quantity_remaining=body.quantity_received,
        received_by=current_user.id,
        synced=False,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=List[BatchResponse])
def list_batches(
    product_id: Optional[str] = Query(default=None),
    active_only: bool = Query(default=True),
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """List all batches. Filter by product or active status."""
    query = db.query(models.InventoryBatch)
    if product_id:
        query = query.filter(models.InventoryBatch.product_id == product_id)
    if active_only:
        query = query.filter(models.InventoryBatch.is_active == True)
    return query.order_by(models.InventoryBatch.expiry_date.asc()).all()


@router.put("/batches/{batch_id}/price", response_model=BatchResponse)
def update_batch_price(
    batch_id:       str,
    body:           PriceUpdateRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(require_ceo),
):
    """CEO only: update the selling price for a batch and log the change."""
    batch = db.query(models.InventoryBatch).filter(models.InventoryBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    # Append to immutable audit log
    log = models.PriceChangeLog(
        batch_id=batch.id,
        product_id=batch.product_id,
        changed_by=current_user.id,
        old_selling_price=batch.selling_price,
        new_selling_price=body.new_selling_price,
        reason=body.reason,
        synced=False,
    )
    db.add(log)

    batch.selling_price = body.new_selling_price
    batch.synced = False  # Queue for sync to cloud

    db.commit()
    db.refresh(batch)
    return batch


@router.get("/expiry-alerts", response_model=List[ExpiryAlertResponse])
def get_expiry_alerts(
    days: int = Query(default=90, description="Alert window in days (30, 60, or 90)"),
    db:   Session = Depends(get_db),
    _:    models.User = Depends(get_current_user),
):
    """
    Return all batches expiring within the specified number of days.
    Powers the CEO's Expiry Time Bomb widget.
    """
    sql = text("""
        SELECT
            b.id                AS batch_id,
            b.batch_number,
            b.product_id,
            p.brand_name,
            p.generic_name,
            p.strength,
            b.expiry_date,
            (b.expiry_date - CURRENT_DATE)::INTEGER  AS days_until_expiry,
            b.quantity_remaining,
            (b.selling_price * b.quantity_remaining)  AS value_at_risk,
            b.selling_price
        FROM inventory_batches b
        JOIN products p ON p.id = b.product_id
        WHERE
            b.is_active = TRUE
            AND b.quantity_remaining > 0
            AND (b.expiry_date - CURRENT_DATE) <= :days
        ORDER BY b.expiry_date ASC
    """)

    rows = db.execute(sql, {"days": days}).mappings().all()
    return [ExpiryAlertResponse.model_validate(row) for row in rows]


@router.get("/suppliers")
def list_suppliers(
    db: Session = Depends(get_db),
    _:  models.User = Depends(get_current_user),
):
    """All active suppliers — used in the batch receive form."""
    return db.query(models.Supplier).filter(models.Supplier.is_active == True).all()


# ═══════════════════════════════════════════════════════════════════════════
# routers/payments.py — Record a debt repayment from a customer
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import APIRouter as PaymentsRouter

payments_router = PaymentsRouter()


class DebtRepaymentRequest(BaseModel):
    sale_id:                str
    customer_id:            str
    payment_type:           str
    amount:                 Decimal
    transfer_reference:     Optional[str] = None
    bank_name:              Optional[str] = None
    notes:                  Optional[str] = None


@payments_router.post("/repay", status_code=201)
def record_debt_repayment(
    body:           DebtRepaymentRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(get_current_user),
):
    """
    Record a customer paying off an existing debt.
    Links back to the original sale and updates the customer ledger.
    """
    sale = db.query(models.Sale).filter(models.Sale.id == body.sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Original sale not found.")

    customer = db.query(models.Customer).filter(models.Customer.id == body.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")

    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Repayment amount must be positive.")

    # Record the payment
    payment = models.Payment(
        sale_id=body.sale_id,
        customer_id=body.customer_id,
        payment_type=body.payment_type,
        amount=body.amount,
        transfer_reference=body.transfer_reference,
        bank_name=body.bank_name,
        notes=body.notes,
        recorded_by=current_user.id,
        synced=False,
    )
    db.add(payment)

    # Update the sale's total_paid
    sale.total_paid += body.amount
    if sale.total_paid >= sale.total_amount:
        sale.payment_status = "paid"
        sale.total_paid = sale.total_amount
    sale.synced = False

    # Update customer ledger
    latest = db.query(models.CustomerLedger).filter(
        models.CustomerLedger.customer_id == body.customer_id
    ).order_by(models.CustomerLedger.created_at.desc()).first()

    current_balance = latest.balance_after if latest else Decimal("0")
    new_balance = current_balance - body.amount
    if new_balance < 0:
        new_balance = Decimal("0")

    ledger_entry = models.CustomerLedger(
        customer_id=body.customer_id,
        sale_id=body.sale_id,
        event_type="payment_received",
        amount=-body.amount,   # negative = debt cleared
        balance_after=new_balance,
        note=body.notes or f"Repayment for sale {sale.sale_reference}",
        created_by=current_user.id,
        synced=False,
    )
    db.add(ledger_entry)
    db.commit()

    return {
        "message": "Repayment recorded successfully.",
        "new_balance": float(new_balance),
        "sale_status": sale.payment_status,
    }
