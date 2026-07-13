"""
Google Maps scraper + social media checker using Playwright.
Runs in a background thread and updates the scrape_jobs table.
"""
import json
import re
import sqlite3
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


def _scrape_worker(job_id, cities, business_type, lead_count):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    _update_job(job_id, status="running")
    found = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"
            )
            page = ctx.new_page()

            per_city = max(1, lead_count // len(cities)) if cities else lead_count

            for city in cities:
                query = f"{business_type} in {city}"
                url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}/"
                page.goto(url, timeout=30000)
                page.wait_for_timeout(3000)

                scraped = 0
                attempts = 0

                while scraped < per_city and attempts < 60:
                    attempts += 1
                    results = page.query_selector_all('[role="feed"] > div')
                    for item in results:
                        if scraped >= per_city:
                            break
                        try:
                            name_el = item.query_selector("a[aria-label]")
                            if not name_el:
                                continue
                            name = name_el.get_attribute("aria-label") or ""
                            if not name or len(name) < 3:
                                continue

                            name_el.click()
                            page.wait_for_timeout(2000)

                            lead = _extract_lead(page, name, city, business_type)
                            if lead:
                                _save_lead(lead, job_id)
                                found += 1
                                scraped += 1
                                _update_job(job_id, leads_found=found)

                            page.go_back()
                            page.wait_for_timeout(1500)
                        except Exception:
                            continue

                    try:
                        panel = page.query_selector('[role="feed"]')
                        if panel:
                            panel.evaluate("el => el.scrollBy(0, 800)")
                        page.wait_for_timeout(1500)
                    except Exception:
                        break

            browser.close()

    except Exception as e:
        _update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    _update_job(job_id, status="done", leads_found=found, finished_at=datetime.now().isoformat())


def _extract_lead(page, name, city, business_type):
    try:
        data = {"name": name, "city": city, "business_type": business_type, "source": "google_maps"}

        try:
            addr_el = page.query_selector('[data-item-id="address"]')
            if addr_el:
                data["address"] = addr_el.inner_text().strip()
        except Exception:
            pass

        try:
            phone_el = page.query_selector('[data-item-id^="phone"]')
            if phone_el:
                data["phone"] = phone_el.inner_text().strip()
        except Exception:
            pass

        try:
            web_el = page.query_selector('[data-item-id="authority"]')
            if web_el:
                data["website"] = web_el.get_attribute("href") or web_el.inner_text().strip()
        except Exception:
            pass

        social = _check_social(name, city)
        data.update(social)

        score = _compute_score(
            website=data.get("website"),
            fb_url=data.get("facebook_url"),
            fb_active=data.get("facebook_active", False),
            ig_url=data.get("instagram_url"),
            ig_active=data.get("instagram_active", False),
        )
        data["quality_score"] = score
        data["priority"] = _priority_label(score)

        return data
    except Exception:
        return None


def _check_social(business_name, city):
    """Heuristic social media check via search."""
    result = {
        "facebook_url": None, "facebook_active": False, "facebook_followers": None,
        "instagram_url": None, "instagram_active": False, "instagram_followers": None,
    }
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context().new_page()

            safe = re.sub(r"[^a-zA-Z0-9 ]", "", business_name)
            query = f"{safe} {city} site:facebook.com"
            page.goto(f"https://www.google.com/search?q={query.replace(' ', '+')}", timeout=15000)
            page.wait_for_timeout(1500)
            links = page.query_selector_all("a[href*='facebook.com']")
            for link in links:
                href = link.get_attribute("href") or ""
                if "facebook.com/" in href and "/l.facebook" not in href:
                    result["facebook_url"] = href
                    break

            query2 = f"{safe} {city} site:instagram.com"
            page.goto(f"https://www.google.com/search?q={query2.replace(' ', '+')}", timeout=15000)
            page.wait_for_timeout(1500)
            links2 = page.query_selector_all("a[href*='instagram.com']")
            for link in links2:
                href = link.get_attribute("href") or ""
                if "instagram.com/" in href and "instagram.com/p/" not in href:
                    result["instagram_url"] = href
                    break

            browser.close()
    except Exception:
        pass
    return result


def _save_lead(data, job_id):
    from database import get_db, _compute_score, _priority_label
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM leads WHERE LOWER(name)=LOWER(?) AND LOWER(city)=LOWER(?)",
                                (data.get("name"), data.get("city"))).fetchone()
        if existing:
            return
        conn.execute("""
            INSERT INTO leads (name, owner_name, address, city, phone, email, website,
                business_type, priority, quality_score,
                facebook_url, facebook_active, facebook_followers, facebook_last_post,
                instagram_url, instagram_active, instagram_followers, instagram_last_post,
                source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("name"), data.get("owner_name"), data.get("address"),
            data.get("city"), data.get("phone"), data.get("email"),
            data.get("website"), data.get("business_type"),
            data.get("priority", "Cold"), data.get("quality_score", 0),
            data.get("facebook_url"), int(data.get("facebook_active", False)),
            data.get("facebook_followers"), data.get("facebook_last_post"),
            data.get("instagram_url"), int(data.get("instagram_active", False)),
            data.get("instagram_followers"), data.get("instagram_last_post"),
            data.get("source", "google_maps"),
        ))


def _compute_score(website, fb_url, fb_active, ig_url, ig_active):
    from database import _compute_score as db_score
    return db_score(website, fb_url, fb_active, ig_url, ig_active)


def _priority_label(score):
    from database import _priority_label as db_label
    return db_label(score)
