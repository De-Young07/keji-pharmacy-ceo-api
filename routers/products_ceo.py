# ceo_remote_backend/routers/products_ceo.py
# Lets the CEO add new drugs to the catalog remotely. Changes sync down
# to the store's local POS the same way price updates do.

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from database import get_db
from auth_utils import require_ceo
import models

router = APIRouter()


class CategoryResponse(BaseModel):
    id:   str
    name: str
    class Config:
        from_attributes = True


class ProductCreateRequest(BaseModel):
    brand_name:             str
    generic_name:            str
    strength:                Optional[str] = None
    form:                    str
    composition:             Optional[str] = None
    category_id:             Optional[str] = None
    category_name:           Optional[str] = None
    unit_of_measure:         str = "piece"
    requires_prescription:   bool = False
    min_stock_alert:         int = 10
    nafdac_number:           Optional[str] = None

    @validator("brand_name", "generic_name", "form")
    def not_blank(cls, v):
        if not v or not v.strip():
            raise ValueError("This field cannot be blank")
        return v.strip()


class ProductResponse(BaseModel):
    id:                     str
    brand_name:             str
    generic_name:            str
    strength:                Optional[str]
    form:                    str
    composition:             Optional[str]
    category_id:             str
    category_name:           Optional[str] = None
    unit_of_measure:         str
    requires_prescription:   bool
    min_stock_alert:         int
    nafdac_number:           Optional[str]
    is_active:               bool


@router.get("/categories", response_model=List[CategoryResponse])
def get_categories(db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    return db.query(models.Category).order_by(models.Category.name).all()


@router.post("", response_model=ProductResponse, status_code=201)
def create_product(
    body: ProductCreateRequest,
    db:   Session = Depends(get_db),
    _:    models.User = Depends(require_ceo),
):
    category_id = body.category_id

    if not category_id and body.category_name:
        cat = db.query(models.Category).filter(
            models.Category.name.ilike(body.category_name.strip())
        ).first()
        if not cat:
            cat = models.Category(name=body.category_name.strip())
            db.add(cat)
            db.flush()
        category_id = str(cat.id)

    if not category_id:
        raise HTTPException(status_code=400, detail="category_id or category_name is required.")

    existing = db.query(models.Product).filter(
        models.Product.brand_name.ilike(body.brand_name.strip()),
        models.Product.strength == body.strength,
        models.Product.is_active == True,
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"'{body.brand_name}' ({body.strength or 'no strength specified'}) already exists in the catalog."
        )

    product = models.Product(
        brand_name=body.brand_name, generic_name=body.generic_name,
        strength=body.strength, form=body.form, composition=body.composition,
        category_id=category_id, unit_of_measure=body.unit_of_measure,
        requires_prescription=body.requires_prescription,
        min_stock_alert=body.min_stock_alert, nafdac_number=body.nafdac_number,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    cat = db.query(models.Category).filter(models.Category.id == product.category_id).first()
    return ProductResponse(
        id=str(product.id), brand_name=product.brand_name, generic_name=product.generic_name,
        strength=product.strength, form=product.form, composition=product.composition,
        category_id=str(product.category_id), category_name=cat.name if cat else None,
        unit_of_measure=product.unit_of_measure, requires_prescription=product.requires_prescription,
        min_stock_alert=product.min_stock_alert, nafdac_number=product.nafdac_number,
        is_active=product.is_active,
    )
