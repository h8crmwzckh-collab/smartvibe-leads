"""
Multi-source scraper: Google Maps + Yelp.
Runs in a background thread and updates the scrape_jobs table.
"""
import re
import threading
import time
from datetime import datetime
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


def run_scrape_job(job_id: int, cities: list[str], business_type: str, lead_count: int):
    thread = threading.Thread(
        target=_scrape_worker,
        args=(job_id, cities, business_type, lead_count),
        daemon=True,
    )
    thread.start()


def _update_job(job_id, **kwargs):
    from database import get_db
    with get_db() as conn:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE scrape_jobs SET {sets} WHERE id=?", list(kwargs.values()) + [job_id])


# ─── Main worker ──────────────────────────────────────────────────────────────

def _scrape_worker(job_id, cities, business_type, lead_count):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    _update_job(job_id, status="running")
    found = 0
    per_city = max(1, lead_count // len(cities)) if cities else lead_count

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            for city in cities:
                city_found = 0

                # ── Google Maps ──────────────────────────────────────
                gmaps_target = max(1, per_city * 2 // 3)  # ~2/3 from Google
                gmaps_leads = _scrape_google_maps(ctx, city, business_type, gmaps_target, job_id)
                for lead in gmaps_leads:
                    if city_found >= per_city:
                        break
                    if _save_lead(lead, job_id):
                        found += 1
                        city_found += 1
                        _update_job(job_id, leads_found=found)

                # ── Yelp ─────────────────────────────────────────────
                yelp_target = per_city - city_found
                if yelp_target > 0:
                    yelp_leads = _scrape_yelp(ctx, city, business_type, yelp_target * 2, job_id)
                    for lead in yelp_leads:
                        if city_found >= per_city:
                            break
                        if _save_lead(lead, job_id):
                            found += 1
                            city_found += 1
                            _update_job(job_id, leads_found=found)

            browser.close()

    except Exception as e:
        print(f"[scraper] worker error: {e}")
        _update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    _update_job(job_id, status="done", leads_found=found, finished_at=datetime.now().isoformat())


# ─── Google Maps ──────────────────────────────────────────────────────────────

def _scrape_google_maps(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        query = f"{business_type} in {city}"
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}/"
        page.goto(url, timeout=30000)
        page.wait_for_timeout(3000)

        seen_names = set()
        attempts = 0

        while len(leads) < target and attempts < 80:
            attempts += 1
            results = page.query_selector_all('[role="feed"] > div')

            for item in results:
                if len(leads) >= target:
                    break
                try:
                    name_el = item.query_selector("a[aria-label]")
                    if not name_el:
                        continue
                    name = (name_el.get_attribute("aria-label") or "").strip()
                    if not name or len(name) < 3 or name in seen_names:
                        continue
                    seen_names.add(name)

                    name_el.click()
                    # Wait for detail panel to fully load
                    try:
                        page.wait_for_selector('[data-item-id="address"], [aria-label="Address"]', timeout=5000)
                    except Exception:
                        page.wait_for_timeout(2500)

                    lead = _extract_gmaps_lead(page, name, city, business_type)
                    if lead:
                        leads.append(lead)

                    page.go_back()
                    page.wait_for_timeout(1500)
                except Exception:
                    continue

            # Scroll to load more results
            try:
                panel = page.query_selector('[role="feed"]')
                if panel:
                    panel.evaluate("el => el.scrollBy(0, 1200)")
                page.wait_for_timeout(1500)
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] Google Maps error for {city}: {e}")
    finally:
        page.close()

    return leads


def _extract_gmaps_lead(page, name, city, business_type):
    try:
        data = {"name": name, "city": city, "business_type": business_type, "source": "google_maps"}

        # Address — try multiple selectors
        address = None
        for selector in [
            '[data-item-id="address"]',
            'button[data-item-id="address"]',
            '[aria-label="Address"]',
            'button[aria-label*="Address"]',
        ]:
            try:
                el = page.query_selector(selector)
                if el:
                    txt = el.inner_text().strip()
                    if txt and len(txt) > 5:
                        address = txt
                        break
            except Exception:
                continue

        # Fallback: look for any text that looks like a street address
        if not address:
            try:
                all_text = page.inner_text('[role="main"]')
                match = re.search(r'\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct|Pl|Circle|Pkwy)[\.,]?', all_text)
                if match:
                    address = match.group(0).strip()
            except Exception:
                pass

        if address:
            data["address"] = address

        # Phone
        for selector in ['[data-item-id^="phone"]', 'button[data-item-id^="phone"]', '[aria-label*="phone" i]']:
            try:
                el = page.query_selector(selector)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        data["phone"] = txt
                        break
            except Exception:
                continue

        # Website
        for selector in ['[data-item-id="authority"]', 'a[data-item-id="authority"]']:
            try:
                el = page.query_selector(selector)
                if el:
                    data["website"] = el.get_attribute("href") or el.inner_text().strip()
                    break
            except Exception:
                continue

        # Owner name from "Owned by" or "Claimed by" text
        try:
            main_text = page.inner_text('[role="main"]')
            owner_match = re.search(r'(?:Owner|Manager)[\s:·]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', main_text)
            if owner_match:
                data["owner_name"] = owner_match.group(1).strip()
        except Exception:
            pass

        social = _check_social(name, city, page.context)
        data.update(social)

        score = _score(data)
        data["quality_score"] = score
        data["priority"] = _priority_label(score)

        return data
    except Exception as e:
        print(f"[scraper] extract error for {name}: {e}")
        return None


# ─── Yelp ─────────────────────────────────────────────────────────────────────

def _scrape_yelp(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        slug_city = city.replace(" ", "-").replace(",", "")
        slug_type = business_type.replace(" ", "-")
        url = f"https://www.yelp.com/search?find_desc={slug_type}&find_loc={slug_city}"
        page.goto(url, timeout=30000)
        page.wait_for_timeout(3000)

        seen_names = set()
        page_num = 0

        while len(leads) < target and page_num < 5:
            page_num += 1

            # Collect listing links from search results
            links = page.query_selector_all('a[href*="/biz/"]')
            biz_urls = []
            for link in links:
                href = link.get_attribute("href") or ""
                if "/biz/" in href and "?" not in href.split("/biz/")[-1]:
                    full = href if href.startswith("http") else f"https://www.yelp.com{href}"
                    if full not in biz_urls:
                        biz_urls.append(full)

            for biz_url in biz_urls:
                if len(leads) >= target:
                    break
                try:
                    detail = ctx.new_page()
                    detail.goto(biz_url, timeout=20000)
                    detail.wait_for_timeout(2000)

                    lead = _extract_yelp_lead(detail, city, business_type)
                    detail.close()

                    if lead and lead.get("name") not in seen_names:
                        seen_names.add(lead["name"])
                        leads.append(lead)
                except Exception:
                    try:
                        detail.close()
                    except Exception:
                        pass
                    continue

            # Next page
            try:
                next_btn = page.query_selector('a[aria-label="Next"]')
                if next_btn:
                    next_btn.click()
                    page.wait_for_timeout(2500)
                else:
                    break
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] Yelp error for {city}: {e}")
    finally:
        try:
            page.close()
        except Exception:
            pass

    return leads


def _extract_yelp_lead(page, city, business_type):
    try:
        data = {"city": city, "business_type": business_type, "source": "yelp"}

        # Business name
        try:
            h1 = page.query_selector("h1")
            if h1:
                data["name"] = h1.inner_text().strip()
        except Exception:
            pass

        if not data.get("name"):
            return None

        # Address
        try:
            addr_el = page.query_selector('address, [class*="address" i]')
            if addr_el:
                txt = addr_el.inner_text().strip().replace("\n", ", ")
                if txt:
                    data["address"] = txt
        except Exception:
            pass

        # Phone
        try:
            phone_el = page.query_selector('p[class*="phone" i], a[href^="tel:"]')
            if phone_el:
                href = phone_el.get_attribute("href") or ""
                data["phone"] = href.replace("tel:", "") if href.startswith("tel:") else phone_el.inner_text().strip()
        except Exception:
            pass

        # Website
        try:
            web_el = page.query_selector('a[href*="biz_redir"]')
            if web_el:
                data["website"] = web_el.get_attribute("href") or ""
        except Exception:
            pass

        # Owner name
        try:
            page_text = page.inner_text("body")
            owner_match = re.search(r'(?:Meet the (?:Owner|Manager)|From the (?:owner|business))[\s:·"]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', page_text)
            if owner_match:
                data["owner_name"] = owner_match.group(1).strip()
        except Exception:
            pass

        social = _check_social(data["name"], city, page.context)
        data.update(social)

        score = _score(data)
        data["quality_score"] = score
        data["priority"] = _priority_label(score)

        return data
    except Exception as e:
        print(f"[scraper] Yelp extract error: {e}")
        return None


# ─── Social media check ───────────────────────────────────────────────────────

def _check_social(business_name, city, ctx=None):
    result = {
        "facebook_url": None, "facebook_active": False, "facebook_followers": None,
        "instagram_url": None, "instagram_active": False, "instagram_followers": None,
    }
    try:
        from playwright.sync_api import sync_playwright

        def _do_check(page):
            safe = re.sub(r"[^a-zA-Z0-9 ]", "", business_name)

            page.goto(
                f"https://www.google.com/search?q={safe.replace(' ', '+')}+{city.replace(' ', '+')}+site:facebook.com",
                timeout=15000,
            )
            page.wait_for_timeout(1200)
            for link in page.query_selector_all("a[href*='facebook.com']"):
                href = link.get_attribute("href") or ""
                if "facebook.com/" in href and "/l.facebook" not in href and "facebook.com/search" not in href:
                    result["facebook_url"] = href
                    break

            page.goto(
                f"https://www.google.com/search?q={safe.replace(' ', '+')}+{city.replace(' ', '+')}+site:instagram.com",
                timeout=15000,
            )
            page.wait_for_timeout(1200)
            for link in page.query_selector_all("a[href*='instagram.com']"):
                href = link.get_attribute("href") or ""
                if "instagram.com/" in href and "instagram.com/p/" not in href and "instagram.com/explore" not in href:
                    result["instagram_url"] = href
                    break

        if ctx:
            p = ctx.new_page()
            try:
                _do_check(p)
            finally:
                p.close()
        else:
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                p = b.new_context().new_page()
                _do_check(p)
                b.close()

    except Exception:
        pass
    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _score(data):
    from database import _compute_score
    return _compute_score(
        website=data.get("website"),
        fb_url=data.get("facebook_url"),
        fb_active=data.get("facebook_active", False),
        ig_url=data.get("instagram_url"),
        ig_active=data.get("instagram_active", False),
    )


def _priority_label(score):
    from database import _priority_label as db_label
    return db_label(score)


def _save_lead(data, job_id):
    """Save lead if not duplicate. Returns True if inserted."""
    from database import get_db
    if not data.get("name"):
        return False
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM leads WHERE LOWER(name)=LOWER(?) AND LOWER(city)=LOWER(?)",
            (data.get("name"), data.get("city")),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO leads (name, owner_name, address, city, phone, email, website,
                business_type, priority, quality_score,
                facebook_url, facebook_active, facebook_followers, facebook_last_post,
                instagram_url, instagram_active, instagram_followers, instagram_last_post,
                source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("name"), data.get("owner_name"), data.get("address"),
                data.get("city"), data.get("phone"), data.get("email"),
                data.get("website"), data.get("business_type"),
                data.get("priority", "Cold"), data.get("quality_score", 0),
                data.get("facebook_url"), int(data.get("facebook_active", False)),
                data.get("facebook_followers"), data.get("facebook_last_post"),
                data.get("instagram_url"), int(data.get("instagram_active", False)),
                data.get("instagram_followers"), data.get("instagram_last_post"),
                data.get("source", "google_maps"),
            ),
        )
    return True
