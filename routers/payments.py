# routers/payments.py
from typing import Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from uuid import UUID
from pydantic import BaseModel

from database import get_db
from auth_utils import get_current_user
import models

router = APIRouter()


class DebtRepaymentRequest(BaseModel):
    sale_id:            UUID
    customer_id:        UUID
    payment_type:       str
    amount:             Decimal
    transfer_reference: Optional[str] = None
    bank_name:          Optional[str] = None
    notes:              Optional[str] = None


@router.post("/repay", status_code=201)
def record_debt_repayment(
    body:           DebtRepaymentRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(get_current_user),
):
    """
    Record a customer paying off an existing debt.
    Updates the original sale, creates a new payment row,
    and appends a 'payment_received' event to the customer ledger.
    """
    sale = db.query(models.Sale).filter(models.Sale.id == body.sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Original sale not found.")

    customer = db.query(models.Customer).filter(models.Customer.id == body.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")

    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Repayment amount must be positive.")

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

    # Update sale totals
    sale.total_paid += body.amount
    if sale.total_paid >= sale.total_amount:
        sale.payment_status = "paid"
        sale.total_paid = sale.total_amount
    sale.synced = False

    # Append to ledger
    latest = db.query(models.CustomerLedger).filter(
        models.CustomerLedger.customer_id == body.customer_id
    ).order_by(models.CustomerLedger.created_at.desc()).first()

    current_balance = latest.balance_after if latest else Decimal("0")
    new_balance = max(Decimal("0"), current_balance - body.amount)

    db.add(models.CustomerLedger(
        customer_id=body.customer_id,
        sale_id=body.sale_id,
        event_type="payment_received",
        amount=-body.amount,
        balance_after=new_balance,
        note=body.notes or f"Repayment for sale {sale.sale_reference}",
        created_by=current_user.id,
        synced=False,
    ))
    db.commit()

    return {
        "message":      "Repayment recorded.",
        "new_balance":  float(new_balance),
        "sale_status":  sale.payment_status,
    }
