"""
sync_worker.py — Keji Pharmacy Cloud Sync Engine
=================================================
Runs as a background process on the store PC.
Every 60 seconds:
  1. Checks internet connectivity
  2. Pushes unsynced local rows to Supabase
  3. Pulls CEO price changes from Supabase to local
  4. Logs everything to sync_log table

Usage:
  python sync_worker.py           # runs continuously (production)
  python sync_worker.py --once    # runs one cycle then exits (testing)

Auto-started via start_sync.bat in Windows Startup folder.
"""

import sys
import time
import logging
import argparse
import socket
from datetime import datetime, timezone
from decimal import Decimal

import schedule
import psycopg2
import psycopg2.extras
import requests

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [SYNC] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync_worker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("sync_worker")


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    """Load .env manually — avoids pydantic dependency in worker process."""
    import os
    from pathlib import Path

    env_path = Path(__file__).parent / ".env"
    config = {}

    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    config[key.strip()] = val.strip()

    # Override with real environment variables if set
    for key in ["DATABASE_URL", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URL"]:
        if os.environ.get(key):
            config[key] = os.environ[key]

    return config


CONFIG = load_config()

LOCAL_DB_URL    = CONFIG.get("DATABASE_URL", "")
SUPABASE_DB_URL = CONFIG.get("SUPABASE_DB_URL", "")
SUPABASE_URL    = CONFIG.get("SUPABASE_URL", "")
SUPABASE_KEY    = CONFIG.get("SUPABASE_KEY", "")

SYNC_INTERVAL_SECONDS = 60
MAX_RETRY_ATTEMPTS    = 5
BATCH_SIZE            = 100   # rows per push cycle

# Tables to sync local → cloud (in dependency order)
PUSH_TABLES = [
    "customers",
    "sales",
    "sale_items",
    "payments",
    "customer_ledgers",
    "inventory_batches",
]

# Column lists for each table (excludes generated/computed columns)
TABLE_COLUMNS = {
    "customers": [
        "id", "full_name", "phone", "email", "address",
        "date_of_birth", "gender", "notes", "is_active",
        "created_at", "created_by",
    ],
    "sales": [
        "id", "sale_reference", "customer_id", "served_by",
        "total_amount", "total_paid", "payment_status",
        "sale_date", "notes", "created_at",
    ],
    "sale_items": [
        "id", "sale_id", "product_id", "batch_id",
        "quantity_sold", "unit_cost_price", "unit_selling_price",
    ],
    "payments": [
        "id", "sale_id", "customer_id", "payment_type", "amount",
        "transfer_reference", "bank_name", "payment_date",
        "recorded_by", "notes", "created_at",
    ],
    "customer_ledgers": [
        "id", "customer_id", "sale_id", "event_type",
        "amount", "balance_after", "note", "created_at", "created_by",
    ],
    "inventory_batches": [
        "id", "product_id", "supplier_id", "batch_number",
        "cost_price", "selling_price", "quantity_received",
        "quantity_remaining", "expiry_date", "manufacture_date",
        "received_date", "received_by", "notes", "is_active", "created_at",
    ],
}


# ── Connectivity check ────────────────────────────────────────────────────────

def has_internet(host="8.8.8.8", port=53, timeout=3) -> bool:
    """Check internet by trying to reach Google's DNS."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except OSError:
        return False


# ── Database connections ───────────────────────────────────────────────────────

def local_conn():
    return psycopg2.connect(LOCAL_DB_URL)


def cloud_conn():
    return psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)


# ── Supabase REST upsert (fallback if direct DB connection unavailable) ───────

def supabase_upsert(table: str, rows: list) -> bool:
    """
    Upsert rows via Supabase REST API.
    Used as fallback if direct PostgreSQL connection to Supabase fails.
    """
    if not rows:
        return True

    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }

    try:
        res = requests.post(url, json=rows, headers=headers, timeout=15)
        if res.status_code in (200, 201, 204):
            return True
        log.error(f"Supabase REST upsert failed for {table}: {res.status_code} {res.text[:200]}")
        return False
    except requests.RequestException as e:
        log.error(f"Supabase REST request failed: {e}")
        return False


# ── Serializer ────────────────────────────────────────────────────────────────

def serialize_row(row: dict) -> dict:
    """Convert psycopg2 types to JSON-serializable Python types."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat"):      # date objects
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ── PUSH: local → Supabase ────────────────────────────────────────────────────

def push_table(table: str, local_cur, cloud_cur) -> int:
    """
    Find unsynced rows in local table, upsert to cloud, mark as synced.
    Returns number of rows pushed.
    """
    columns = TABLE_COLUMNS.get(table)
    if not columns:
        return 0

    col_list = ", ".join(columns)

    # Fetch unsynced rows from local DB
    local_cur.execute(
        f"SELECT {col_list} FROM {table} WHERE synced = FALSE LIMIT %s",
        (BATCH_SIZE,)
    )
    rows = local_cur.fetchall()

    if not rows:
        return 0

    col_names = [desc[0] for desc in local_cur.description]
    row_dicts  = [serialize_row(dict(zip(col_names, row))) for row in rows]
    row_ids    = [r["id"] for r in row_dicts]

    # Upsert to cloud
    pushed = False
    if cloud_cur:
        try:
            # Build upsert SQL for cloud
            placeholders = ", ".join(["%s"] * len(columns))
            update_set   = ", ".join([f"{c}=EXCLUDED.{c}" for c in columns if c != "id"])
            sql = f"""
                INSERT INTO {table} ({col_list})
                VALUES ({placeholders})
                ON CONFLICT (id) DO UPDATE SET {update_set}
            """
            for row_dict in row_dicts:
                vals = [row_dict.get(c) for c in columns]
                cloud_cur.execute(sql, vals)
            pushed = True
        except Exception as e:
            log.warning(f"Direct cloud upsert failed for {table}: {e}. Trying REST API.")

    if not pushed:
        # Fallback to REST API
        pushed = supabase_upsert(table, row_dicts)

    if pushed:
        # Mark rows as synced in local DB
        ids_placeholder = ", ".join(["%s"] * len(row_ids))
        local_cur.execute(
            f"UPDATE {table} SET synced = TRUE, synced_at = NOW() WHERE id IN ({ids_placeholder})",
            row_ids
        )
        return len(row_ids)

    return 0


# ── PULL: Supabase → local (price changes) ────────────────────────────────────

def pull_price_changes(local_cur, cloud_cur) -> int:
    """
    Pull unsynced price changes from cloud (CEO updates) and apply to local batches.
    Returns number of price changes applied.
    """
    if not cloud_cur:
        # Try REST API fallback
        return pull_price_changes_rest(local_cur)

    try:
        cloud_cur.execute("""
            SELECT batch_id, new_selling_price, changed_at
            FROM price_change_log
            WHERE synced = FALSE
            ORDER BY changed_at ASC
            LIMIT 50
        """)
        changes = cloud_cur.fetchall()
    except Exception as e:
        log.warning(f"Could not fetch price changes from cloud: {e}")
        return 0

    if not changes:
        return 0

    applied = 0
    change_ids = []

    for batch_id, new_price, changed_at in changes:
        try:
            # Apply to local inventory_batches
            local_cur.execute(
                "UPDATE inventory_batches SET selling_price = %s WHERE id = %s",
                (new_price, str(batch_id))
            )
            applied += 1
        except Exception as e:
            log.error(f"Failed to apply price change for batch {batch_id}: {e}")

    if applied > 0:
        # Mark price changes as synced on cloud
        try:
            cloud_cur.execute("""
                UPDATE price_change_log
                SET synced = TRUE, synced_at = NOW()
                WHERE synced = FALSE
            """)
        except Exception as e:
            log.warning(f"Could not mark price changes as synced on cloud: {e}")

    return applied


def pull_price_changes_rest(local_cur) -> int:
    """REST API fallback for pulling price changes."""
    url = f"{SUPABASE_URL}/rest/v1/price_change_log"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {"synced": "eq.false", "select": "batch_id,new_selling_price,changed_at", "limit": "50"}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if not res.ok:
            return 0
        changes = res.json()
    except Exception:
        return 0

    applied = 0
    for change in changes:
        try:
            local_cur.execute(
                "UPDATE inventory_batches SET selling_price = %s WHERE id = %s",
                (change["new_selling_price"], change["batch_id"])
            )
            applied += 1
        except Exception as e:
            log.error(f"Price apply error: {e}")

    if applied:
        # Mark synced via REST PATCH
        try:
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/price_change_log",
                json={"synced": True, "synced_at": datetime.now(timezone.utc).isoformat()},
                headers={**headers, "Content-Type": "application/json"},
                params={"synced": "eq.false"},
                timeout=10,
            )
        except Exception:
            pass

    return applied


# ── Log sync result ───────────────────────────────────────────────────────────

def log_sync_result(local_cur, table: str, record_id: str,
                    operation: str, status: str, error: str = None):
    """Write to local sync_log table for audit trail."""
    try:
        local_cur.execute("""
            INSERT INTO sync_log
              (table_name, record_id, operation, direction, status, error_message, last_attempted)
            VALUES (%s, %s::uuid, %s, 'push', %s, %s, NOW())
            ON CONFLICT DO NOTHING
        """, (table, record_id, operation, status, error))
    except Exception:
        pass   # sync_log failure should never crash the worker


# ── Main sync cycle ───────────────────────────────────────────────────────────

def run_sync_cycle():
    """One complete push + pull cycle."""

    if not has_internet():
        log.info("No internet connection. Skipping sync cycle.")
        return

    if not LOCAL_DB_URL:
        log.error("DATABASE_URL not set in .env. Cannot connect to local database.")
        return

    if not SUPABASE_DB_URL and not (SUPABASE_URL and SUPABASE_KEY):
        log.error("No Supabase credentials found in .env. Cannot sync to cloud.")
        return

    local_db    = None
    cloud_db    = None
    cloud_cur   = None
    total_push  = 0
    total_pull  = 0

    try:
        local_db  = local_conn()
        local_cur = local_db.cursor()

        # Try direct cloud DB connection first
        if SUPABASE_DB_URL:
            try:
                cloud_db  = cloud_conn()
                cloud_cur = cloud_db.cursor()
                log.debug("Connected to Supabase via direct PostgreSQL connection.")
            except Exception as e:
                log.warning(f"Direct Supabase connection failed ({e}). Will use REST API.")
                cloud_cur = None

        # Push each table
        for table in PUSH_TABLES:
            try:
                pushed = push_table(table, local_cur, cloud_cur)
                if pushed:
                    log.info(f"Pushed {pushed} rows from {table}")
                    total_push += pushed
            except Exception as e:
                log.error(f"Error pushing {table}: {e}")

        # Pull price changes from CEO
        try:
            pulled = pull_price_changes(local_cur, cloud_cur)
            if pulled:
                log.info(f"Applied {pulled} price change(s) from CEO dashboard")
                total_pull += pulled
        except Exception as e:
            log.error(f"Error pulling price changes: {e}")

        # Commit all local changes
        local_db.commit()
        if cloud_db:
            cloud_db.commit()

        log.info(
            f"Cycle complete. Pushed: {total_push} rows. "
            f"Pulled: {total_pull} price changes."
        )

    except Exception as e:
        log.error(f"Sync cycle failed: {e}")
        if local_db:
            try: local_db.rollback()
            except Exception: pass

    finally:
        if cloud_db:
            try: cloud_db.close()
            except Exception: pass
        if local_db:
            try: local_db.close()
            except Exception: pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Keji Pharmacy Sync Worker")
    parser.add_argument("--once", action="store_true",
                        help="Run one sync cycle and exit (for testing)")
    parser.add_argument("--interval", type=int, default=SYNC_INTERVAL_SECONDS,
                        help=f"Sync interval in seconds (default: {SYNC_INTERVAL_SECONDS})")
    args = parser.parse_args()

    log.info("="*50)
    log.info("  Keji Pharmacy Sync Worker starting")
    log.info(f"  Local DB:   {LOCAL_DB_URL[:40]}..." if LOCAL_DB_URL else "  Local DB:   NOT SET")
    log.info(f"  Cloud DB:   {'configured' if SUPABASE_DB_URL else 'using REST API'}")
    log.info(f"  Interval:   {args.interval}s")
    log.info("="*50)

    if args.once:
        log.info("Running single sync cycle (--once mode)")
        run_sync_cycle()
        log.info("Done. Exiting.")
        return

    # Continuous mode — run on schedule
    log.info(f"Running continuously. First sync in {args.interval}s.")

    # Run immediately on start
    run_sync_cycle()

    # Then schedule repeating cycles
    schedule.every(args.interval).seconds.do(run_sync_cycle)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            log.info("Sync worker stopped by user.")
            break
        except Exception as e:
            log.error(f"Scheduler error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
