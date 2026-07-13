"""
AI preview generation using Claude API + QR code creation.
"""
import os
import re
import qrcode
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

STATIC_DIR = Path(__file__).parent / "static"
QR_DIR = STATIC_DIR / "qrcodes"
PREVIEW_DIR = STATIC_DIR / "previews"
QR_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

BASE_PREVIEW_URL = "http://smartvibe.social/preview"


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return s.strip("-")[:60]


def _fetch_photos(btype: str) -> dict:
    """Fetch real trade photos from Unsplash. Returns dict with hero, work, team photo URLs."""
    import requests as _req

    btype_lower = (btype or "").lower()
    if any(x in btype_lower for x in ["plumb", "excavat", "drain", "sewer"]):
        queries = ["plumber working pipe", "plumbing repair home", "plumber tools"]
    elif any(x in btype_lower for x in ["clean", "maid", "housekeep"]):
        queries = ["professional house cleaning", "cleaning service worker", "maid cleaning home"]
    elif any(x in btype_lower for x in ["pest", "exterminator", "termite"]):
        queries = ["pest control worker", "exterminator spraying", "pest control home"]
    elif any(x in btype_lower for x in ["hvac", "heat", "air", "cool"]):
        queries = ["hvac technician working", "air conditioning repair", "hvac worker"]
    elif any(x in btype_lower for x in ["electric"]):
        queries = ["electrician working", "electrical panel repair", "electrician home"]
    elif any(x in btype_lower for x in ["landscap", "lawn", "tree"]):
        queries = ["landscaper working yard", "lawn care professional", "landscaping crew"]
    else:
        queries = ["local contractor working", "home service professional", "contractor tools"]

    access_key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    photos = {}

    if access_key:
        try:
            for i, q in enumerate(queries[:3]):
                r = _req.get(
                    "https://api.unsplash.com/search/photos",
                    params={"query": q, "per_page": 1, "orientation": "landscape"},
                    headers={"Authorization": f"Client-ID {access_key}"},
                    timeout=6
                )
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    if results:
                        url = results[0]["urls"]["regular"]
                        key = ["hero", "work", "team"][i]
                        photos[key] = url
        except Exception:
            pass

    # Fallback: curated Unsplash source URLs (no key needed, random from collection)
    fallbacks = {
        "plumb": {
            "hero": "https://images.unsplash.com/photo-1607472586893-edb57bdc0e39?w=1200&q=80",
            "work": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=800&q=80",
            "team": "https://images.unsplash.com/photo-1504307651254-35680f356dfd?w=800&q=80",
        },
        "clean": {
            "hero": "https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=1200&q=80",
            "work": "https://images.unsplash.com/photo-1527515545081-5db817172677?w=800&q=80",
            "team": "https://images.unsplash.com/photo-1563453392212-326f5e854473?w=800&q=80",
        },
        "pest": {
            "hero": "https://images.unsplash.com/photo-1632399776843-6b745e4093e6?w=1200&q=80",
            "work": "https://images.unsplash.com/photo-1590845947376-2638caa89309?w=800&q=80",
            "team": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=800&q=80",
        },
        "hvac": {
            "hero": "https://images.unsplash.com/photo-1621905251189-08b45d6a269e?w=1200&q=80",
            "work": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=800&q=80",
            "team": "https://images.unsplash.com/photo-1504307651254-35680f356dfd?w=800&q=80",
        },
        "default": {
            "hero": "https://images.unsplash.com/photo-1504307651254-35680f356dfd?w=1200&q=80",
            "work": "https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=800&q=80",
            "team": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=800&q=80",
        },
    }

    category = "default"
    for key in ["plumb", "clean", "pest", "hvac"]:
        if key in btype_lower:
            category = key
            break

    for slot in ["hero", "work", "team"]:
        if slot not in photos:
            photos[slot] = fallbacks[category][slot]

    return photos


