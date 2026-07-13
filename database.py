import sqlite3
import csv
import os
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "smartvibe.db"
LEADS_CSV = Path("/Users/russelljohnson/Desktop/leads.csv")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        # Add new columns if they don't exist yet (safe migration)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
        new_cols = {
            "website_live": "INTEGER",
            "website_mobile": "INTEGER",
            "website_grade": "TEXT",
            "facebook_last_post_days": "INTEGER",
            "facebook_post_frequency": "TEXT",
            "facebook_posts_per_month": "REAL",
            "instagram_last_post_days": "INTEGER",
            "tiktok_url": "TEXT",
            "tiktok_active": "INTEGER DEFAULT 0",
            "tiktok_followers": "INTEGER",
            "tiktok_last_post_days": "INTEGER",
            "last_checked": "TEXT",
            "check_status": "TEXT DEFAULT 'unchecked'",
            "ai_recommendations": "TEXT",
            "follow_up_stage": "TEXT DEFAULT 'new'",
            "instantly_contact_id": "TEXT",
            "lob_delivered_date": "TEXT",
            "form_submitted_date": "TEXT",
        }
        for col, coltype in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            phone TEXT,
            email TEXT,
            website TEXT,
            business_type TEXT,
            priority TEXT DEFAULT 'Cold',
            quality_score INTEGER DEFAULT 0,

            facebook_url TEXT,
            facebook_active INTEGER DEFAULT 0,
            facebook_followers INTEGER,
            facebook_last_post TEXT,
            instagram_url TEXT,
            instagram_active INTEGER DEFAULT 0,
            instagram_followers INTEGER,
            instagram_last_post TEXT,

            outreach_status TEXT DEFAULT 'new',
            postcard_sent_date TEXT,
            email_sent_date TEXT,
            called_date TEXT,
            converted_date TEXT,

            preview_url TEXT,
            preview_html TEXT,
            qr_code_path TEXT,

            lob_postcard_id TEXT,
            lob_tracking_url TEXT,

            qr_scanned INTEGER DEFAULT 0,
            qr_scan_date TEXT,
            form_submitted_date TEXT,

            opt_in_status TEXT DEFAULT 'no_selection',

            notes TEXT,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scrape_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cities TEXT,
            business_type TEXT,
            lead_count INTEGER,
            status TEXT DEFAULT 'pending',
            leads_found INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS import_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            rows_imported INTEGER,
            imported_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            message TEXT,
            type TEXT DEFAULT 'info',
            read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)


