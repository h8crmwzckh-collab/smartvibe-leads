#!/usr/bin/env python3
"""SmartVibe Leads — SaaS Lead Management"""

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

load_dotenv(Path(__file__).parent / ".env")

import database as db
_POSTCARD_LOGO_URL = "https://res.cloudinary.com/dpkrpt7p7/image/upload/v1783892029/smartvibe_logo.png"
from database import (add_notification, delete_lead, get_business_types,
                      get_call_queue, get_cities, get_db, get_lead, get_leads,
                      get_notifications, get_stats, init_db, import_csv_if_needed,
                      mark_notifications_read, rescore_all_leads, update_lead)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "smartvibe-leads-2026-secret")

STATIC_DIR = Path(__file__).parent / "static"


_rescored = False

@app.before_request
def setup():
    global _rescored
    init_db()
    if not _rescored:
        _rescored = True
        rescore_all_leads()


@app.route("/api/rescore", methods=["POST"])
def api_rescore():
    count = rescore_all_leads()
    return jsonify({"ok": True, "rescored": count})


@app.route("/api/bulk-check", methods=["POST"])
def api_bulk_check():
    from checker import run_bulk_check, get_bulk_status
    lead_ids_raw = request.json.get("lead_ids") if request.is_json else None
    started = run_bulk_check(lead_ids_raw)
    return jsonify({"ok": True, "started": started, **get_bulk_status()})


@app.route("/api/bulk-check/status")
def api_bulk_check_status():
    from checker import get_bulk_status
    return jsonify(get_bulk_status())


