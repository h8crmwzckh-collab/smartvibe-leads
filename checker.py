"""
Digital presence checker — website liveness/quality + Facebook/Instagram activity.
Runs in background threads, updates leads table in real time.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.max_redirects = 5


# ─── Website ──────────────────────────────────────────────────────────────────

def check_website(url: str) -> dict:
    """
    Returns:
      live          bool — site responds with 2xx/3xx
      mobile        bool — has viewport meta tag
      grade         str  — A / B / C / D / F / dead / none
      status_code   int or None
      grade_reasons list[str]
    """
    if not url:
        return {"live": False, "mobile": False, "grade": "none",
                "status_code": None, "grade_reasons": []}

    try:
        r = SESSION.get(url, headers=HEADERS_DESKTOP, timeout=9,
                        allow_redirects=True)
        live = r.status_code < 400
    except Exception:
        return {"live": False, "mobile": False, "grade": "dead",
                "status_code": None, "grade_reasons": ["Site unreachable"]}

    # 403/429 = bot-blocked but definitely alive — treat as live grade B
    if r.status_code in (403, 429, 401):
        return {"live": True, "mobile": True, "grade": "B",
                "status_code": r.status_code,
                "grade_reasons": ["Bot-protected site (likely professional)"]}

    if not live:
        return {"live": False, "mobile": False, "grade": "dead",
                "status_code": r.status_code,
                "grade_reasons": [f"HTTP {r.status_code}"]}

    html = r.text.lower()
    reasons = []
    score = 0

    # Mobile viewport
    mobile = bool(re.search(r'<meta[^>]+name=["\']viewport', html))
    if mobile:
        score += 2
    else:
        reasons.append("Not mobile-friendly")

    # HTTPS
    if url.startswith("https://"):
        score += 1
    else:
        reasons.append("No HTTPS")

    # Contact info present
    if re.search(r'(contact|call us|phone|email us|get a quote)', html):
        score += 1
    else:
        reasons.append("No contact/quote section")

    # Has booking or CTA
    if re.search(r'(schedule|book|appointment|request|free estimate)', html):
        score += 1
    else:
        reasons.append("No booking/CTA")

    # Not a Facebook redirect / parking page
    if "facebook.com" in r.url or re.search(r'(domain for sale|parked|godaddy)', html):
        score = 0
        reasons = ["Parked or redirected to social"]

    grade = ["F", "D", "D", "C", "B", "A"][min(score, 5)]
    return {"live": True, "mobile": mobile, "grade": grade,
            "status_code": r.status_code, "grade_reasons": reasons}


# ─── Facebook ─────────────────────────────────────────────────────────────────

def check_facebook(url: str) -> dict:
    """
    Returns:
      reachable         bool
      followers         int or None
      last_post_days    int or None
      active            bool
      activity_label    str
      post_timestamps   list[int]  — days-ago for each visible post
      posts_per_month   float or None
      post_frequency    str  — 'Daily' / '2–3x/week' / 'Weekly' / 'Monthly' / 'Sporadic' / 'Dead' / 'Unknown'
      consistency_note  str  — human-readable summary for the sales call
    """
    if not url:
        return _fb_empty()

    mob_url = url.replace("www.facebook.com", "m.facebook.com")
    if "facebook.com" not in mob_url:
        return _fb_empty()

    try:
        r = SESSION.get(mob_url, headers=HEADERS_MOBILE, timeout=12,
                        allow_redirects=True)
        if r.status_code >= 400:
            return {**_fb_empty(), "reachable": False}
    except Exception:
        return _fb_empty()

    html = r.text

    # ── Follower / like count ─────────────────────────────────────────────────
    followers = None
    for pat in [
        r'([\d,]+)\s*(?:people follow|followers)',
        r'([\d,]+)\s*(?:likes|Likes)',
        r'"followers_count"\s*:\s*(\d+)',
        r'"fan_count"\s*:\s*(\d+)',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            try:
                followers = int(m.group(1).replace(",", ""))
                break
            except Exception:
                pass

    # ── Collect ALL visible post timestamps ───────────────────────────────────
    post_days_list = []

    # Relative timestamps — collect all matches (not just first)
    rel_patterns = [
        (r'(\d+)\s+hours?\s+ago',  lambda n: n / 24),
        (r'(\d+)\s+days?\s+ago',   lambda n: float(n)),
        (r'(\d+)\s+weeks?\s+ago',  lambda n: n * 7.0),
        (r'(\d+)\s+months?\s+ago', lambda n: n * 30.0),
        (r'(\d+)\s+years?\s+ago',  lambda n: n * 365.0),
    ]
    for pat, fn in rel_patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            try:
                days = fn(int(m.group(1)))
                post_days_list.append(days)
            except Exception:
                pass

    # "Yesterday" and "just now"
    post_days_list += [1.0] * len(re.findall(r'\byesterday\b', html, re.IGNORECASE))
    post_days_list += [0.0] * len(re.findall(r'\bjust now\b', html, re.IGNORECASE))

    # Unix timestamps buried in JSON (data-store attributes etc.)
    now_ts = time.time()
    for m in re.finditer(r'"(?:timestamp|created_time)"\s*:\s*(\d{10})', html):
        try:
            days = (now_ts - int(m.group(1))) / 86400
            if 0 <= days <= 3650:
                post_days_list.append(days)
        except Exception:
            pass

    # Deduplicate and sort oldest → newest
    post_days_list = sorted(set(round(d, 1) for d in post_days_list))

    # ── Derive frequency stats ─────────────────────────────────────────────────
    last_post_days = int(post_days_list[0]) if post_days_list else None

    posts_per_month = None
    post_frequency = "Unknown"
    consistency_note = "Couldn't determine posting frequency."

    if post_days_list:
        # Posts visible on the page within the last 90 days
        recent = [d for d in post_days_list if d <= 90]
        if recent:
            span = max(recent) if len(recent) > 1 else 30
            posts_per_month = round(len(recent) / max(span / 30, 1), 1)

            if posts_per_month >= 20:
                post_frequency = "Daily"
                consistency_note = f"Posting almost every day ({posts_per_month:.0f}/mo). Very active but still no website."
            elif posts_per_month >= 8:
                post_frequency = "2–3x/week"
                consistency_note = f"Posting {posts_per_month:.0f}x/month — consistent but zero website presence."
            elif posts_per_month >= 4:
                post_frequency = "Weekly"
                consistency_note = f"About once a week ({posts_per_month:.1f}/mo). Some effort but no real web presence."
            elif posts_per_month >= 1:
                post_frequency = "Monthly"
                consistency_note = f"Only {posts_per_month:.1f} posts/month — inconsistent, still needs help."
            else:
                post_frequency = "Sporadic"
                consistency_note = "Rarely posts — less than once a month."
        else:
            post_frequency = "Dead"
            consistency_note = f"Last post was {last_post_days} days ago. Page is effectively abandoned."

    elif last_post_days and last_post_days > 180:
        post_frequency = "Dead"
        consistency_note = f"Last post {last_post_days} days ago — page is abandoned."

    active, label = _activity_label(last_post_days)
    return {
        "reachable": True,
        "followers": followers,
        "last_post_days": last_post_days,
        "active": active,
        "activity_label": label,
        "post_timestamps": post_days_list[:10],
        "posts_per_month": posts_per_month,
        "post_frequency": post_frequency,
        "consistency_note": consistency_note,
    }


def _fb_empty():
    return {
        "reachable": False, "followers": None,
        "last_post_days": None, "active": False, "activity_label": "unknown",
        "post_timestamps": [], "posts_per_month": None,
        "post_frequency": "Unknown", "consistency_note": "No Facebook page found.",
    }


# ─── Instagram ────────────────────────────────────────────────────────────────

def check_instagram(url: str) -> dict:
    """
    Returns:
      reachable     bool
      followers     int or None
      last_post_days int or None
      active        bool
      activity_label str
    """
    if not url:
        return _ig_empty()

    if "instagram.com" not in url:
        return _ig_empty()

    try:
        r = SESSION.get(url, headers=HEADERS_MOBILE, timeout=10,
                        allow_redirects=True)
        if r.status_code >= 400:
            return {**_ig_empty(), "reachable": False}
    except Exception:
        return _ig_empty()

    html = r.text

    # Instagram embeds JSON in the page for server-side rendering
    followers = None
    m = re.search(r'"edge_followed_by":\{"count":(\d+)\}', html)
    if not m:
        m = re.search(r'"followers":(\d+)', html)
    if m:
        try:
            followers = int(m.group(1))
        except Exception:
            pass

    # Last post — look for timestamp patterns
    last_post_days = None
    m = re.search(r'"taken_at_timestamp":(\d+)', html)
    if m:
        try:
            ts = int(m.group(1))
            days = (datetime.now() - datetime.fromtimestamp(ts)).days
            last_post_days = max(0, days)
        except Exception:
            pass

    active, label = _activity_label(last_post_days)
    return {
        "reachable": bool(followers is not None or last_post_days is not None),
        "followers": followers,
        "last_post_days": last_post_days,
        "active": active,
        "activity_label": label,
    }


def _ig_empty():
    return {"reachable": False, "followers": None,
            "last_post_days": None, "active": False, "activity_label": "unknown"}


# ─── TikTok ───────────────────────────────────────────────────────────────────

def check_tiktok(url: str) -> dict:
    """
    Returns:
      reachable      bool
      followers      int or None
      likes_total    int or None
      last_post_days int or None
      video_count    int or None
      active         bool
      activity_label str
    """
    if not url:
        return _tiktok_empty()

    if "tiktok.com" not in url:
        return _tiktok_empty()

    try:
        r = SESSION.get(url, headers=HEADERS_MOBILE, timeout=12,
                        allow_redirects=True)
        if r.status_code >= 400:
            return {**_tiktok_empty(), "reachable": False}
    except Exception:
        return _tiktok_empty()

    html = r.text

    followers = None
    likes_total = None
    video_count = None
    last_post_days = None

    # TikTok embeds data in a __UNIVERSAL_DATA__ or __NEXT_DATA__ JSON block
    json_block = None
    for pat in [r'<script id="__UNIVERSAL_DATA__"[^>]*>(.*?)</script>',
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>']:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                json_block = json.loads(m.group(1))
                break
            except Exception:
                pass

    if json_block:
        raw = json.dumps(json_block)
        for key, target in [
            (r'"followerCount"\s*:\s*(\d+)', 'followers'),
            (r'"heartCount"\s*:\s*(\d+)', 'likes_total'),
            (r'"videoCount"\s*:\s*(\d+)', 'video_count'),
        ]:
            m = re.search(key, raw)
            if m:
                try:
                    val = int(m.group(1))
                    if target == 'followers': followers = val
                    elif target == 'likes_total': likes_total = val
                    elif target == 'video_count': video_count = val
                except Exception:
                    pass

        # Most recent video timestamp
        ts_matches = re.findall(r'"createTime"\s*:\s*"?(\d{10})"?', raw)
        if ts_matches:
            try:
                newest_ts = max(int(t) for t in ts_matches)
                last_post_days = int((time.time() - newest_ts) / 86400)
            except Exception:
                pass

    # Fallback: look for follower patterns in raw HTML
    if followers is None:
        for pat in [r'([\d,.]+[KkMm]?)\s*Followers',
                    r'"fans"\s*:\s*(\d+)',
                    r'data-e2e="followers-count"[^>]*>([^<]+)']:
            m = re.search(pat, html)
            if m:
                try:
                    followers = _parse_short_num(m.group(1))
                    break
                except Exception:
                    pass

    active, label = _activity_label(last_post_days)
    return {
        "reachable": True,
        "followers": followers,
        "likes_total": likes_total,
        "video_count": video_count,
        "last_post_days": last_post_days,
        "active": active,
        "activity_label": label,
    }


def _tiktok_empty():
    return {"reachable": False, "followers": None, "likes_total": None,
            "video_count": None, "last_post_days": None,
            "active": False, "activity_label": "unknown"}


def _parse_short_num(val: str) -> int:
    """Parse '12.3K', '1.2M', '500' → int."""
    val = str(val).strip().replace(",", "")
    if val.upper().endswith("K"):
        return int(float(val[:-1]) * 1_000)
    if val.upper().endswith("M"):
        return int(float(val[:-1]) * 1_000_000)
    return int(float(val))


def _activity_label(days):
    """Return (active_bool, label_str) based on days since last post."""
    if days is None:
        return False, "unknown"
    if days <= 30:
        return True, "active"
    if days <= 90:
        return True, "recent"
    if days <= 180:
        return False, "dormant"
    return False, "dead"


# ─── Social media discovery ───────────────────────────────────────────────────

def discover_social_urls(lead: dict) -> dict:
    """
    Google-search for a business's Facebook, Instagram, and TikTok pages.
    Returns dict with facebook_url, instagram_url, tiktok_url (or None if not found).
    """
    name = lead.get("name", "")
    city = lead.get("city", "")
    results = {}

    platforms = [
        ("facebook_url",  f'site:facebook.com "{name}" "{city}"',  "facebook.com"),
        ("instagram_url", f'site:instagram.com "{name}" "{city}"', "instagram.com"),
        ("tiktok_url",    f'site:tiktok.com "{name}"',             "tiktok.com"),
    ]

    for field, query, domain in platforms:
        if lead.get(field):   # already have it, skip
            continue
        try:
            search_url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num=3"
            r = SESSION.get(search_url, headers=HEADERS_DESKTOP, timeout=8)
            # Extract first matching URL from search results
            matches = re.findall(
                rf'https?://(?:www\.)?{re.escape(domain)}/[^\s"&<>]+',
                r.text
            )
            for url in matches:
                # Skip generic pages, ads, share links
                if any(x in url for x in ["/sharer", "/share/", "ads.", "l.facebook", "/watch", "/hashtag"]):
                    continue
                # Must look like a real page (facebook.com/PageName or /pages/...)
                if domain == "facebook.com" and not re.search(r'facebook\.com/(?!events|groups|marketplace|login|watch)', url):
                    continue
                results[field] = url.split("?")[0]  # strip tracking params
                break
            time.sleep(1.5)  # respect Google rate limits
        except Exception:
            pass

    return results


# ─── Full lead check ──────────────────────────────────────────────────────────

def check_lead(lead: dict) -> dict:
    """Run all checks for a single lead. Returns update dict for database."""
    updates = {"last_checked": datetime.now().isoformat(), "check_status": "done"}

    # Website
    web = check_website(lead.get("website"))
    updates["website_live"] = 1 if web["live"] else 0
    updates["website_mobile"] = 1 if web["mobile"] else 0
    updates["website_grade"] = web["grade"]

    # Facebook (with consistency analysis)
    fb = check_facebook(lead.get("facebook_url"))
    if lead.get("facebook_url"):
        updates["facebook_active"] = 1 if fb["active"] else 0
        if fb["followers"] is not None:
            updates["facebook_followers"] = fb["followers"]
        if fb["last_post_days"] is not None:
            updates["facebook_last_post_days"] = fb["last_post_days"]
            updates["facebook_last_post"] = fb["activity_label"]
        if fb["posts_per_month"] is not None:
            updates["facebook_posts_per_month"] = fb["posts_per_month"]
        if fb["post_frequency"]:
            updates["facebook_post_frequency"] = fb["post_frequency"]

    # Instagram
    ig = check_instagram(lead.get("instagram_url"))
    if lead.get("instagram_url"):
        updates["instagram_active"] = 1 if ig["active"] else 0
        if ig["followers"] is not None:
            updates["instagram_followers"] = ig["followers"]
        if ig["last_post_days"] is not None:
            updates["instagram_last_post_days"] = ig["last_post_days"]
            updates["instagram_last_post"] = ig["activity_label"]

    # TikTok
    tt = check_tiktok(lead.get("tiktok_url"))
    if lead.get("tiktok_url"):
        updates["tiktok_active"] = 1 if tt["active"] else 0
        if tt["followers"] is not None:
            updates["tiktok_followers"] = tt["followers"]
        if tt["last_post_days"] is not None:
            updates["tiktok_last_post_days"] = tt["last_post_days"]

    return updates


# ─── Bulk check (background) ──────────────────────────────────────────────────

_bulk_status = {"running": False, "total": 0, "done": 0, "errors": 0}


def get_bulk_status():
    return dict(_bulk_status)


def run_bulk_check(lead_ids=None):
    """Start background bulk check. Pass None to check all leads."""
    if _bulk_status["running"]:
        return False
    t = threading.Thread(target=_bulk_worker, args=(lead_ids,), daemon=True)
    t.start()
    return True


def _bulk_worker(lead_ids):
    from database import get_db, update_lead, rescore_all_leads, _compute_score_full, _priority_label

    _bulk_status["running"] = True
    _bulk_status["errors"] = 0
    _bulk_status["done"] = 0

    with get_db() as conn:
        if lead_ids:
            placeholders = ",".join("?" * len(lead_ids))
            leads = conn.execute(
                f"SELECT * FROM leads WHERE id IN ({placeholders})", lead_ids
            ).fetchall()
        else:
            leads = conn.execute("SELECT * FROM leads").fetchall()

    _bulk_status["total"] = len(leads)

    for row in leads:
        lead = dict(row)
        try:
            conn2 = None
            # Discover social URLs if not already set
            discovered = discover_social_urls(lead)
            if discovered:
                update_lead(lead["id"], **discovered)
                lead = {**lead, **discovered}
            updates = check_lead(lead)
            update_lead(lead["id"], **updates)

            # Re-score with fresh data
            fresh = {**lead, **updates}
            score = _compute_score_full(
                website=fresh.get("website"),
                website_live=bool(fresh.get("website_live")),
                website_grade=fresh.get("website_grade"),
                fb_url=fresh.get("facebook_url"),
                fb_active=bool(fresh.get("facebook_active")),
                fb_last_post_days=fresh.get("facebook_last_post_days"),
                fb_followers=fresh.get("facebook_followers"),
                fb_posts_per_month=fresh.get("facebook_posts_per_month"),
                ig_url=fresh.get("instagram_url"),
                ig_active=bool(fresh.get("instagram_active")),
                ig_last_post_days=fresh.get("instagram_last_post_days"),
                tiktok_url=fresh.get("tiktok_url"),
                tiktok_active=bool(fresh.get("tiktok_active")),
                tiktok_last_post_days=fresh.get("tiktok_last_post_days"),
            )
            update_lead(lead["id"],
                        quality_score=score,
                        priority=_priority_label(score))
        except Exception:
            _bulk_status["errors"] += 1
        finally:
            _bulk_status["done"] += 1
        time.sleep(0.5)   # be polite to servers

    _bulk_status["running"] = False
