# ═══════════════════════════════════════════════════════════════════════════
# routers/products.py — Drug catalog and POS search endpoint
# ═══════════════════════════════════════════════════════════════════════════

from typing import List, Optional
from decimal import Decimal
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text, or_, func
from sqlalchemy.orm import Session, joinedload

from uuid import UUID
from pydantic import BaseModel

from database import get_db
from auth_utils import get_current_user, require_ceo
import models

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProductSearchResult(BaseModel):
    """
    The shape returned to the POS search bar.
    Contains exactly what the store manager needs to make a sale.
    """
    product_id:             UUID
    brand_name:             str
    generic_name:           str
    strength:               Optional[str]
    form:                   str
    category:               str
    unit_of_measure:        str
    total_stock:            int
    stock_status:           str         # ok | low_stock | out_of_stock | expiring_soon
    min_stock_alert:        int
    earliest_expiry:        Optional[date]
    active_batch_id:        Optional[UUID]
    active_batch_number:    Optional[str]
    current_selling_price:  Optional[Decimal]
    current_cost_price:     Optional[Decimal]
    requires_prescription:  bool

    model_config = {"from_attributes": True}


class ProductCreateRequest(BaseModel):
    brand_name:             str
    generic_name:           str
    strength:               Optional[str] = None
    form:                   str
    category_id:            UUID
    unit_of_measure:        str = "piece"
    requires_prescription:  bool = False
    min_stock_alert:        int = 10
    nafdac_number:          Optional[str] = None


class ProductResponse(BaseModel):
    id:                     UUID
    brand_name:             str
    generic_name:           str
    strength:               Optional[str]
    form:                   str
    category_id:            UUID
    unit_of_measure:        str
    requires_prescription:  bool
    min_stock_alert:        int
    nafdac_number:          Optional[str]
    is_active:              bool

    model_config = {"from_attributes": True}


class CategoryResponse(BaseModel):
    id:     UUID
    name:   str

    class Config:
        from_attributes = True


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/search", response_model=List[ProductSearchResult])
def search_products(
    q:          str = Query(default="", description="Search term — brand name, generic, or strength"),
    category:   Optional[str] = Query(default=None, description="Category name to filter by"),
    status:     Optional[str] = Query(default=None, description="Filter: ok | low_stock | out_of_stock | expiring_soon"),
    limit:      int = Query(default=50, le=200),
    db:         Session = Depends(get_db),
    _:          models.User = Depends(get_current_user),
):
    """
    The POS search endpoint. Powers the drug table on the store manager screen.

    - Searches both brand_name and generic_name using PostgreSQL trigram similarity.
    - Automatically returns stock summary from the product_stock_summary VIEW.
    - Results sorted by stock_status severity, then alphabetically.
    """
    # Use the pre-built VIEW for performance
    sql = text("""
        SELECT
            product_id,
            brand_name,
            generic_name,
            strength,
            form,
            category,
            unit_of_measure,
            total_stock,
            stock_status,
            min_stock_alert,
            earliest_expiry,
            active_batch_id,
            active_batch_number,
            current_selling_price,
            current_cost_price,
            requires_prescription
        FROM product_stock_summary
        WHERE
            (:q = '' OR
             brand_name ILIKE '%' || :q || '%' OR
             generic_name ILIKE '%' || :q || '%' OR
             strength ILIKE '%' || :q || '%')
            AND (:category IS NULL OR category = :category)
            AND (:status IS NULL OR stock_status = :status)
        ORDER BY
            CASE stock_status
                WHEN 'out_of_stock'   THEN 1
                WHEN 'expiring_soon'  THEN 2
                WHEN 'low_stock'      THEN 3
                ELSE 4
            END,
            brand_name ASC
        LIMIT :limit
    """)

    rows = db.execute(sql, {"q": q, "category": category, "status": status, "limit": limit}).mappings().all()
    return [ProductSearchResult.model_validate(row) for row in rows]


@router.get("/categories", response_model=List[CategoryResponse])
def list_categories(
    db: Session = Depends(get_db),
    _:  models.User = Depends(get_current_user),
):
    """All drug categories — used to populate the filter chips on the POS."""
    return db.query(models.Category).order_by(models.Category.name).all()


@router.post("/", response_model=ProductResponse, status_code=201)
def create_product(
    body:           ProductCreateRequest,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(require_ceo),
):
    """Create a new drug in the catalog. CEO only."""
    # Validate category exists
    cat = db.query(models.Category).filter(models.Category.id == body.category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")

    product = models.Product(
        **body.model_dump(),
        created_by=current_user.id,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: str,
    db:         Session = Depends(get_db),
    _:          models.User = Depends(get_current_user),
):
    """Get a single product's full catalog details."""
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    return product


@router.delete("/{product_id}", status_code=204)
def deactivate_product(
    product_id:     str,
    db:             Session = Depends(get_db),
    current_user:   models.User = Depends(require_ceo),
):
    """Soft-delete a product (sets is_active=False). CEO only. Data is preserved."""
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")
    product.is_active = False
    db.commit()
