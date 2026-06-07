# ═══════════════════════════════════════════════════════════════════════════
# routers/sales.py — The core transaction engine
# This is the most important router. Get this right.
# ═══════════════════════════════════════════════════════════════════════════

from typing import List, Optional
from decimal import Decimal
from datetime import datetime, date, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
from sqlalchemy import text

from uuid import UUID
from pydantic import BaseModel

from database import get_db
from auth_utils import get_current_user, require_ceo
import models

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class SaleItemRequest(BaseModel):
    product_id:     UUID
    batch_id:       UUID
    quantity:       int

    @validator("quantity")
    def qty_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Quantity must be greater than zero.")
        return v


class PaymentRequest(BaseModel):
    payment_type:           str   # cash | transfer | pos_card
    amount:                 Decimal
    transfer_reference:     Optional[str] = None
    bank_name:              Optional[str] = None

    @validator("payment_type")
    def valid_type(cls, v):
        if v not in ("cash", "transfer", "pos_card"):
            raise ValueError("payment_type must be cash, transfer, or pos_card")
        return v

    @validator("amount")
    def positive_amount(cls, v):
        if v <= 0:
            raise ValueError("Payment amount must be positive.")
        return v


class CreateSaleRequest(BaseModel):
    items:          List[SaleItemRequest]
    payments:       List[PaymentRequest]
    customer_id:    Optional[UUID] = None
    notes:          Optional[str] = None

    @validator("items")
    def at_least_one_item(cls, v):
        if not v:
            raise ValueError("A sale must have at least one item.")
        return v


class SaleItemResponse(BaseModel):
    id:                 UUID
    product_id:         UUID
    batch_id:           UUID
    brand_name:         str
    quantity_sold:      int
    unit_selling_price: Decimal
    line_total:         Decimal
    line_profit:        Decimal

    model_config = {"from_attributes": True}

    

class SaleResponse(BaseModel):
    id:             UUID
    sale_reference: str
    customer_id:    Optional[UUID]
    customer_name:  Optional[str]
    served_by_name: str
    total_amount:   Decimal
    total_paid:     Decimal
    balance_due:    Decimal
    payment_status: str
    sale_date:      datetime
    items:          List[SaleItemResponse]

    model_config = {"from_attributes": True}

    


# ── Helper ────────────────────────────────────────────────────────────────────

def generate_sale_reference(db: Session) -> str:
    """
    Generate a human-readable, unique sale reference.
    Format: ADA-YYYYMMDD-NNNN (e.g. ADA-20261105-0042)
    """
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"ADA-{today}-"

    # Count today's sales to get the next sequence number
    count = db.execute(
        text("SELECT COUNT(*) FROM sales WHERE sale_reference LIKE :prefix"),
        {"prefix": prefix + "%"}
    ).scalar()

    return f"{prefix}{str(count + 1).zfill(4)}"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=SaleResponse, status_code=201)