@app.route("/api/check-lead/<int:lead_id>", methods=["POST"])
def api_check_lead(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        abort(404)
    from checker import check_lead
    from database import _compute_score_full, _priority_label
    update_lead(lead_id, check_status="checking")
    updates = check_lead(lead)
    score = _compute_score_full(
        website=lead.get("website"),
        website_live=updates.get("website_live"),
        website_grade=updates.get("website_grade"),
        fb_url=lead.get("facebook_url"),
        fb_active=bool(updates.get("facebook_active", lead.get("facebook_active"))),
        fb_last_post_days=updates.get("facebook_last_post_days"),
        fb_followers=updates.get("facebook_followers", lead.get("facebook_followers")),
        fb_posts_per_month=updates.get("facebook_posts_per_month", lead.get("facebook_posts_per_month")),
        ig_url=lead.get("instagram_url"),
        ig_active=bool(updates.get("instagram_active", lead.get("instagram_active"))),
        ig_last_post_days=updates.get("instagram_last_post_days"),
        tiktok_url=updates.get("tiktok_url", lead.get("tiktok_url")),
        tiktok_active=bool(updates.get("tiktok_active", lead.get("tiktok_active"))),
        tiktok_last_post_days=updates.get("tiktok_last_post_days", lead.get("tiktok_last_post_days")),
    )
    updates["quality_score"] = score
    updates["priority"] = _priority_label(score)
    update_lead(lead_id, **updates)
    return jsonify({"ok": True, "score": score, "priority": updates["priority"]})


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    imported = import_csv_if_needed()
    stats = get_stats()
    hot_leads, _ = get_leads(priority="Hot", per_page=5)
    recent, _ = get_leads(sort="recent", per_page=5)
    notifications = get_notifications(unread_only=True)
    return render_template("dashboard.html",
                           stats=stats, hot_leads=hot_leads,
                           recent_leads=recent, notifications=notifications,
                           imported=imported)


# ─── Leads ────────────────────────────────────────────────────────────────────

@app.route("/leads")
def leads_list():
    search = request.args.get("q", "")
    priority = request.args.get("priority", "")
    city = request.args.get("city", "")
    btype = request.args.get("type", "")
    status = request.args.get("status", "")
    sort = request.args.get("sort", "score")
    page = int(request.args.get("page", 1))

    leads, total = get_leads(
        search=search, priority=priority, city=city,
        business_type=btype, status=status, sort=sort,
        page=page, per_page=25
    )
    pages = (total + 24) // 25
    cities = get_cities()
    btypes = get_business_types()

    return render_template("leads.html",
                           leads=leads, total=total, page=page, pages=pages,
                           cities=cities, btypes=btypes,
                           search=search, priority=priority, city=city,
                           btype=btype, status=status, sort=sort)


@app.route("/leads/<int:lead_id>")
def lead_detail(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        abort(404)
    import json as _json
    email_body = None
    if lead.get("preview_url"):
        from ai_tools import generate_cold_email
        try:
            email_body = generate_cold_email(lead)
        except Exception:
            pass
    recs = []
    if lead.get("ai_recommendations"):
        try:
            recs = _json.loads(lead["ai_recommendations"])
        except Exception:
            pass
    return render_template("lead_detail.html", lead=lead, email_body=email_body, recommendations=recs)


@app.route("/leads/<int:lead_id>/update", methods=["POST"])
def lead_update(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        abort(404)
    fields = ["name", "owner_name", "address", "city", "phone", "email",
              "website", "business_type", "notes", "outreach_status"]
    updates = {f: request.form.get(f) for f in fields if request.form.get(f) is not None}
    if "outreach_status" in updates:
        status = updates["outreach_status"]
        now = datetime.now().isoformat()
        if status == "postcard_sent" and not lead.get("postcard_sent_date"):
            updates["postcard_sent_date"] = now
        elif status == "email_sent" and not lead.get("email_sent_date"):
            updates["email_sent_date"] = now
        elif status == "called" and not lead.get("called_date"):
            updates["called_date"] = now
        elif status == "converted" and not lead.get("converted_date"):
            updates["converted_date"] = now
    update_lead(lead_id, **updates)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
def lead_delete(lead_id):
    delete_lead(lead_id)
    return redirect(url_for("leads_list"))


@app.route("/leads/new", methods=["GET", "POST"])
def lead_new():
    if request.method == "POST":
        from database import _compute_score, _priority_label
        website = request.form.get("website") or None
        score = _compute_score(website=website, fb_url=None, fb_active=False,
                               ig_url=None, ig_active=False)
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO leads (name, owner_name, address, city, phone, email,
                    website, business_type, priority, quality_score, notes, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                request.form.get("name"),
                request.form.get("owner_name") or None,
                request.form.get("address") or None,
                request.form.get("city") or None,
                request.form.get("phone") or None,
                request.form.get("email") or None,
                website,
                request.form.get("business_type") or None,
                _priority_label(score),
                score,
                request.form.get("notes") or None,
                "manual",
            ))
            new_id = cur.lastrowid
        return redirect(url_for("lead_detail", lead_id=new_id))
    return render_template("lead_new.html")


# ─── Preview + QR ─────────────────────────────────────────────────────────────

@app.route("/leads/<int:lead_id>/generate-preview", methods=["POST"])
def generate_preview(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        abort(404)
    from ai_tools import generate_preview_site, generate_qr_code
    preview_url, html = generate_preview_site(lead)
    qr_path = generate_qr_code(lead, preview_url)
    update_lead(lead_id, preview_url=preview_url, preview_html=html, qr_code_path=qr_path)
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/recommendations", methods=["POST"])
def generate_lead_recommendations(lead_id):
    import json
    lead = get_lead(lead_id)
    if not lead:
        abort(404)
    from ai_tools import generate_recommendations
    recs = generate_recommendations(lead)
    update_lead(lead_id, ai_recommendations=json.dumps(recs))
    return jsonify({"ok": True, "recommendations": recs})


@app.route("/preview/<path:slug>")
def view_preview(slug):
    from pathlib import Path
    preview_file = STATIC_DIR / "previews" / f"{slug}.html"
    if preview_file.exists():
        return preview_file.read_text(encoding="utf-8")
    abort(404)


@app.route("/static/qrcodes/<path:filename>")
def serve_qr(filename):
    return send_from_directory(STATIC_DIR / "qrcodes", filename)


@app.route("/static/previews/<path:filename>")
def serve_preview_file(filename):
    return send_from_directory(STATIC_DIR / "previews", filename)


# ─── Lob Postcard ─────────────────────────────────────────────────────────────

_TRADE_UNSPLASH_QUERIES = {
    "plumber":      "plumber plumbing pipes repair",
    "plumbing":     "plumber plumbing pipes repair",
    "hvac":         "hvac air conditioning heating",
    "electrician":  "electrician wiring electrical",
    "electrical":   "electrician wiring electrical",
    "roofer":       "roofing contractor roof shingles",
    "roofing":      "roofing contractor roof shingles",
    "landscaper":   "landscaping lawn garden",
    "landscaping":  "landscaping lawn garden",
    "painter":      "painting contractor house paint",
    "painting":     "painting contractor house paint",
    "concrete":     "concrete driveway patio",
    "excavation":   "excavation construction equipment",
    "contractor":   "construction contractor renovation",
}

# Multiple palette variants per trade so two businesses in the same trade look different
_TRADE_PALETTES = {
    "plumber":     [
        {"primary": "#1a3a5c", "accent": "#2196f3", "light": "#e8f4fd"},
        {"primary": "#0d2137", "accent": "#00acc1", "light": "#e0f7fa"},
        {"primary": "#1c2e4a", "accent": "#42a5f5", "light": "#e3f2fd"},
    ],
    "plumbing":    [
        {"primary": "#1a3a5c", "accent": "#2196f3", "light": "#e8f4fd"},
        {"primary": "#0d2137", "accent": "#00acc1", "light": "#e0f7fa"},
        {"primary": "#1c2e4a", "accent": "#42a5f5", "light": "#e3f2fd"},
    ],
    "hvac":        [
        {"primary": "#7a2200", "accent": "#ff6d00", "light": "#fff3e0"},
        {"primary": "#4a1500", "accent": "#ff8f00", "light": "#fff8e1"},
        {"primary": "#6d1f00", "accent": "#f4511e", "light": "#fbe9e7"},
    ],
    "electrician": [
        {"primary": "#1a1a2e", "accent": "#f0c040", "light": "#fffde7"},
        {"primary": "#12122a", "accent": "#ffd600", "light": "#fff9c4"},
        {"primary": "#0f0f1e", "accent": "#ffab00", "light": "#fff8e1"},
    ],
    "electrical":  [
        {"primary": "#1a1a2e", "accent": "#f0c040", "light": "#fffde7"},
        {"primary": "#12122a", "accent": "#ffd600", "light": "#fff9c4"},
        {"primary": "#0f0f1e", "accent": "#ffab00", "light": "#fff8e1"},
    ],
    "roofer":      [
        {"primary": "#2c2416", "accent": "#a0522d", "light": "#fdf6ec"},
        {"primary": "#1e1a10", "accent": "#795548", "light": "#efebe9"},
        {"primary": "#33291a", "accent": "#bf360c", "light": "#fbe9e7"},
    ],
    "roofing":     [
        {"primary": "#2c2416", "accent": "#a0522d", "light": "#fdf6ec"},
        {"primary": "#1e1a10", "accent": "#795548", "light": "#efebe9"},
        {"primary": "#33291a", "accent": "#bf360c", "light": "#fbe9e7"},
    ],
    "landscaper":  [
        {"primary": "#1b3a1b", "accent": "#4caf50", "light": "#f1f8e9"},
        {"primary": "#0f2b0f", "accent": "#2e7d32", "light": "#e8f5e9"},
        {"primary": "#1a3320", "accent": "#66bb6a", "light": "#f1f8e9"},
    ],
    "landscaping": [
        {"primary": "#1b3a1b", "accent": "#4caf50", "light": "#f1f8e9"},
        {"primary": "#0f2b0f", "accent": "#2e7d32", "light": "#e8f5e9"},
        {"primary": "#1a3320", "accent": "#66bb6a", "light": "#f1f8e9"},
    ],
    "painter":     [
        {"primary": "#2d1457", "accent": "#9c27b0", "light": "#f3e5f5"},
        {"primary": "#1a0a3d", "accent": "#7b1fa2", "light": "#f3e5f5"},
        {"primary": "#3a1a6e", "accent": "#ce93d8", "light": "#fce4ec"},
    ],
    "painting":    [
        {"primary": "#2d1457", "accent": "#9c27b0", "light": "#f3e5f5"},
        {"primary": "#1a0a3d", "accent": "#7b1fa2", "light": "#f3e5f5"},
        {"primary": "#3a1a6e", "accent": "#ce93d8", "light": "#fce4ec"},
    ],
    "concrete":    [
        {"primary": "#263238", "accent": "#78909c", "light": "#eceff1"},
        {"primary": "#1c2a30", "accent": "#546e7a", "light": "#cfd8dc"},
        {"primary": "#2e3c43", "accent": "#90a4ae", "light": "#eceff1"},
    ],
    "excavation":  [
        {"primary": "#3e2000", "accent": "#ff8f00", "light": "#fff8e1"},
        {"primary": "#2d1700", "accent": "#f57c00", "light": "#fff3e0"},
        {"primary": "#4a2800", "accent": "#ffa000", "light": "#fff8e1"},
    ],
    "contractor":  [
        {"primary": "#0f2027", "accent": "#00bcd4", "light": "#e0f7fa"},
        {"primary": "#0a1a24", "accent": "#0097a7", "light": "#e0f7fa"},
        {"primary": "#152535", "accent": "#26c6da", "light": "#e0f7fa"},
    ],
}
_DEFAULT_PALETTES = [
    {"primary": "#0f2027", "accent": "#00bcd4", "light": "#e0f7fa"},
    {"primary": "#1a0a3d", "accent": "#7b1fa2", "light": "#f3e5f5"},
    {"primary": "#1b3a1b", "accent": "#4caf50", "light": "#f1f8e9"},
]


def _pick_palette(trade: str, seed: str) -> dict:
    """Pick a palette variant deterministically from the business name so it's stable across re-registrations."""
    import hashlib
    trade_lower = (trade or "").lower()
    variants = next(
        (v for k, v in _TRADE_PALETTES.items() if k in trade_lower),
        _DEFAULT_PALETTES
    )
    idx = int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(variants)
    return variants[idx]


def _generate_preview_copy(name: str, trade: str, city: str) -> dict:
    """Use Claude Haiku to generate content for this business's own website preview."""
    import anthropic as _ant
    client = _ant.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    prompt = f"""You are writing content for a {trade} business's own website. This should look and read like THEIR real homepage — not an ad for any other company.

Business name: {name}
Trade: {trade}
City: {city}

Return ONLY valid JSON with these exact keys:
{{
  "hero_headline": "The business's main tagline. Bold, confident, 6-10 words. Mentions {city}. Sounds like a real {trade} company's homepage. Example: 'Colorado Springs Trusted Plumber Since Day One'",
  "hero_sub": "One line under the tagline. What they do and where. 10-15 words max. No fluff.",
  "about": "2 sentences written as if the business owner is talking. Mentions how long they've served {city}, what they specialize in, and that they're local. Warm and real.",
  "services": [
    {{"name": "Service name", "desc": "One sentence description"}},
    {{"name": "Service name", "desc": "One sentence description"}},
    {{"name": "Service name", "desc": "One sentence description"}},
    {{"name": "Service name", "desc": "One sentence description"}},
    {{"name": "Service name", "desc": "One sentence description"}},
    {{"name": "Service name", "desc": "One sentence description"}}
  ],
  "service_areas": ["List", "of", "5-7", "real", "nearby", "cities", "and", "neighborhoods", "near", "{city}", "Colorado"],
  "review_1": {{"author": "Realistic Colorado first + last name", "text": "1-2 sentences praising the {trade} work specifically. Mentions a real job type. Sounds like a genuine Google review.", "stars": 5}},
  "review_2": {{"author": "Different realistic name", "text": "Different job, different angle. Still specific and believable as a real review.", "stars": 5}},
  "review_3": {{"author": "Third realistic name", "text": "Another genuine-sounding review. Maybe mentions speed, price, or professionalism.", "stars": 5}}
}}

Services should be real {trade} services — specific and practical, not generic. Make the reviews sound like real homeowners, not marketing copy."""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        print(f"[Claude copy] generation failed: {e}")
        return {
            "hero_headline": f"{city}'s Reliable {trade.title()} — Call Today",
            "hero_sub": f"Fast, honest {trade} service across {city} and surrounding areas.",
            "about": f"We've been serving {city} homeowners and businesses for years. Our team takes pride in doing the job right the first time.",
            "services": [
                {"name": "Emergency Service", "desc": "Available when you need us most."},
                {"name": "Repairs", "desc": "Fast, reliable fixes that last."},
                {"name": "Installations", "desc": "Professional installation done right."},
                {"name": "Inspections", "desc": "Thorough inspections you can trust."},
            ],
            "service_areas": [city, "Fountain", "Pueblo", "Monument", "Falcon", "Peyton"],
            "review_1": {"author": "Dave M.", "text": "Called them on a Sunday and they showed up within the hour. Great work, fair price.", "stars": 5},
            "review_2": {"author": "Linda R.", "text": "Very professional and cleaned up after themselves. Will definitely use again.", "stars": 5},
            "review_3": {"author": "Carlos T.", "text": "Fixed what two other companies couldn't figure out. Highly recommend.", "stars": 5},
        }


def _fetch_trade_images(trade: str, count: int = 4) -> list:
    """Return up to `count` landscape Unsplash photo URLs for the trade."""
    import requests as _req
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    if not key:
        return []
    trade_lower = (trade or "").lower()
    query = next(
        (v for k, v in _TRADE_UNSPLASH_QUERIES.items() if k in trade_lower),
        "contractor construction professional"
    )
    try:
        resp = _req.get(
            "https://api.unsplash.com/photos/random",
            params={"query": query, "orientation": "landscape",
                    "content_filter": "high", "count": count},
            headers={"Authorization": f"Client-ID {key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            photos = resp.json()
            if isinstance(photos, dict):
                photos = [photos]
            urls = [p.get("urls", {}).get("regular", "") for p in photos if p.get("urls", {}).get("regular")]
            print(f"[Unsplash] {len(urls)}/{count} images for '{trade}'")
            return urls
    except Exception as e:
        print(f"[Unsplash] fetch failed for '{query}': {e}")
    return []


def _register_preview_on_replit(lead):
    """Push lead data to Replit so it can serve the preview page publicly."""
    import re, requests as _req
    name  = lead.get("name", "")
    trade = lead.get("business_type", "")
    city  = lead.get("city", "Colorado Springs")
    slug  = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    preview_url = f"https://smartvibe.social/preview/{slug}"

    import concurrent.futures as _cf
    palette = _pick_palette(trade, name)
    with _cf.ThreadPoolExecutor(max_workers=2) as _pool:
        _img_fut  = _pool.submit(_fetch_trade_images, trade, 4)
        _copy_fut = _pool.submit(_generate_preview_copy, name, trade, city)
        images = _img_fut.result()
        copy   = _copy_fut.result()

    print(f"[Preview] {name} | palette primary={palette['primary']} | {len(images)} images")

    try:
        _req.post(
            "https://smartvibe.social/api/register-lead",
            json={
                "slug":      slug,
                "name":      name,
                "trade":     trade,
                "city":      city,
                "phone":     lead.get("phone", ""),
                "lead_id":   lead.get("id"),
                "image_url": images[0] if images else "",
                "images":    images,
                "palette":   palette,
                "copy":      copy,
            },
            timeout=15
        )
    except Exception as e:
        print(f"[Replit] preview register failed: {e}")
    return preview_url, slug


@app.route("/leads/<int:lead_id>/send-postcard", methods=["POST"])
def send_postcard(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        abort(404)

    lob_key = os.environ.get("LOB_API_KEY", "")
    if not lob_key:
        return jsonify({"error": "LOB_API_KEY not configured"}), 400

    # Register preview on Replit so QR scan lands on public preview page
    preview_url, slug = _register_preview_on_replit(lead)
    update_lead(lead_id, preview_url=preview_url)
    lead = get_lead(lead_id)

    try:
        import lob
        lob.api_key = lob_key

        address_raw = lead.get("address") or ""
        address_parts = [p.strip() for p in address_raw.split(",")]
        city = lead.get("city") or (address_parts[1] if len(address_parts) > 1 else "Colorado Springs")
        address_line1 = address_parts[0] if address_parts and address_parts[0] else "Address on file"
        # Parse state and zip from last segment e.g. "CO 80903"
        state, zip_code = "CO", "80901"
        if len(address_parts) > 2:
            state_zip = address_parts[-1].strip().split()
            if len(state_zip) >= 2:
                state, zip_code = state_zip[0][:2], state_zip[1]
            elif len(state_zip) == 1 and state_zip[0].isdigit():
                zip_code = state_zip[0]

        front_html = _build_postcard_front(lead)
        back_html = _build_postcard_back(lead)

        postcard = lob.Postcard.create(
            description=f"SmartVibe — {lead['name']}",
            to_address={
                "name": lead["name"],
                "address_line1": address_line1,
                "address_city": city,
                "address_state": state,
                "address_zip": zip_code,
                "address_country": "US",
            },
            from_address={
                "name": "SmartVibe Social",
                "address_line1": os.environ.get("FROM_ADDRESS_LINE1", "PO Box 1"),
                "address_city": os.environ.get("FROM_ADDRESS_CITY", "Colorado Springs"),
                "address_state": "CO",
                "address_zip": os.environ.get("FROM_ADDRESS_ZIP", "80901"),
                "address_country": "US",
            },
            front=front_html,
            back=back_html,
            size="4x6",
            use_type="marketing",
        )

        update_lead(lead_id, lob_postcard_id=postcard.id,
                    outreach_status="postcard_sent",
                    postcard_sent_date=datetime.now().isoformat())
        add_notification(lead_id, f"Postcard mailed to {lead['name']}", "success")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def _build_postcard_front(lead):
    name = lead.get("name", "")
    lead_id = lead.get("id", "0")
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", (lead.get("name") or "").lower()).strip("-")
    scan_url = f"https://smartvibe.social/preview/{slug}?ref={lead_id}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&margin=4&data={scan_url}"
    logo = _POSTCARD_LOGO_URL
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  html,body{{margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
  @page{{size:6.25in 4.25in;margin:0;}}
</style>
</head>
<body>
<table cellpadding="0" cellspacing="0" border="0"
  style="width:6.25in;height:4.25in;background:#120a2e;font-family:Arial,Helvetica,sans-serif;">
  <tr>
    <!-- LEFT: copy -->
    <td style="width:58%;vertical-align:middle;padding:30px 16px 30px 34px;">

      <!-- Logo + badge -->
      <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:12px;">
        <tr>
          <td style="vertical-align:middle;padding-right:10px;">
            <img src="{logo}" width="80" height="80" style="display:block;" />
          </td>
          <td style="vertical-align:middle;">
            <span style="color:#f5c518;border:1.5px solid #f5c518;border-radius:20px;
              padding:4px 12px;font-size:10pt;font-weight:800;letter-spacing:.05em;">
              SMARTVIBE SOCIAL
            </span>
          </td>
        </tr>
      </table>

      <!-- Gold accent -->
      <div style="width:36px;height:3px;background:#f5c518;border-radius:2px;margin-bottom:14px;"></div>

      <!-- Headline -->
      <div style="color:#ffffff;font-size:28pt;font-weight:900;line-height:1.0;
                  letter-spacing:-0.02em;margin-bottom:12px;">
        Your phone<br>rang while you<br>were on<br>
        <span style="color:#f5c518;">that last job.</span>
      </div>

      <!-- Body -->
      <div style="color:#c8c0e0;font-size:10pt;line-height:1.4;margin-bottom:14px;">
        Every missed call is a lead calling your competitor.
        We built <strong style="color:#ffffff;">{name}</strong>
        a free preview of what your business could look like
        with the right systems behind it.
      </div>

      <!-- CTA -->
      <div style="color:#f5c518;font-size:10pt;font-weight:800;">
        Scan the QR code &rarr; See your free site
      </div>

    </td>

    <!-- RIGHT: QR card -->
    <td style="width:42%;vertical-align:middle;text-align:center;padding:24px 28px 24px 8px;">
      <table cellpadding="0" cellspacing="0" border="0"
        style="background:#ffffff;border:2px solid #f5c518;border-radius:14px;
               margin:0 auto;width:175px;">
        <tr>
          <td style="padding:16px 16px 10px;text-align:center;">
            <img src="{qr_url}" width="155" height="155" style="display:block;margin:0 auto;" />
            <div style="color:#d4a008;font-size:13pt;font-weight:900;
                        margin-top:10px;line-height:1.1;">
              Free Preview Site &rarr;
            </div>
            <div style="color:#555;font-size:9pt;font-weight:700;margin-top:4px;">
              smartvibe.social
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body></html>"""


def _build_postcard_back(lead):
    name = lead.get("name", "")
    city = lead.get("city", "Colorado Springs") or "Colorado Springs"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  html,body{{margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
  @page{{size:6.25in 4.25in;margin:0;}}
</style>
</head>
<body>
<table cellpadding="0" cellspacing="0" border="0"
  style="width:6.25in;height:4.25in;font-family:Arial,Helvetica,sans-serif;table-layout:fixed;">
  <!-- TOP ROW: full-width dark content band -->
  <tr>
    <td colspan="2" style="background:#120a2e;padding:20px 28px 16px 28px;vertical-align:top;">

      <!-- Headline + body side by side -->
      <table cellpadding="0" cellspacing="0" border="0" style="width:100%;">
        <tr>
          <td style="vertical-align:top;width:55%;padding-right:24px;">
            <div style="color:#ffffff;font-size:15pt;font-weight:900;line-height:1.1;margin-bottom:8px;">
              We noticed something about <span style="color:#f5c518;">{name}.</span>
            </div>
            <div style="color:#c0b8d8;font-size:8.5pt;line-height:1.45;">
              When customers in <strong style="color:#f5c518;">{city}</strong> search for
              your services, what do they find? We checked — and we built you a free preview
              of what your business could look like online.
            </div>
          </td>
          <td style="vertical-align:top;width:45%;">
            <div style="color:#f5c518;font-size:7.5pt;font-weight:900;letter-spacing:.08em;margin-bottom:8px;">HERE'S ALL YOU DO:</div>
            <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:6px;">
              <tr>
                <td style="vertical-align:top;padding-right:7px;">
                  <div style="background:#f5c518;color:#120a2e;font-size:7pt;font-weight:900;
                              width:16px;height:16px;border-radius:50%;text-align:center;line-height:16px;">1</div>
                </td>
                <td style="vertical-align:middle;">
                  <span style="color:#c0b8d8;font-size:8.5pt;">Flip over and scan the QR code</span>
                </td>
              </tr>
            </table>
            <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:10px;">
              <tr>
                <td style="vertical-align:top;padding-right:7px;">
                  <div style="background:#f5c518;color:#120a2e;font-size:7pt;font-weight:900;
                              width:16px;height:16px;border-radius:50%;text-align:center;line-height:16px;">2</div>
                </td>
                <td style="vertical-align:middle;">
                  <span style="color:#c0b8d8;font-size:8.5pt;">See your free preview — no login, no credit card</span>
                </td>
              </tr>
            </table>
            <div style="color:#8d79c9;font-size:7.5pt;">smartvibe.social &nbsp;&middot;&nbsp; Colorado Springs, CO</div>
          </td>
        </tr>
      </table>

    </td>
  </tr>

  <!-- BOTTOM ROW: full-width white USPS zone — Lob fills this -->
  <tr>
    <td colspan="2" style="background:#ffffff;"></td>
  </tr>
</table>
</body></html>"""


# ─── Lob Webhook (QR scan) ────────────────────────────────────────────────────

@app.route("/webhook/lob", methods=["POST"])
def lob_webhook():
    """Lob fires this when a postcard is delivered via USPS tracking."""
    data = request.get_json(silent=True) or {}
    event_type = data.get("event_type", {}).get("id", "")
    if "postcard" in event_type and "delivered" in event_type:
        resource = data.get("body", {})
        postcard_id = resource.get("id")
        if postcard_id:
            with get_db() as conn:
                lead_row = conn.execute(
                    "SELECT id, name, email, phone FROM leads WHERE lob_postcard_id=?",
                    (postcard_id,)
                ).fetchone()
            if lead_row:
                delivered_date = datetime.now().isoformat()
                update_lead(lead_row["id"],
                            follow_up_stage="delivered",
                            lob_delivered_date=delivered_date)
                add_notification(
                    lead_row["id"],
                    f"Postcard delivered to {lead_row['name']} — email sequence starting.",
                    "info"
                )
                # Trigger Instantly email sequence now that postcard is in their hands
                _add_to_instantly_sequence(lead_row)
    return jsonify({"ok": True})


def _add_to_instantly_sequence(lead):
    """Add a contact to the Instantly follow-up sequence after postcard delivery."""
    import requests as _requests
    api_key = os.environ.get("INSTANTLY_API_KEY", "")
    campaign_id = os.environ.get("INSTANTLY_CAMPAIGN_ID", "")
    if not api_key or not campaign_id or not lead.get("email"):
        return
    try:
        _requests.post(
            "https://api.instantly.ai/api/v1/lead/add",
            json={
                "api_key": api_key,
                "campaign_id": campaign_id,
                "skip_if_in_workspace": True,
                "leads": [{
                    "email": lead["email"],
                    "first_name": (lead.get("name") or "").split()[0],
                    "company_name": lead.get("name", ""),
                    "phone": lead.get("phone", ""),
                }]
            },
            timeout=10
        )
    except Exception as e:
        print(f"[Instantly] Failed to add {lead.get('name')}: {e}")


@app.route("/api/debug-slack")
def debug_slack():
    import requests as _req
    token   = os.environ.get("SLACK_TOKEN", "")
    channel = os.environ.get("SLACK_LEADS_CHANNEL", "")
    try:
        resp = _req.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": ":white_check_mark: Railway Slack test"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        slack_result = resp.text
    except Exception as e:
        slack_result = str(e)
    return jsonify({"token_prefix": token[:12] if token else "MISSING", "channel": channel or "MISSING", "slack_response": slack_result})


def _slack_notify_lead(name: str, phone: str, email: str, business: str):
    """Fire a Slack message to #leads when a preview form is submitted."""
    import requests as _req
    token   = os.environ.get("SLACK_TOKEN", "")
    channel = os.environ.get("SLACK_LEADS_CHANNEL", "leads")
    if not token:
        return
    text = (
        f":fire: *New lead from preview site!*\n"
        f">*Business:* {business}\n"
        f">*Name:* {name}\n"
        f">*Phone:* {phone}\n"
        f">*Email:* {email or '—'}"
    )
    try:
        _req.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": text, "unfurl_links": False},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
    except Exception as e:
        print(f"[Slack] lead notify failed: {e}")


@app.route("/api/inbound-lead", methods=["POST"])
def api_inbound_lead():
    """Receives form submissions from smartvibe.social landing page."""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    business = data.get("business", "").strip()
    trade = data.get("trade", "").strip()
    city = data.get("city", "").strip()
    lead_ref = data.get("lead_ref", "")  # links back to existing lead if QR was scanned

    if not name or not phone:
        return jsonify({"ok": False, "error": "name and phone required"}), 400

    submitted_at = datetime.now().isoformat()

    # Map opt_in boolean from Replit to a stored status string
    raw_opt = data.get("opt_in")
    if raw_opt is True:
        opt_status = "opted_in"
    elif raw_opt is False:
        opt_status = "opted_out"
    else:
        opt_status = "no_selection"

    if lead_ref:
        # Form came from a QR scan — update the existing lead and move straight to call queue
        try:
            lead_id = int(lead_ref)
            update_lead(lead_id,
                        follow_up_stage="call_ready",
                        form_submitted_date=submitted_at,
                        qr_scanned=1,
                        qr_scan_date=submitted_at,
                        outreach_status="call_ready",
                        opt_in_status=opt_status)
            label = {"opted_in": "opted IN ✅", "opted_out": "opted OUT ❌", "no_selection": "no selection"}.get(opt_status, "")
            add_notification(lead_id,
                             f"{name} filled out the preview site form ({label}) — call them now!",
                             "hot")
        except (ValueError, TypeError):
            pass
    else:
        # Organic form submission — create a new lead
        with get_db() as conn:
            conn.execute(
                """INSERT INTO leads
                   (name, phone, city, business_type, source,
                    follow_up_stage, form_submitted_date, outreach_status, opt_in_status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (business or name, phone, city, trade,
                 "landing_page", "call_ready", submitted_at, "call_ready", opt_status)
            )
            lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        add_notification(lead_id,
                         f"New inbound lead from preview site: {name} ({business})",
                         "hot")

    email = data.get("email", "").strip()
    _slack_notify_lead(name, phone, email, business or name)

    return jsonify({"ok": True})


# ─── Scraper ──────────────────────────────────────────────────────────────────

@app.route("/scraper")
def scraper_page():
    with get_db() as conn:
        jobs = conn.execute(
            "SELECT * FROM scrape_jobs ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    return render_template("scraper.html", jobs=[dict(j) for j in jobs])


@app.route("/scraper/start", methods=["POST"])
def scraper_start():
    cities_raw = request.form.get("cities", "")
    cities = [c.strip() for c in cities_raw.split(",") if c.strip()]
    business_type = request.form.get("business_type", "").strip()
    lead_count = int(request.form.get("lead_count", 20))

    if not cities or not business_type:
        return redirect(url_for("scraper_page"))

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO scrape_jobs (cities, business_type, lead_count) VALUES (?,?,?)",
            (json.dumps(cities), business_type, lead_count)
        )
        job_id = cur.lastrowid

    from scraper import run_scrape_job
    run_scrape_job(job_id, cities, business_type, lead_count)

    return redirect(url_for("scraper_page"))


@app.route("/scraper/status/<int:job_id>")
def scraper_status(job_id):
    with get_db() as conn:
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
    return jsonify(dict(job) if job else {})


# ─── Call Queue ───────────────────────────────────────────────────────────────

@app.route("/call-queue")
def call_queue():
    queue = get_call_queue()
    return render_template("call_queue.html", queue=queue)


@app.route("/call-queue/<int:lead_id>/mark-called", methods=["POST"])
def mark_called(lead_id):
    update_lead(lead_id, outreach_status="called", called_date=datetime.now().isoformat())
    return redirect(url_for("call_queue"))


# ─── Notifications API ────────────────────────────────────────────────────────

@app.route("/api/notifications")
def api_notifications():
    notifs = get_notifications(unread_only=True)
    return jsonify(notifs)


@app.route("/api/notifications/read", methods=["POST"])
def api_mark_read():
    mark_notifications_read()
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


# ─── QR Scan Simulator (dev) ──────────────────────────────────────────────────

@app.route("/scan")
def qr_scan():
    """QR code on postcard lands here — logs the scan then redirects to preview site."""
    lead_id = request.args.get("ref", "")
    if lead_id:
        try:
            lid = int(lead_id)
            lead = get_lead(lid)
            if lead:
                scan_date = datetime.now().isoformat()
                update_lead(lid, qr_scanned=1, qr_scan_date=scan_date,
                            follow_up_stage="scanned")
                add_notification(lid,
                                 f"{lead['name']} scanned the QR code — they're looking!",
                                 "hot")
                # Build params so the landing page can show a personalized card
                from urllib.parse import urlencode, quote
                params = {"ref": lead_id, "biz": lead.get("name", "")}
                if lead.get("preview_url"):
                    params["preview"] = lead["preview_url"]
                dest = "https://smartvibe.social/?" + urlencode(params)
                return redirect(dest)
        except (ValueError, TypeError):
            pass
    return redirect("https://smartvibe.social")


@app.route("/dev/simulate-scan/<int:lead_id>", methods=["POST"])
def simulate_scan(lead_id):
    lead = get_lead(lead_id)
    if lead:
        update_lead(lead_id, qr_scanned=1, qr_scan_date=datetime.now().isoformat())
        add_notification(lead_id,
                         f"QR code scanned! {lead['name']} viewed their preview — time to call!",
                         "hot")
    return jsonify({"ok": True})


# ─── Template filters ─────────────────────────────────────────────────────────

@app.template_filter("timeago")
def timeago(val):
    if not val:
        return ""
    try:
        dt = datetime.fromisoformat(val)
        diff = datetime.now() - dt
        days = diff.days
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if days < 30:
            return f"{days // 7}w ago"
        return f"{days // 30}mo ago"
    except Exception:
        return val


@app.template_filter("status_label")
def status_label(val):
    labels = {
        "new": "New",
        "postcard_sent": "Postcard Sent",
        "email_sent": "Email Sent",
        "called": "Called",
        "converted": "Converted",
    }
    return labels.get(val, val or "New")


@app.template_filter("priority_color")
def priority_color(val):
    return {"Hot": "red", "Warm": "amber", "Cold": "blue"}.get(val, "slate")


if __name__ == "__main__":
    import webbrowser, threading
    init_db()
    import_csv_if_needed()

    def open_browser():
        import time
        time.sleep(1)
        webbrowser.open("http://localhost:5001")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
