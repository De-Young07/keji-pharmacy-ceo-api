# ceo_remote_backend/routers/reports.py
# Identical to store backend reports.py but reads from Supabase.
from typing import List, Optional
from decimal import Decimal
from datetime import date, datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import get_db
from auth_utils import require_ceo
import models

router = APIRouter()

class KPISummary(BaseModel):
    gross_revenue:      Decimal
    net_profit:         Decimal
    total_transactions: int
    total_outstanding:  Decimal
    margin_percent:     float
    total_debtors:      int
    expiring_batches:   int
    low_stock_products: int

class DailySummary(BaseModel):
    sale_day:           date
    total_transactions: int
    gross_revenue:      Decimal
    net_profit:         Decimal
    total_collected:    Decimal
    total_outstanding:  Decimal

class DebtorSummary(BaseModel):
    customer_id:         str
    full_name:           str
    phone:               Optional[str]
    current_balance:     Decimal
    last_transaction_at: Optional[datetime]

class CategoryRevenue(BaseModel):
    category:      str
    total_revenue: Decimal
    total_profit:  Decimal
    units_sold:    int

class StaffPerformance(BaseModel):
    user_id:            str
    full_name:          str
    role:               str
    total_sales:        Decimal
    total_transactions: int
    total_profit:       Decimal


@router.get("/kpi", response_model=KPISummary)
def get_kpi(
    date_from: Optional[date] = Query(default=None),
    date_to:   Optional[date] = Query(default=None),
    db:        Session = Depends(get_db),
    _:         models.User = Depends(require_ceo),
):
    date_filter, params = "", {}
    if date_from: date_filter += " AND s.sale_date >= :date_from"; params["date_from"] = date_from
    if date_to:   date_filter += " AND s.sale_date <= :date_to";   params["date_to"]   = date_to

    row = db.execute(text(f"""
        SELECT
            COALESCE(SUM(s.total_amount), 0)  AS gross_revenue,
            COALESCE(SUM(si.total_profit), 0) AS net_profit,
            COUNT(DISTINCT s.id)              AS total_transactions,
            COALESCE(SUM(s.balance_due), 0)   AS total_outstanding
        FROM sales s
        LEFT JOIN (
            SELECT sale_id, SUM(line_profit) AS total_profit
            FROM sale_items GROUP BY sale_id
        ) si ON si.sale_id = s.id
        WHERE 1=1 {date_filter}
    """), params).mappings().one()

    gross  = row["gross_revenue"] or Decimal("0")
    profit = row["net_profit"]    or Decimal("0")
    margin = float(profit / gross * 100) if gross > 0 else 0.0

    debtor_count = db.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT ON (customer_id) customer_id, balance_after
            FROM customer_ledgers ORDER BY customer_id, created_at DESC
        ) sub WHERE balance_after > 0
    """)).scalar() or 0

    expiring = db.execute(text("""
        SELECT COUNT(*) FROM inventory_batches
        WHERE is_active = TRUE AND quantity_remaining > 0
          AND expiry_date <= CURRENT_DATE + INTERVAL '90 days'
    """)).scalar() or 0

    low_stock = db.execute(text("""
        SELECT COUNT(*) FROM product_stock_summary
        WHERE stock_status IN ('low_stock','out_of_stock')
    """)).scalar() or 0

    return KPISummary(
        gross_revenue=gross, net_profit=profit,
        total_transactions=row["total_transactions"] or 0,
        total_outstanding=row["total_outstanding"] or Decimal("0"),
        margin_percent=round(margin, 2),
        total_debtors=debtor_count,
        expiring_batches=expiring,
        low_stock_products=low_stock,
    )


@router.get("/daily", response_model=List[DailySummary])
def get_daily(days: int = Query(default=30, le=365), db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text("""
        SELECT * FROM daily_sales_summary
        WHERE sale_day >= CURRENT_DATE - :days * INTERVAL '1 day'
        ORDER BY sale_day DESC
    """), {"days": days}).mappings().all()
    return [DailySummary(**dict(r)) for r in rows]


@router.get("/debtors", response_model=List[DebtorSummary])
def get_debtors(db: Session = Depends(get_db), _: models.User = Depends(require_ceo)):
    rows = db.execute(text("""
        SELECT c.id AS customer_id, c.full_name, c.phone,
               cb.current_balance, cb.last_transaction_at
        FROM customer_balances cb
        JOIN customers c ON c.id = cb.customer_id
        WHERE cb.current_balance > 0 AND c.is_active = TRUE
        ORDER BY cb.current_balance DESC
    """)).mappings().all()
    return [DebtorSummary(**dict(r)) for r in rows]


@router.get("/category-performance", response_model=List[CategoryRevenue])
def get_category_perf(
    date_from: Optional[date] = Query(default=None),
    date_to:   Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_ceo),
):
    date_filter, params = "", {}
    if date_from: date_filter += " AND s.sale_date >= :date_from"; params["date_from"] = date_from
    if date_to:   date_filter += " AND s.sale_date <= :date_to";   params["date_to"]   = date_to

    rows = db.execute(text(f"""
        SELECT cat.name AS category, SUM(si.line_total) AS total_revenue,
               SUM(si.line_profit) AS total_profit, SUM(si.quantity_sold) AS units_sold
        FROM sale_items si
        JOIN products p     ON p.id = si.product_id
        JOIN categories cat ON cat.id = p.category_id
        JOIN sales s        ON s.id = si.sale_id
        WHERE 1=1 {date_filter}
        GROUP BY cat.name ORDER BY total_revenue DESC
    """), params).mappings().all()
    return [CategoryRevenue(**dict(r)) for r in rows]


@router.get("/staff-performance", response_model=List[StaffPerformance])
def get_staff(
    date_from: Optional[date] = Query(default=None),
    date_to:   Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_ceo),
):
    date_filter, params = "", {}
    if date_from: date_filter += " AND s.sale_date >= :date_from"; params["date_from"] = date_from
    if date_to:   date_filter += " AND s.sale_date <= :date_to";   params["date_to"]   = date_to

    rows = db.execute(text(f"""
        SELECT u.id AS user_id, u.full_name, u.role,
               COALESCE(SUM(s.total_amount), 0) AS total_sales,
               COUNT(s.id) AS total_transactions,
               COALESCE(SUM(si.profit), 0) AS total_profit
        FROM users u
        LEFT JOIN sales s ON s.served_by = u.id AND 1=1 {date_filter}
        LEFT JOIN (SELECT sale_id, SUM(line_profit) AS profit FROM sale_items GROUP BY sale_id) si
          ON si.sale_id = s.id
        WHERE u.is_active = TRUE AND u.role = 'manager'
        GROUP BY u.id ORDER BY total_sales DESC
    """), params).mappings().all()
    return [StaffPerformance(**dict(r)) for r in rows]