def _fetch_brand(domain: str) -> dict:
    """Pull logo URL + brand colors from Brandfetch. Returns {} on failure."""
    if not domain:
        return {}
    try:
        import requests as _req
        # Strip to bare domain
        domain = re.sub(r"https?://", "", domain).split("/")[0].lstrip("www.")
        api_key = os.environ.get("BRANDFETCH_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = _req.get(f"https://api.brandfetch.io/v2/brands/{domain}",
                     headers=headers, timeout=6)
        if r.status_code != 200:
            return {}
        data = r.json()
        result = {}
        # Logo — prefer SVG, fallback to PNG
        for logo in data.get("logos", []):
            for fmt in logo.get("formats", []):
                if fmt.get("format") in ("svg", "png") and fmt.get("src"):
                    result["logo_url"] = fmt["src"]
                    break
            if "logo_url" in result:
                break
        # Brand colors
        colors = []
        for palette in data.get("colors", []):
            hex_val = palette.get("hex")
            if hex_val:
                colors.append(hex_val)
        if colors:
            result["primary_color"] = colors[0]
            result["accent_color"] = colors[1] if len(colors) > 1 else colors[0]
        return result
    except Exception:
        return {}


def _business_theme(btype: str) -> dict:
    """Return color theme + icon set based on business type."""
    btype = (btype or "").lower()
    if any(x in btype for x in ["plumb", "excavat", "drain", "sewer"]):
        return {"primary": "#1e40af", "accent": "#3b82f6", "light": "#eff6ff",
                "emoji": "🔧", "cta": "Get a Free Estimate", "tagline": "Fast, Reliable, Local"}
    if any(x in btype for x in ["clean", "maid", "housekeep", "janitorial"]):
        return {"primary": "#065f46", "accent": "#10b981", "light": "#ecfdf5",
                "emoji": "✨", "cta": "Book a Cleaning", "tagline": "Spotless Every Time"}
    if any(x in btype for x in ["pest", "exterminator", "termite", "bug"]):
        return {"primary": "#7c2d12", "accent": "#ea580c", "light": "#fff7ed",
                "emoji": "🛡️", "cta": "Get a Free Inspection", "tagline": "Protect Your Home"}
    if any(x in btype for x in ["hvac", "heat", "air", "cool", "furnace"]):
        return {"primary": "#0c4a6e", "accent": "#0ea5e9", "light": "#f0f9ff",
                "emoji": "❄️", "cta": "Schedule Service", "tagline": "Comfort Year-Round"}
    if any(x in btype for x in ["electric", "wir"]):
        return {"primary": "#713f12", "accent": "#eab308", "light": "#fefce8",
                "emoji": "⚡", "cta": "Request a Quote", "tagline": "Safe, Certified, Local"}
    if any(x in btype for x in ["landscap", "lawn", "tree", "garden"]):
        return {"primary": "#14532d", "accent": "#22c55e", "light": "#f0fdf4",
                "emoji": "🌿", "cta": "Get a Free Quote", "tagline": "Your Yard, Our Passion"}
    # Default
    return {"primary": "#1e293b", "accent": "#6366f1", "light": "#f8fafc",
            "emoji": "⭐", "cta": "Contact Us Today", "tagline": "Professional & Reliable"}


