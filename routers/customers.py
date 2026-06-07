# ═══════════════════════════════════════════════════════════════════════════
# routers/customers.py
# ═══════════════════════════════════════════════════════════════════════════

from typing import List, Optional
from decimal import Decimal
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_utils import get_current_user, require_ceo
import models

router = APIRouter()

# ── Schemas ───────────────────────────────────────────────────────────────────

class CustomerCreateRequest(BaseModel):
    full_name:      str
    phone:          Optional[str] = None
    email:          Optional[str] = None
    address:        Optional[str] = None
    date_of_birth:  Optional[date] = None
    gender:         Optional[str] = None
    notes:          Optional[str] = None


class CustomerResponse(BaseModel):
    id:              UUID
    full_name:       str
    phone:           Optional[str]
    email:           Optional[str]
    gender:          Optional[str]
    address:         Optional[str]
    notes:           Optional[str]
    is_active:       bool
    created_at:      datetime
    current_balance: Optional[Decimal] = None  # populated dynamically

    model_config = {"from_attributes": True}


class LedgerEntryResponse(BaseModel):
    id:             UUID
    event_type:     str
    amount:         Decimal
    balance_after:  Decimal
    note:           Optional[str]
    created_at:     datetime

    model_config = {"from_attributes": True}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=CustomerResponse, status_code=201)
def create_customer(
    body:           CustomerCreateRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(get_current_user),
):
    """Add a new customer profile. Available to all store managers."""
    if body.phone:
        existing = db.query(models.Customer).filter(models.Customer.phone == body.phone).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"A customer with phone {body.phone} already exists.")

    customer = models.Customer(**body.model_dump(), created_by=current_user.id, synced=False)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    
    # Use model_validate instead of unpacking __dict__
    response_data = CustomerResponse.model_validate(customer)
    response_data.current_balance = Decimal("0")
    return response_data


@router.get("", response_model=List[CustomerResponse])
def list_customers(
    q:      str = Query(default="", description="Search by name or phone"),
    limit:  int = Query(default=50, le=200),
    db:     Session = Depends(get_db),
    _:      models.User = Depends(get_current_user),
):
    """Search and list customers. Powers the customer attach modal in the POS."""
    query = db.query(models.Customer).filter(models.Customer.is_active == True)

    if q:
        query = query.filter(
            models.Customer.full_name.ilike(f"%{q}%") |
            models.Customer.phone.ilike(f"%{q}%")
        )

    customers = query.order_by(models.Customer.full_name).limit(limit).all()
    results = []

    for c in customers:
        latest = db.query(models.CustomerLedger).filter(
            models.CustomerLedger.customer_id == c.id
        ).order_by(models.CustomerLedger.created_at.desc()).first()

        balance = latest.balance_after if latest else Decimal("0")
        
        # Build cleanly using Pydantic validation mapping
        customer_res = CustomerResponse.model_validate(c)
        customer_res.current_balance = balance
        results.append(customer_res)

    return results


@router.get("/{customer_id}/ledger", response_model=List[LedgerEntryResponse])
def get_customer_ledger(
    customer_id:    UUID,  #  Changed from str to UUID
    db:             Session = Depends(get_db),
    _:              models.User = Depends(get_current_user),
):
    """Full transaction history for a customer. Shows every debt and payment."""
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")

    return db.query(models.CustomerLedger).filter(
        models.CustomerLedger.customer_id == customer_id
    ).order_by(models.CustomerLedger.created_at.desc()).all()


@router.post("/{customer_id}/adjust", response_model=LedgerEntryResponse, status_code=201)
def adjust_customer_balance(
    customer_id:    UUID,  #  Changed from str to UUID
    amount:         Decimal = Query(description="Positive = add debt, Negative = clear debt"),
    note:           str = Query(description="Reason for manual adjustment"),
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(require_ceo),
):
    """CEO-only: manually adjust a customer's balance with an audit note."""
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")

    latest = db.query(models.CustomerLedger).filter(
        models.CustomerLedger.customer_id == customer_id
    ).order_by(models.CustomerLedger.created_at.desc()).first()

    current_balance = latest.balance_after if latest else Decimal("0")
    new_balance = current_balance + amount

    entry = models.CustomerLedger(
        customer_id=customer_id,
        event_type="ceo_adjustment",
        amount=amount,
        balance_after=new_balance,
        note=note,
        created_by=current_user.id,
        synced=False,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry