import hashlib
import secrets
import sqlite3
import time


def _connect(db_path):
    return sqlite3.connect(db_path, check_same_thread=False)


def init_payment_system(db_path):
    conn = _connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payment_activation_keys (
           activation_key TEXT PRIMARY KEY,
           duration_days INTEGER NOT NULL DEFAULT 30,
           created_at REAL DEFAULT 0,
           redeemed_at REAL DEFAULT 0,
           redeemed_by INTEGER,
           status TEXT DEFAULT 'unused'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payment_system_license (
           id INTEGER PRIMARY KEY CHECK (id=1),
           active_until REAL DEFAULT 0,
           last_key TEXT,
           updated_at REAL DEFAULT 0
        )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO payment_system_license
           (id, active_until, last_key, updated_at)
           VALUES (1, 0, '', 0)"""
    )
    conn.commit()
    conn.close()


def generate_activation_key(db_path, duration_days=30):
    duration_days = max(1, min(3650, int(duration_days or 30)))
    raw = secrets.token_urlsafe(24).replace("-", "").replace("_", "").upper()
    digest = hashlib.sha1(f"{raw}|{time.time()}".encode("utf-8")).hexdigest()[:8].upper()
    activation_key = f"PAY-{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{digest}"
    conn = _connect(db_path)
    conn.execute(
        """INSERT INTO payment_activation_keys
           (activation_key, duration_days, created_at, status)
           VALUES (?, ?, ?, 'unused')""",
        (activation_key, duration_days, time.time())
    )
    conn.commit()
    conn.close()
    return activation_key


def redeem_activation_key(db_path, activation_key, redeemed_by=None):
    activation_key = str(activation_key or "").strip().upper()
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT duration_days, status FROM payment_activation_keys WHERE activation_key=?",
        (activation_key,)
    ).fetchone()
    if not row:
        conn.close()
        return False, "Activation key not found", 0
    duration_days, status = row
    if status != "unused":
        conn.close()
        return False, "Activation key already used", 0

    current = conn.execute(
        "SELECT active_until FROM payment_system_license WHERE id=1"
    ).fetchone()
    now = time.time()
    start_time = max(now, float(current[0] or 0) if current else 0)
    active_until = start_time + (int(duration_days) * 86400)
    conn.execute(
        """UPDATE payment_activation_keys
           SET status='used', redeemed_at=?, redeemed_by=?
           WHERE activation_key=?""",
        (now, redeemed_by, activation_key)
    )
    conn.execute(
        """INSERT INTO payment_system_license (id, active_until, last_key, updated_at)
           VALUES (1, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             active_until=excluded.active_until,
             last_key=excluded.last_key,
             updated_at=excluded.updated_at""",
        (active_until, activation_key, now)
    )
    conn.commit()
    conn.close()
    return True, "Payment system activated", active_until


def get_license_status(db_path):
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT active_until, last_key, updated_at FROM payment_system_license WHERE id=1"
    ).fetchone()
    conn.close()
    if not row:
        return {"active": False, "active_until": 0, "last_key": "", "seconds_left": 0}
    active_until, last_key, updated_at = row
    active_until = float(active_until or 0)
    seconds_left = max(0, int(active_until - time.time()))
    return {
        "active": seconds_left > 0,
        "active_until": active_until,
        "last_key": last_key or "",
        "updated_at": float(updated_at or 0),
        "seconds_left": seconds_left,
    }


def is_payment_system_active(db_path):
    return get_license_status(db_path)["active"]


def list_activation_keys(db_path, limit=20):
    conn = _connect(db_path)
    rows = conn.execute(
        """SELECT activation_key, duration_days, status, created_at, redeemed_at, redeemed_by
           FROM payment_activation_keys
           ORDER BY created_at DESC
           LIMIT ?""",
        (int(limit),)
    ).fetchall()
    conn.close()
    return rows