def _ai_content(lead: dict, theme: dict, brand: dict) -> dict:
    """Use GPT-4o to write sharp copy for the template. Falls back to Claude."""
    name = lead.get("name", "")
    btype = lead.get("business_type") or "local business"
    city = lead.get("city") or "Colorado Springs"
    phone = lead.get("phone") or ""

    default = {
        "headline": f"Colorado Springs' Trusted {btype.title()} Experts",
        "subheadline": f"{name} — serving {city} and surrounding areas.",
        "about": f"{name} is a local {btype} company serving {city}, CO. We pride ourselves on honest pricing, fast response times, and quality work that lasts.",
        "service1": "Residential Service", "service1_desc": "Expert solutions for your home.",
        "service2": "Commercial Service", "service2_desc": "Reliable service for businesses.",
        "service3": "Emergency Service", "service3_desc": "Available when you need us most.",
        "why1": "Licensed & Insured", "why2": "Free Estimates", "why3": "Same-Day Service",
        "review": f"Best {btype} in {city}! Fast, honest, and fairly priced. Will definitely call again.",
        "review_author": "Mike D. — Briargate",
    }

    prompt = f"""Write punchy, specific website copy for a local {btype} business. Sound like a real local company, not a template. No corporate fluff.

Business: {name}
City: {city}, CO
Phone: {phone or 'call for pricing'}

Return a JSON object with EXACTLY these keys — no extra text:
{{
  "headline": "Hero headline (6-8 words max, punchy, mention city or trade)",
  "subheadline": "One sentence — what they do and where. Confident tone.",
  "about": "2-3 sentences. Local pride, years of experience implied, honest pricing. Sound human.",
  "service1": "Core service name", "service1_desc": "One specific sentence about this service.",
  "service2": "Second service name", "service2_desc": "One specific sentence.",
  "service3": "Third service name (e.g. Emergency/24-hr)", "service3_desc": "One specific sentence.",
  "why1": "2-3 word trust badge", "why2": "2-3 word trust badge", "why3": "2-3 word trust badge",
  "review": "Realistic 5-star review from a homeowner. Mention the specific trade. 1-2 sentences.",
  "review_author": "First name + last initial + Colorado Springs neighborhood, e.g. 'Sarah T. — Old Colorado City'"
}}"""

    # Try GPT-4o first
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        try:
            import requests as _req
            r = _req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 800, "response_format": {"type": "json_object"}},
                timeout=20
            )
            if r.status_code == 200:
                import json as _json
                text = r.json()["choices"][0]["message"]["content"]
                return {**default, **_json.loads(text)}
        except Exception:
            pass

    # Fallback to Claude
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            import anthropic, json as _json
            client = anthropic.Anthropic(api_key=anthropic_key)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            text = msg.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"): text = text[4:]
            return {**default, **_json.loads(text)}
        except Exception:
            pass

    return default