def import_csv_if_needed():
    if not LEADS_CSV.exists():
        return 0
    with get_db() as conn:
        already = conn.execute("SELECT COUNT(*) FROM import_log WHERE filename=?", (str(LEADS_CSV),)).fetchone()[0]
        if already:
            return 0
        count = 0
        with open(LEADS_CSV, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                # Clean first — treat "Not found" / empty as absent
                fb = _none_if_empty(row.get("social_facebook", ""))
                ig = _none_if_empty(row.get("social_instagram", ""))
                website = _none_if_empty(row.get("website", ""))
                score = _compute_score(
                    website=website,
                    fb_url=fb, fb_active=False,
                    ig_url=ig, ig_active=False,
                )
                conn.execute("""
                    INSERT INTO leads (name, owner_name, address, city, phone, email,
                        website, business_type, priority, quality_score,
                        facebook_url, instagram_url, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    name,
                    _clean(row.get("owner_name")),
                    _clean_addr(row.get("address", "")),
                    _clean(row.get("city")),
                    _clean(row.get("phone")),
                    _clean(row.get("email")),
                    _none_if_empty(website),
                    _clean(row.get("business_type")),
                    _priority_label(score),
                    score,
                    _none_if_empty(fb),
                    _none_if_empty(ig),
                    "csv_import",
                ))
                count += 1
        conn.execute("INSERT INTO import_log (filename, rows_imported) VALUES (?,?)", (str(LEADS_CSV), count))
        return count


def _clean(val):
    if not val:
        return None
    v = val.strip()
    return None if v.lower() in ("not found", "n/a", "", "none") else v


def _clean_addr(val):
    if not val:
        return None
    import re
    v = re.sub(r'[-]', '', val).strip()
    return v or None


def _none_if_empty(val):
    if not val:
        return None
    v = val.strip()
    return None if v.lower() in ("not found", "n/a", "", "none") else v


def _present(val):
    """Return True only if val is a real non-empty, non-placeholder string."""
    if not val:
        return False
    return val.strip().lower() not in ("", "not found", "n/a", "none", "false", "0")


def _compute_score(website, fb_url, fb_active, ig_url, ig_active):
    """Basic score used during CSV import (before website/social checks run)."""
    return _compute_score_full(
        website=website,
        website_live=None,
        website_grade=None,
        fb_url=fb_url,
        fb_active=fb_active,
        fb_last_post_days=None,
        fb_followers=None,
        ig_url=ig_url,
        ig_active=ig_active,
        ig_last_post_days=None,
    )


def _compute_score_full(website, website_live, website_grade,
                        fb_url, fb_active, fb_last_post_days, fb_followers,
                        fb_posts_per_month=None,
                        ig_url=None, ig_active=False, ig_last_post_days=None,
                        tiktok_url=None, tiktok_active=False, tiktok_last_post_days=None):
    """
    Full score using all available digital presence data.

    Score = 100 means "this business has ZERO online presence" → hottest lead.
    Score = 0   means "fully established online" → cold lead.

    WEBSITE  (up to -40 pts)
      No website at all            →  0 deducted  (perfect target)
      URL exists but site is dead  →  -5           (still great — they tried & failed)
      Site live but grade D/F      → -15           (embarrassing site = still good lead)
      Site live, grade C           → -25
      Site live, grade B           → -35
      Site live, grade A           → -40

    FACEBOOK  (up to -25 pts)
      No page                      →  0
      Page exists, dead 6+ months  →  -5           (abandoned = still a lead)
      Page exists, dormant 2-6mo   → -12
      Page active, low followers   → -18
      Page active, 500+ followers  → -25

    INSTAGRAM  (up to -20 pts — weighted less than FB for tradesman market)
      No profile                   →  0
      Profile dead 6+ months       →  -3
      Profile dormant              →  -8
      Profile active               → -15
      Profile active + followers   → -20

    Thresholds:
      Hot  ≥ 75   (little or no real online presence)
      Warm 45–74  (partial/weak presence)
      Cold  < 45  (established online — lowest priority)
    """
    score = 100

    # ── Website ───────────────────────────────────────────────────────────────
    if _present(website):
        if website_live is None:
            # Not yet checked — penalise moderately
            score -= 20
        elif not website_live:
            score -= 5    # Dead site — barely counts
        else:
            grade_penalty = {"A": 40, "B": 35, "C": 25, "D": 15, "F": 10, "dead": 5, "none": 0}
            score -= grade_penalty.get(website_grade or "D", 20)

    # ── Facebook ──────────────────────────────────────────────────────────────
    if _present(fb_url):
        if fb_last_post_days is None:
            score -= 8    # URL known, not yet checked
        elif fb_last_post_days > 180:
            score -= 5    # Abandoned page — barely counts against them
        elif fb_last_post_days > 90:
            score -= 10   # Very dormant
        else:
            # Active — weight by HOW consistently they post
            # fb_posts_per_month: >20=daily, 8-20=2-3x/wk, 4-8=weekly, 1-4=monthly, <1=sporadic
            ppm = fb_posts_per_month or 0
            followers = fb_followers or 0

            if ppm >= 20:       # Posting almost daily
                consistency_penalty = 28
            elif ppm >= 8:      # 2–3 times a week
                consistency_penalty = 22
            elif ppm >= 4:      # About weekly
                consistency_penalty = 16
            elif ppm >= 1:      # A few times a month
                consistency_penalty = 10
            else:               # Active but very sporadic
                consistency_penalty = 6

            # Bigger following = more established = more penalty
            if followers >= 1000:
                consistency_penalty += 5
            elif followers >= 300:
                consistency_penalty += 3

            score -= min(consistency_penalty, 30)

    # ── Instagram ─────────────────────────────────────────────────────────────
    if _present(ig_url):
        if ig_last_post_days is None:
            score -= 5
        elif ig_last_post_days > 180:
            score -= 3
        elif ig_last_post_days > 90:
            score -= 7
        elif ig_last_post_days > 30:
            score -= 12
        else:
            score -= 18   # Actively posting on IG

    # ── TikTok ────────────────────────────────────────────────────────────────
    if _present(tiktok_url):
        if tiktok_last_post_days is None:
            score -= 5    # Has TikTok, not checked yet
        elif tiktok_last_post_days > 180:
            score -= 3    # Dead TikTok
        elif tiktok_last_post_days > 60:
            score -= 8    # Dormant TikTok
        else:
            score -= 15   # Active TikTok — they're digitally savvy

    return max(0, score)


def _priority_label(score):
    if score >= 75:
        return "Hot"    # Little or no real online presence — prime targets
    if score >= 45:
        return "Warm"   # Partial/weak presence — worth reaching out
    return "Cold"       # Established online — lowest priority


def get_leads(search=None, priority=None, city=None, business_type=None,
              status=None, sort="score", page=1, per_page=30):
    clauses = ["1=1"]
    params = []
    if search:
        clauses.append("(name LIKE ? OR city LIKE ? OR owner_name LIKE ? OR business_type LIKE ?)")
        params += [f"%{search}%"] * 4
    if priority:
        clauses.append("priority = ?")
        params.append(priority)
    if city:
        clauses.append("city = ?")
        params.append(city)
    if business_type:
        clauses.append("business_type = ?")
        params.append(business_type)
    if status:
        clauses.append("outreach_status = ?")
        params.append(status)

    order = {
        "score": "quality_score DESC, qr_scanned DESC",
        "name": "name ASC",
        "city": "city ASC",
        "recent": "created_at DESC",
        "qr": "qr_scanned DESC, quality_score DESC",
    }.get(sort, "quality_score DESC")

    where = " AND ".join(clauses)
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM leads WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM leads WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()
    return [dict(r) for r in rows], total


def get_lead(lead_id):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(r) if r else None


def update_lead(lead_id, **kwargs):
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [lead_id]
    with get_db() as conn:
        conn.execute(f"UPDATE leads SET {sets} WHERE id=?", vals)


def delete_lead(lead_id):
    with get_db() as conn:
        conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))


def rescore_all_leads():
    """Re-compute quality_score and priority for every lead using full scoring logic."""
    with get_db() as conn:
        leads = conn.execute("SELECT * FROM leads").fetchall()
        for lead in leads:
            keys = lead.keys()
            score = _compute_score_full(
                website=lead["website"],
                website_live=lead["website_live"] if "website_live" in keys else None,
                website_grade=lead["website_grade"] if "website_grade" in keys else None,
                fb_url=lead["facebook_url"],
                fb_active=bool(lead["facebook_active"]),
                fb_last_post_days=lead["facebook_last_post_days"] if "facebook_last_post_days" in keys else None,
                fb_followers=lead["facebook_followers"],
                fb_posts_per_month=lead["facebook_posts_per_month"] if "facebook_posts_per_month" in keys else None,
                ig_url=lead["instagram_url"],
                ig_active=bool(lead["instagram_active"]),
                ig_last_post_days=lead["instagram_last_post_days"] if "instagram_last_post_days" in keys else None,
                tiktok_url=lead["tiktok_url"] if "tiktok_url" in keys else None,
                tiktok_active=bool(lead["tiktok_active"]) if "tiktok_active" in keys else False,
                tiktok_last_post_days=lead["tiktok_last_post_days"] if "tiktok_last_post_days" in keys else None,
            )
            conn.execute(
                "UPDATE leads SET quality_score=?, priority=?, updated_at=? WHERE id=?",
                (score, _priority_label(score), datetime.now().isoformat(), lead["id"])
            )
    return len(leads)


def get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        hot = conn.execute("SELECT COUNT(*) FROM leads WHERE priority='Hot'").fetchone()[0]
        warm = conn.execute("SELECT COUNT(*) FROM leads WHERE priority='Warm'").fetchone()[0]
        emails = conn.execute("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email!=''").fetchone()[0]
        owners = conn.execute("SELECT COUNT(*) FROM leads WHERE owner_name IS NOT NULL AND owner_name!=''").fetchone()[0]
        converted = conn.execute("SELECT COUNT(*) FROM leads WHERE outreach_status='converted'").fetchone()[0]
        contacted = conn.execute("SELECT COUNT(*) FROM leads WHERE outreach_status NOT IN ('new')").fetchone()[0]
        qr_scans = conn.execute("SELECT COUNT(*) FROM leads WHERE qr_scanned=1").fetchone()[0]
        postcard_sent = conn.execute("SELECT COUNT(*) FROM leads WHERE outreach_status IN ('postcard_sent','email_sent','called','converted')").fetchone()[0]
        call_ready = conn.execute("SELECT COUNT(*) FROM leads WHERE outreach_status='email_sent' OR qr_scanned=1").fetchone()[0]
    rate = round((converted / contacted * 100), 1) if contacted > 0 else 0
    return dict(
        total=total, hot=hot, warm=warm, emails=emails, owners=owners,
        converted=converted, conversion_rate=rate, qr_scans=qr_scans,
        postcard_sent=postcard_sent, call_ready=call_ready
    )


def get_cities():
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT city FROM leads WHERE city IS NOT NULL ORDER BY city").fetchall()
    return [r[0] for r in rows]


def get_business_types():
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT business_type FROM leads WHERE business_type IS NOT NULL ORDER BY business_type").fetchall()
    return [r[0] for r in rows]


def get_call_queue():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM leads
            WHERE (outreach_status IN ('email_sent', 'call_ready') OR qr_scanned=1)
              AND (opt_in_status IS NULL OR opt_in_status != 'opted_out')
            ORDER BY qr_scanned DESC, quality_score DESC, email_sent_date ASC
        """).fetchall()
    return [dict(r) for r in rows]


def get_notifications(unread_only=False):
    with get_db() as conn:
        q = "SELECT n.*, l.name as lead_name FROM notifications n LEFT JOIN leads l ON n.lead_id=l.id"
        if unread_only:
            q += " WHERE n.read=0"
        q += " ORDER BY n.created_at DESC LIMIT 20"
        rows = conn.execute(q).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_read():
    with get_db() as conn:
        conn.execute("UPDATE notifications SET read=1")


def add_notification(lead_id, message, ntype="info"):
    with get_db() as conn:
        conn.execute("INSERT INTO notifications (lead_id, message, type) VALUES (?,?,?)",
                     (lead_id, message, ntype))