def create_sale(
    body:           CreateSaleRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(get_current_user),
):
    """
    Complete a sale transaction. This endpoint does five things atomically:

    1. Validates stock availability for every item
    2. Locks the FEFO batch and deducts quantity_remaining
    3. Creates the Sale and SaleItem records
    4. Records all payment events
    5. Updates the customer ledger if there's a balance due
    """

    # ── Step 1: Validate all batches and stock ─────────────────────────────
    validated_items = []
    total_amount = Decimal("0")

    for item_req in body.items:
        batch = db.query(models.InventoryBatch).filter(
            models.InventoryBatch.id == item_req.batch_id,
            models.InventoryBatch.product_id == item_req.product_id,
            models.InventoryBatch.is_active == True,
        ).with_for_update().first()  # Lock row to prevent race conditions

        if not batch:
            raise HTTPException(
                status_code=404,
                detail=f"Batch {item_req.batch_id} not found or inactive."
            )

        if batch.quantity_remaining < item_req.quantity:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock for batch {batch.batch_number}. "
                    f"Requested: {item_req.quantity}, Available: {batch.quantity_remaining}"
                )
            )

        if batch.expiry_date <= datetime.now().date():
            raise HTTPException(
                status_code=400,
                detail=f"Batch {batch.batch_number} has expired. Cannot sell expired stock."
            )

        line_total = batch.selling_price * item_req.quantity
        total_amount += line_total

        validated_items.append({
            "batch":            batch,
            "product_id":       item_req.product_id,
            "quantity":         item_req.quantity,
            "cost_price":       batch.cost_price,
            "selling_price":    batch.selling_price,
            "line_total":       line_total,
        })

    # ── Step 2: Validate payment amounts ──────────────────────────────────
    total_paid = sum(p.amount for p in body.payments)
    balance_due = total_amount - total_paid

    # Determine payment status
    if balance_due <= 0:
        payment_status = "paid"
        balance_due = Decimal("0")
    elif total_paid == 0:
        payment_status = "unpaid"
    else:
        payment_status = "partial"

    # Database constraint: partial/unpaid MUST have a customer
    if payment_status in ("partial", "unpaid") and not body.customer_id:
        raise HTTPException(
            status_code=400,
            detail="A customer profile must be attached for partial or unpaid sales."
        )

    # Validate customer exists if provided
    customer = None
    if body.customer_id:
        customer = db.query(models.Customer).filter(
            models.Customer.id == body.customer_id,
            models.Customer.is_active == True,
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found.")

    # ── Step 3: Create the Sale record ────────────────────────────────────
    sale = models.Sale(
        sale_reference=generate_sale_reference(db),
        customer_id=body.customer_id,
        served_by=current_user.id,
        total_amount=total_amount,
        total_paid=total_paid,
        payment_status=payment_status,
        notes=body.notes,
        synced=False,
    )
    db.add(sale)
    db.flush()  # Get sale.id without committing yet

    # ── Step 4: Create SaleItems and deduct stock ──────────────────────────
    for v in validated_items:
        batch = v["batch"]

        sale_item = models.SaleItem(
            sale_id=sale.id,
            product_id=v["product_id"],
            batch_id=batch.id,
            quantity_sold=v["quantity"],
            unit_cost_price=v["cost_price"],
            unit_selling_price=v["selling_price"],
            synced=False,
        )
        db.add(sale_item)

        # Deduct from batch
        batch.quantity_remaining -= v["quantity"]

        # Auto-deactivate exhausted batches
        if batch.quantity_remaining == 0:
            batch.is_active = False

    # ── Step 5: Record payments ────────────────────────────────────────────
    for pay in body.payments:
        payment = models.Payment(
            sale_id=sale.id,
            customer_id=body.customer_id,
            payment_type=pay.payment_type,
            amount=pay.amount,
            transfer_reference=pay.transfer_reference,
            bank_name=pay.bank_name,
            recorded_by=current_user.id,
            synced=False,
        )
        db.add(payment)

    # ── Step 6: Update customer ledger if debt created ─────────────────────
    if balance_due > 0 and customer:
        # Get the customer's current balance from their latest ledger entry
        latest_entry = db.query(models.CustomerLedger).filter(
            models.CustomerLedger.customer_id == customer.id
        ).order_by(models.CustomerLedger.created_at.desc()).first()

        current_balance = latest_entry.balance_after if latest_entry else Decimal("0")
        new_balance = current_balance + balance_due

        ledger_entry = models.CustomerLedger(
            customer_id=customer.id,
            sale_id=sale.id,
            event_type="sale_debt",
            amount=balance_due,
            balance_after=new_balance,
            note=f"Balance from sale {sale.sale_reference}",
            created_by=current_user.id,
            synced=False,
        )
        db.add(ledger_entry)

    # ── Commit everything atomically ───────────────────────────────────────
    db.commit()
    db.refresh(sale)

    # Build enriched response
    response_items = []
    for item in sale.items:
        product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
        line_total_val = item.quantity_sold * item.unit_selling_price
        line_profit_val = item.quantity_sold * (item.unit_selling_price - item.unit_cost_price)
        response_items.append(SaleItemResponse(
            id=item.id,
            product_id=item.product_id,
            batch_id=item.batch_id,
            brand_name=product.brand_name if product else "Unknown",
            quantity_sold=item.quantity_sold,
            unit_selling_price=item.unit_selling_price,
            line_total=line_total_val,
            line_profit=line_profit_val,
        ))

    served_by_user = db.query(models.User).filter(models.User.id == sale.served_by).first()

    return SaleResponse(
        id=sale.id,
        sale_reference=sale.sale_reference,
        customer_id=sale.customer_id,
        customer_name=customer.full_name if customer else None,
        served_by_name=served_by_user.full_name if served_by_user else "Unknown",
        total_amount=sale.total_amount,
        total_paid=sale.total_paid,
        balance_due=sale.balance_due,
        payment_status=sale.payment_status,
        sale_date=sale.sale_date,
        items=response_items,
    )


@router.get("", response_model=List[SaleResponse])
def list_sales(
    date_from:  Optional[date] = Query(default=None),
    date_to:    Optional[date] = Query(default=None),
    status:     Optional[str]  = Query(default=None),
    served_by:  Optional[str]  = Query(default=None),
    limit:      int = Query(default=50, le=500),
    db:         Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """List sales with optional filters. Managers only see their own sales."""
    query = db.query(models.Sale)

    # Managers can only see their own sales
    if current_user.role == "manager":
        query = query.filter(models.Sale.served_by == current_user.id)
    elif served_by:
        query = query.filter(models.Sale.served_by == served_by)

    if date_from:
        query = query.filter(models.Sale.sale_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(models.Sale.sale_date <= datetime.combine(date_to, datetime.max.time()))
    if status:
        query = query.filter(models.Sale.payment_status == status)

    sales = query.order_by(models.Sale.sale_date.desc()).limit(limit).all()

    results = []
    for sale in sales:
        customer = db.query(models.Customer).filter(models.Customer.id == sale.customer_id).first() if sale.customer_id else None
        served_by_user = db.query(models.User).filter(models.User.id == sale.served_by).first()

        items = []
        for item in sale.items:
            product = db.query(models.Product).filter(models.Product.id == item.product_id).first()
            items.append(SaleItemResponse(
                id=item.id,
                product_id=item.product_id,
                batch_id=item.batch_id,
                brand_name=product.brand_name if product else "Unknown",
                quantity_sold=item.quantity_sold,
                unit_selling_price=item.unit_selling_price,
                line_total=item.quantity_sold * item.unit_selling_price,
                line_profit=item.quantity_sold * (item.unit_selling_price - item.unit_cost_price),
            ))

        results.append(SaleResponse(
            id=sale.id,
            sale_reference=sale.sale_reference,
            customer_id=sale.customer_id,
            customer_name=customer.full_name if customer else None,
            served_by_name=served_by_user.full_name if served_by_user else "Unknown",
            total_amount=sale.total_amount,
            total_paid=sale.total_paid,
            balance_due=sale.balance_due,
            payment_status=sale.payment_status,
            sale_date=sale.sale_date,
            items=items,
        ))

    return results
