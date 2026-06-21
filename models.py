# ceo_remote_backend/models.py
import uuid
from sqlalchemy import Column, String, Boolean, Integer, Numeric, Date, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from database import Base

def new_uuid(): return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"
    id            = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    full_name     = Column(String(150), nullable=False)
    email         = Column(String(150), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role          = Column(String(20), nullable=False)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at = Column(DateTime(timezone=True))

class Product(Base):
    __tablename__ = "products"
    id                     = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    brand_name             = Column(String(150), nullable=False)
    generic_name           = Column(String(150), nullable=False)
    strength               = Column(String(50))
    form                   = Column(String(30))
    composition            = Column(Text)
    category_id            = Column(UUID(as_uuid=False))
    unit_of_measure        = Column(String(20))
    requires_prescription  = Column(Boolean, default=False)
    min_stock_alert        = Column(Integer, default=10)
    nafdac_number          = Column(String(50))
    is_active              = Column(Boolean, default=True)
    created_at             = Column(DateTime(timezone=True), server_default=func.now())

class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    id                 = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    product_id         = Column(UUID(as_uuid=False), ForeignKey("products.id"))
    batch_number       = Column(String(100))
    cost_price         = Column(Numeric(12, 2))
    selling_price      = Column(Numeric(12, 2))
    quantity_remaining = Column(Integer)
    expiry_date        = Column(Date)
    is_active          = Column(Boolean, default=True)
    # Price tiers — fixed prices set at stock receipt
    price_retail_general       = Column(Numeric(12, 2))
    price_retail_subsidized    = Column(Numeric(12, 2))
    price_wholesale_general    = Column(Numeric(12, 2))
    price_wholesale_subsidized = Column(Numeric(12, 2))
    price_wholesale_bulk       = Column(Numeric(12, 2))
    synced             = Column(Boolean, default=False)
    synced_at          = Column(DateTime(timezone=True))

class PriceChangeLog(Base):
    __tablename__ = "price_change_log"
    id                = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    batch_id          = Column(UUID(as_uuid=False))
    product_id        = Column(UUID(as_uuid=False))
    changed_by        = Column(UUID(as_uuid=False))
    old_selling_price = Column(Numeric(12, 2))
    new_selling_price = Column(Numeric(12, 2))
    reason            = Column(Text)
    changed_at        = Column(DateTime(timezone=True), server_default=func.now())
    synced            = Column(Boolean, default=False)