def _build_html(lead: dict, content: dict, theme: dict, brand: dict, photos: dict) -> str:
    """Assemble the final HTML from template + content + brand."""
    name = lead.get("name", "")
    phone = lead.get("phone") or "Call for a free quote"
    address = lead.get("address") or lead.get("city") or "Colorado Springs, CO"
    city = lead.get("city") or "Colorado Springs"
    btype = lead.get("business_type") or "local business"

    primary = brand.get("primary_color") or theme["primary"]
    accent = brand.get("accent_color") or theme["accent"]
    light = theme["light"]
    logo_html = ""
    if brand.get("logo_url"):
        logo_html = f'<img src="{brand["logo_url"]}" alt="{name} logo" style="height:48px;object-fit:contain;max-width:200px;">'
    else:
        logo_html = f'<span style="font-size:1.4rem;font-weight:800;color:white;letter-spacing:-0.5px;">{name}</span>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{name} | {city}, CO</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/daisyui@4.6.0/dist/full.min.css">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root {{ --primary: {primary}; --accent: {accent}; }}
  .btn-primary-custom {{ background:{primary};color:#fff;border:none; }}
  .btn-primary-custom:hover {{ background:{accent}; }}
  .text-primary-custom {{ color:{primary}; }}
  .bg-primary-custom {{ background:{primary}; }}
  .bg-light-custom {{ background:{light}; }}
  .border-primary-custom {{ border-color:{primary}; }}
  html {{ scroll-behavior:smooth; }}
</style>
</head>
<body class="font-sans antialiased text-gray-800">

<!-- NAV -->
<nav class="bg-primary-custom shadow-lg sticky top-0 z-50">
  <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
    {logo_html}
    <a href="#contact" class="btn btn-sm btn-primary-custom rounded-full px-5 font-semibold shadow">
      {theme["cta"]}
    </a>
  </div>
</nav>

<!-- HERO -->
<section class="relative text-white min-h-[520px] flex items-center overflow-hidden">
  <div class="absolute inset-0">
    <img src="{photos["hero"]}" alt="{btype} professional" class="w-full h-full object-cover">
    <div class="absolute inset-0" style="background:linear-gradient(135deg,{primary}ee 0%,{primary}99 50%,{primary}55 100%)"></div>
  </div>
  <div class="relative max-w-5xl mx-auto px-6 py-20 flex flex-col md:flex-row items-center gap-10">
    <div class="flex-1 text-center md:text-left">
      <div class="inline-block text-sm font-bold uppercase tracking-widest mb-4 px-3 py-1 rounded-full" style="background:rgba(255,255,255,0.15)">
        {city}, Colorado
      </div>
      <h1 class="text-4xl md:text-5xl font-black mb-4 leading-tight drop-shadow-lg">{content["headline"]}</h1>
      <p class="text-lg md:text-xl opacity-90 mb-8 max-w-lg">{content["subheadline"]}</p>
      <div class="flex flex-col sm:flex-row gap-3 justify-center md:justify-start">
        <a href="tel:{phone}" class="btn btn-lg rounded-full bg-white font-bold shadow-xl" style="color:{primary};">
          📞 {phone}
        </a>
        <a href="#contact" class="btn btn-lg rounded-full border-2 border-white font-bold" style="background:transparent;">
          {theme["cta"]} →
        </a>
      </div>
    </div>
    <div class="hidden md:block flex-shrink-0 w-64 h-64 rounded-2xl overflow-hidden shadow-2xl border-4 border-white/30">
      <img src="{photos["work"]}" alt="Our work" class="w-full h-full object-cover">
    </div>
  </div>
</section>

<!-- TRUST BAR -->
<div class="bg-gray-900 text-white py-4">
  <div class="max-w-4xl mx-auto px-4 flex flex-wrap justify-center gap-8 text-sm font-semibold">
    <span>✅ {content["why1"]}</span>
    <span>✅ {content["why2"]}</span>
    <span>✅ {content["why3"]}</span>
    <span>✅ Locally Owned</span>
  </div>
</div>

<!-- SERVICES -->
<section id="services" class="py-16 px-4 bg-light-custom">
  <div class="max-w-5xl mx-auto">
    <h2 class="text-3xl font-black text-center mb-2 text-primary-custom">Our Services</h2>
    <p class="text-center text-gray-500 mb-10">Everything you need, handled by local pros</p>
    <div class="grid md:grid-cols-3 gap-6">
      {_service_card(content["service1"], content["service1_desc"], primary, accent)}
      {_service_card(content["service2"], content["service2_desc"], primary, accent)}
      {_service_card(content["service3"], content["service3_desc"], primary, accent)}
    </div>
  </div>
</section>

<!-- ABOUT -->
<section id="about" class="py-16 px-4 bg-white">
  <div class="max-w-5xl mx-auto flex flex-col md:flex-row gap-10 items-center">
    <div class="flex-shrink-0 w-full md:w-80 h-64 rounded-2xl overflow-hidden shadow-xl">
      <img src="{photos["team"]}" alt="Our team" class="w-full h-full object-cover">
    </div>
    <div class="flex-1">
      <h2 class="text-3xl font-black mb-4 text-primary-custom">About {name}</h2>
      <p class="text-gray-600 leading-relaxed text-lg">{content["about"]}</p>
      <div class="mt-6 flex flex-wrap gap-3">
        <span class="px-3 py-1.5 rounded-full text-sm font-semibold text-white" style="background:{primary};">{content["why1"]}</span>
        <span class="px-3 py-1.5 rounded-full text-sm font-semibold text-white" style="background:{primary};">{content["why2"]}</span>
        <span class="px-3 py-1.5 rounded-full text-sm font-semibold text-white" style="background:{primary};">{content["why3"]}</span>
      </div>
      <p class="mt-4 text-gray-400 text-sm">📍 {address}</p>
    </div>
  </div>
</section>

<!-- REVIEW -->
<section class="py-12 px-4 bg-primary-custom text-white">
  <div class="max-w-2xl mx-auto text-center">
    <div class="text-yellow-400 text-2xl mb-3">★★★★★</div>
    <blockquote class="text-xl italic opacity-95 mb-4">"{content["review"]}"</blockquote>
    <p class="opacity-75 font-semibold">— {content["review_author"]}</p>
  </div>
</section>

<!-- CONTACT -->
<section id="contact" class="py-16 px-4 bg-light-custom">
  <div class="max-w-lg mx-auto text-center">
    <h2 class="text-3xl font-black mb-2 text-primary-custom">{theme["cta"]}</h2>
    <p class="text-gray-500 mb-8">We'll get back to you within 1 business hour.</p>
    <form class="space-y-3" onsubmit="handleForm(event)">
      <input type="text" placeholder="Your Name" required
        class="input input-bordered w-full border-2 focus:outline-none" style="border-color:{primary}20;focus:border-color:{primary};">
      <input type="tel" placeholder="Phone Number" required
        class="input input-bordered w-full border-2" style="border-color:{primary}20;">
      <textarea placeholder="Tell us what you need..." rows="3"
        class="textarea textarea-bordered w-full border-2 text-base" style="border-color:{primary}20;"></textarea>
      <button type="submit" class="btn btn-lg w-full btn-primary-custom rounded-xl font-bold text-base shadow-lg">
        Send Request {theme["emoji"]}
      </button>
    </form>
    <div class="mt-8 pt-6 border-t border-gray-200">
      <a href="tel:{phone}" class="text-2xl font-black text-primary-custom">📞 {phone}</a>
      <p class="text-gray-400 text-sm mt-1">Call or text anytime</p>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer class="bg-gray-900 text-white py-8 text-center text-sm">
  <p class="font-semibold text-lg mb-1">{name}</p>
  <p class="opacity-60">{address}</p>
  <p class="opacity-60 mt-1">{phone}</p>
  <p class="opacity-30 mt-4 text-xs">Website preview powered by SmartVibe Social</p>
</footer>

<div id="toast" class="hidden fixed bottom-6 right-6 bg-green-500 text-white px-6 py-3 rounded-xl shadow-xl font-semibold z-50">
  ✅ Request sent! We'll be in touch soon.
</div>

<script>
function handleForm(e) {{
  e.preventDefault();
  document.getElementById('toast').classList.remove('hidden');
  e.target.reset();
  setTimeout(() => document.getElementById('toast').classList.add('hidden'), 4000);
}}
</script>
</body>
</html>"""


def _service_card(title, desc, primary, accent):
    return f"""<div class="card bg-white shadow-lg border border-gray-100 p-6 rounded-2xl hover:shadow-xl transition-shadow">
        <div class="w-12 h-12 rounded-xl mb-4 flex items-center justify-center text-white font-bold text-lg" style="background:{primary};">✓</div>
        <h3 class="font-bold text-lg mb-2" style="color:{primary};">{title}</h3>
        <p class="text-gray-500 text-sm leading-relaxed">{desc}</p>
      </div>"""


def generate_preview_site(lead: dict) -> tuple[str, str]:
    """Generate a professional HTML preview site. Returns (preview_url, html)."""
    btype = lead.get("business_type") or "local business"
    theme = _business_theme(btype)
    brand = _fetch_brand(lead.get("website") or "")
    photos = _fetch_photos(btype)
    content = _ai_content(lead, theme, brand)
    html = _build_html(lead, content, theme, brand, photos)

    s = f"{slug(lead.get('name', 'business'))}-{lead.get('id', 0)}"
    preview_url = f"{BASE_PREVIEW_URL}/{s}"
    html_file = PREVIEW_DIR / f"{s}.html"
    html_file.write_text(html, encoding="utf-8")
    return preview_url, html


def _fallback_preview(name, address, btype, city):
    colors = {
        "plumber": ("#1e40af", "#dbeafe"),
        "house cleaning": ("#065f46", "#d1fae5"),
        "electrician": ("#92400e", "#fef3c7"),
        "landscaping": ("#14532d", "#dcfce7"),
        "roofing": ("#7f1d1d", "#fee2e2"),
    }
    accent, bg = colors.get(btype.lower(), ("#4f46e5", "#ede9fe"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111}}
.hero{{background:{accent};color:#fff;padding:80px 20px;text-align:center}}
.hero h1{{font-size:2.5rem;font-weight:800;margin-bottom:12px}}
.hero p{{font-size:1.2rem;opacity:.9;margin-bottom:32px}}
.cta{{display:inline-block;background:#fff;color:{accent};padding:14px 32px;border-radius:8px;font-weight:700;text-decoration:none;font-size:1rem}}
.services{{padding:60px 20px;max-width:900px;margin:auto}}
.services h2{{font-size:1.8rem;font-weight:700;margin-bottom:32px;text-align:center}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px}}
.card{{background:{bg};padding:24px;border-radius:12px;text-align:center}}
.card h3{{font-weight:700;margin-bottom:8px;color:{accent}}}
.contact{{background:#f9fafb;padding:60px 20px;text-align:center}}
.contact h2{{font-size:1.8rem;font-weight:700;margin-bottom:16px}}
.contact p{{color:#6b7280;margin-bottom:8px}}
footer{{background:#111;color:#9ca3af;text-align:center;padding:20px;font-size:.85rem}}
</style>
</head>
<body>
<div class="hero">
  <h1>{name}</h1>
  <p>Professional {btype.title()} Services in {city}</p>
  <a href="#contact" class="cta">Get a Free Quote</a>
</div>
<div class="services">
  <h2>Our Services</h2>
  <div class="grid">
    <div class="card"><h3>Quality Work</h3><p>Licensed & insured professionals</p></div>
    <div class="card"><h3>Fast Response</h3><p>Same-day service available</p></div>
    <div class="card"><h3>Fair Pricing</h3><p>Transparent, upfront quotes</p></div>
    <div class="card"><h3>Local Experts</h3><p>Serving {city} and surrounding areas</p></div>
  </div>
</div>
<div class="contact" id="contact">
  <h2>Contact Us Today</h2>
  <p>{address}</p>
  <p>Call us for a free estimate</p>
  <a href="#" class="cta" style="background:{accent};color:#fff;margin-top:20px;display:inline-block">Call Now</a>
</div>
<footer>Powered by SmartVibe &bull; Professional Website Services</footer>
</body>
</html>"""


def generate_qr_code(lead: dict, preview_url: str) -> str:
    """Generate QR code PNG and return its static path."""
    s = slug(lead.get("name", str(lead.get("id", "lead"))))
    qr_path = QR_DIR / f"{s}.png"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(preview_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(str(qr_path))

    return f"qrcodes/{s}.png"


def generate_cold_email(lead: dict) -> str:
    """Generate a personalized cold email for a lead."""
    name = lead.get("name", "")
    owner = lead.get("owner_name") or "there"
    city = lead.get("city") or ""
    btype = lead.get("business_type") or "business"
    preview_url = lead.get("preview_url") or ""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _fallback_email(name, owner, city, btype, preview_url)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"""Write a short, personalized cold email for a local business owner. Keep it under 150 words, conversational, and focused on their lack of online presence.

Business: {name}
Owner: {owner}
City: {city}
Type: {btype}
Preview site URL: {preview_url}

The email should:
- Open with their name
- Mention we noticed their business doesn't have a strong online presence
- Mention we already built them a free preview website (link: {preview_url})
- Include one line about how a better web presence gets more calls
- End with a soft CTA to check the preview and reply if interested
- Sign off as "The SmartVibe Team"
- Subject line on first line as: Subject: ...

Return only the email text, nothing else."""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception:
        return _fallback_email(name, owner, city, btype, preview_url)


def generate_recommendations(lead: dict) -> list:
    """Generate 3-5 AI service recommendations tailored to this lead's gaps."""
    import json
    name = lead.get("name", "")
    btype = lead.get("business_type") or "local business"
    city = lead.get("city") or ""
    website = lead.get("website") or ""
    website_grade = lead.get("website_grade") or ""
    website_live = lead.get("website_live")
    fb_url = lead.get("facebook_url") or ""
    fb_freq = lead.get("facebook_post_frequency") or ""
    ig_url = lead.get("instagram_url") or ""
    tiktok_url = lead.get("tiktok_url") or ""
    score = lead.get("quality_score", 0)

    # Build context summary of their digital gaps
    gaps = []
    if not website:
        gaps.append("no website at all")
    elif not website_live:
        gaps.append("website is broken/offline")
    elif website_grade in ("D", "F"):
        gaps.append(f"website exists but is low quality (grade {website_grade})")
    elif website_grade in ("B", "C"):
        gaps.append(f"website exists but could be improved (grade {website_grade})")
    if not fb_url:
        gaps.append("no Facebook page")
    elif fb_freq in ("Sporadic", "Dead", "Monthly", ""):
        gaps.append(f"Facebook page exists but posting is {fb_freq or 'infrequent'}")
    if not ig_url:
        gaps.append("no Instagram")
    if not tiktok_url:
        gaps.append("no TikTok")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _fallback_recommendations(gaps, btype)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"""You are a sharp AI services sales consultant who sells to local small businesses. Your job is to identify the highest-value services to pitch to a specific business — both fixing their digital gaps AND solving operational bottlenecks with AI tools and automation.

Business: {name}
Type: {btype}
City: {city}
Digital gaps detected: {', '.join(gaps) if gaps else 'fairly established online'}
Lead score: {score}/100 (higher score = weaker online presence = more opportunity)

Think carefully about this specific type of business:
- What does their typical day look like? Where do they lose money or time?
- What calls do they miss? What leads fall through the cracks?
- What's manual that AI could automate for them?
- What do their customers complain about that AI could fix?

Generate 5–6 service recommendations. Mix BOTH types:
1. Digital presence fixes based on their gaps (website, social media, Google profile, etc.)
2. AI tools & automation that solve operational pain points SPECIFIC to their business type (e.g. for a plumber: missed-call AI bot that answers and books jobs 24/7; for a cleaning company: automated review requests after each job; for a pest control company: AI chatbot that qualifies leads and gives instant quotes)

Be creative and specific — don't just list generic "chatbot" or "AI assistant." Name the exact pain point it solves.

Return a JSON array. Each item:
{{
  "service": "Short punchy name (2–5 words)",
  "category": "digital" | "ai_tool" | "automation",
  "why": "One sentence — the specific pain point this solves for THIS type of business",
  "value": "One sentence — the concrete outcome (saved hours, more bookings, fewer missed calls, etc.)",
  "price_range": "Realistic monthly or one-time range, e.g. '$149/mo' or '$500–1,200 one-time'",
  "priority": "high" | "medium" | "low"
}}

Order: high priority first. Return ONLY the JSON array, nothing else."""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception:
        return _fallback_recommendations(gaps, btype)


def _fallback_recommendations(gaps, btype):
    recs = []
    if any("website" in g for g in gaps):
        recs.append({"service": "Website Build", "why": f"No professional web presence for this {btype}.",
                     "value": "More calls from Google searches in their area.", "price_range": "$800–2,000 one-time", "priority": "high"})
    if any("Facebook" in g for g in gaps):
        recs.append({"service": "Social Media Management", "why": "Missing or inactive Facebook presence.",
                     "value": "Regular posts keep them top-of-mind with local customers.", "price_range": "$299–499/mo", "priority": "high"})
    if any("Instagram" in g for g in gaps):
        recs.append({"service": "Instagram Setup & Content", "why": "No Instagram presence.",
                     "value": "Reach younger customers searching for local services.", "price_range": "$199–399/mo", "priority": "medium"})
    recs.append({"service": "Google Business Profile", "why": "Most local businesses have unclaimed or incomplete profiles.",
                 "value": "Show up in Google Maps searches immediately.", "price_range": "$149 one-time", "priority": "medium"})
    return recs


def _fallback_email(name, owner, city, btype, preview_url):
    return f"""Subject: We built {name} a free website — take a look

Hi {owner},

I was looking for {btype} services in {city} and had trouble finding {name} online.

We went ahead and built you a free preview website — check it out here:
{preview_url}

A strong web presence means more calls, more jobs, and more revenue. Businesses with websites in your area are booking 40% more work than those without.

Would you like us to publish this site and help more customers find you? Just reply to this email and we'll get you set up.

Best,
The SmartVibe Team"""
