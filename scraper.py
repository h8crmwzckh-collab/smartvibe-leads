"""
Multi-source lead scraper: Google Maps + Yelp + BBB + Yellow Pages.
Extracts emails from business websites, checks real social media activity,
and scores leads based on web presence quality.
"""
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urljoin

STATIC_DIR = Path(__file__).parent / "static"

# ─── Public API ───────────────────────────────────────────────────────────────

def run_scrape_job(job_id: int, cities: list[str], business_type: str, lead_count: int):
    thread = threading.Thread(
        target=_scrape_worker,
        args=(job_id, cities, business_type, lead_count),
        daemon=True,
    )
    thread.start()


# ─── Job state ────────────────────────────────────────────────────────────────

def _update_job(job_id, **kwargs):
    from database import get_db
    with get_db() as conn:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE scrape_jobs SET {sets} WHERE id=?", list(kwargs.values()) + [job_id])


# ─── Worker ───────────────────────────────────────────────────────────────────

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
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )

            for city in cities:
                city_found = 0
                seen_phones = set()  # primary dedup key

                sources = [
                    (_scrape_google_maps, per_city * 2 // 3),
                    (_scrape_yelp,        per_city * 1 // 3),
                    (_scrape_yellow_pages, per_city // 4),
                    (_scrape_bbb,          per_city // 4),
                ]

                for scrape_fn, target in sources:
                    if city_found >= per_city:
                        break
                    remaining = per_city - city_found
                    try:
                        raw_leads = scrape_fn(ctx, city, business_type, max(target, remaining), job_id)
                    except Exception as e:
                        print(f"[scraper] {scrape_fn.__name__} failed for {city}: {e}")
                        raw_leads = []

                    for lead in raw_leads:
                        if city_found >= per_city:
                            break
                        phone = _normalize_phone(lead.get("phone") or "")
                        if phone and phone in seen_phones:
                            continue  # same business, different source
                        if phone:
                            seen_phones.add(phone)
                        if _save_lead(lead, job_id):
                            found += 1
                            city_found += 1
                            _update_job(job_id, leads_found=found)

            browser.close()

    except Exception as e:
        print(f"[scraper] worker crashed: {e}")
        _update_job(job_id, status="error", finished_at=datetime.now().isoformat())
        return

    _update_job(job_id, status="done", leads_found=found, finished_at=datetime.now().isoformat())


# ─── Google Maps ──────────────────────────────────────────────────────────────

def _scrape_google_maps(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        query = f"{business_type} in {city}"
        page.goto(f"https://www.google.com/maps/search/{query.replace(' ', '+')}/", timeout=30000)
        page.wait_for_timeout(3000)

        seen_names = set()
        scroll_attempts = 0

        while len(leads) < target and scroll_attempts < 80:
            scroll_attempts += 1
            items = page.query_selector_all('[role="feed"] > div')

            for item in items:
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
                    # Wait for detail panel — address or phone appearing signals it's loaded
                    try:
                        page.wait_for_selector(
                            '[data-item-id="address"], [data-item-id^="phone"], [aria-label="Address"]',
                            timeout=6000,
                        )
                    except Exception:
                        page.wait_for_timeout(3000)

                    lead = _extract_gmaps_lead(page, name, city, business_type)
                    if lead:
                        # Deep enrich: visit website for email
                        if lead.get("website"):
                            lead["email"] = _extract_email_from_website(ctx, lead["website"]) or lead.get("email")
                        leads.append(lead)

                    page.go_back()
                    page.wait_for_timeout(1800)
                except Exception:
                    continue

            try:
                panel = page.query_selector('[role="feed"]')
                if panel:
                    panel.evaluate("el => el.scrollBy(0, 1400)")
                page.wait_for_timeout(1800)
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] GMaps {city}: {e}")
    finally:
        page.close()
    return leads


def _extract_gmaps_lead(page, name, city, business_type):
    try:
        data = {"name": name, "city": city, "business_type": business_type, "source": "google_maps"}

        # Address — multiple selectors + regex fallback
        for sel in [
            '[data-item-id="address"]',
            'button[data-item-id="address"]',
            '[aria-label="Address"]',
            'button[aria-label*="ddress"]',
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt and len(txt) > 5:
                        data["address"] = txt
                        break
            except Exception:
                continue

        if not data.get("address"):
            try:
                body = page.inner_text('[role="main"]')
                m = re.search(
                    r'\d+\s+[A-Za-z][A-Za-z\s]+(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct|Pl|Circle|Pkwy|Highway|Hwy|Suite|Ste)[\w\s,\.]*',
                    body,
                )
                if m:
                    data["address"] = m.group(0).strip().rstrip(",")
            except Exception:
                pass

        # Phone
        for sel in ['[data-item-id^="phone"]', 'button[data-item-id^="phone"]']:
            try:
                el = page.query_selector(sel)
                if el:
                    data["phone"] = el.inner_text().strip()
                    break
            except Exception:
                continue

        # Website
        for sel in ['[data-item-id="authority"]', 'a[data-item-id="authority"]']:
            try:
                el = page.query_selector(sel)
                if el:
                    data["website"] = el.get_attribute("href") or el.inner_text().strip()
                    break
            except Exception:
                continue

        # Rating + review count
        try:
            body = page.inner_text('[role="main"]')
            rating_m = re.search(r'(\d\.\d)\s*\([\d,]+\)', body)
            if rating_m:
                data["rating"] = float(rating_m.group(1))
            review_m = re.search(r'\(([\d,]+)\s+review', body)
            if review_m:
                data["review_count"] = int(review_m.group(1).replace(",", ""))
        except Exception:
            pass

        # Owner name
        try:
            body = page.inner_text('[role="main"]')
            m = re.search(r'(?:Owner|Manager)[\s:·]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', body)
            if m:
                data["owner_name"] = m.group(1).strip()
        except Exception:
            pass

        social = _check_social_real(name, city, ctx=page.context)
        data.update(social)

        data["quality_score"] = _score(data)
        data["priority"] = _priority_label(data["quality_score"])
        return data
    except Exception as e:
        print(f"[scraper] GMaps extract '{name}': {e}")
        return None


# ─── Yelp ─────────────────────────────────────────────────────────────────────

def _scrape_yelp(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        slug_city = city.replace(" ", "+")
        slug_type = business_type.replace(" ", "+")
        page.goto(
            f"https://www.yelp.com/search?find_desc={slug_type}&find_loc={slug_city}",
            timeout=30000,
        )
        page.wait_for_timeout(3000)

        seen_names = set()
        page_num = 0

        while len(leads) < target and page_num < 6:
            page_num += 1

            biz_links = []
            for link in page.query_selector_all('a[href*="/biz/"]'):
                href = link.get_attribute("href") or ""
                if "/biz/" in href:
                    clean = href.split("?")[0]
                    full = clean if clean.startswith("http") else f"https://www.yelp.com{clean}"
                    if full not in biz_links:
                        biz_links.append(full)

            for url in biz_links:
                if len(leads) >= target:
                    break
                detail = ctx.new_page()
                try:
                    detail.goto(url, timeout=20000)
                    detail.wait_for_timeout(2500)
                    lead = _extract_yelp_lead(detail, city, business_type)
                    if lead and lead.get("name") not in seen_names:
                        seen_names.add(lead["name"])
                        if lead.get("website"):
                            lead["email"] = _extract_email_from_website(ctx, lead["website"]) or lead.get("email")
                        leads.append(lead)
                except Exception:
                    pass
                finally:
                    detail.close()

            try:
                nxt = page.query_selector('a[aria-label="Next"]')
                if nxt:
                    nxt.click()
                    page.wait_for_timeout(2500)
                else:
                    break
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] Yelp {city}: {e}")
    finally:
        page.close()
    return leads


def _extract_yelp_lead(page, city, business_type):
    try:
        data = {"city": city, "business_type": business_type, "source": "yelp"}

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
            for sel in ['address', '[class*="address" i]', 'p[class*="Address" i]']:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip().replace("\n", ", ")
                    if txt and len(txt) > 5:
                        data["address"] = txt
                        break
        except Exception:
            pass

        # Phone
        try:
            el = page.query_selector('a[href^="tel:"]')
            if el:
                data["phone"] = (el.get_attribute("href") or "").replace("tel:", "").strip()
            else:
                body = page.inner_text("body")
                m = re.search(r'\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}', body)
                if m:
                    data["phone"] = m.group(0)
        except Exception:
            pass

        # Website
        try:
            el = page.query_selector('a[href*="biz_redir"], a[href*="redirect_uri"]')
            if el:
                data["website"] = el.get_attribute("href") or ""
        except Exception:
            pass

        # Rating + review count
        try:
            body = page.inner_text("body")
            rating_m = re.search(r'(\d\.\d)\s+star', body)
            if rating_m:
                data["rating"] = float(rating_m.group(1))
            review_m = re.search(r'([\d,]+)\s+review', body)
            if review_m:
                data["review_count"] = int(review_m.group(1).replace(",", ""))
        except Exception:
            pass

        # Owner name
        try:
            body = page.inner_text("body")
            m = re.search(
                r'(?:Meet the (?:Owner|Manager|Business Owner)|From the (?:owner|business))[\s:·"–-]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
                body,
            )
            if m:
                data["owner_name"] = m.group(1).strip()
        except Exception:
            pass

        # Years in business
        try:
            body = page.inner_text("body")
            m = re.search(r'(\d{4})\s+(?:established|founded|in business)', body, re.IGNORECASE)
            if m:
                data["years_in_business"] = datetime.now().year - int(m.group(1))
        except Exception:
            pass

        social = _check_social_real(data["name"], city, ctx=page.context)
        data.update(social)
        data["quality_score"] = _score(data)
        data["priority"] = _priority_label(data["quality_score"])
        return data
    except Exception as e:
        print(f"[scraper] Yelp extract: {e}")
        return None


# ─── Yellow Pages ─────────────────────────────────────────────────────────────

def _scrape_yellow_pages(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        slug_city = city.replace(" ", "-").lower()
        slug_type = business_type.replace(" ", "-").lower()
        page.goto(
            f"https://www.yellowpages.com/search?search_terms={slug_type}&geo_location_terms={slug_city}",
            timeout=25000,
        )
        page.wait_for_timeout(2500)

        seen_names = set()
        page_num = 0

        while len(leads) < target and page_num < 4:
            page_num += 1
            cards = page.query_selector_all(".result .info")

            for card in cards:
                if len(leads) >= target:
                    break
                try:
                    name_el = card.query_selector("a.business-name")
                    if not name_el:
                        continue
                    name = name_el.inner_text().strip()
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)

                    data = {"name": name, "city": city, "business_type": business_type, "source": "yellow_pages"}

                    try:
                        phone_el = card.query_selector(".phones")
                        if phone_el:
                            data["phone"] = phone_el.inner_text().strip()
                    except Exception:
                        pass

                    try:
                        addr_el = card.query_selector(".adr")
                        if addr_el:
                            data["address"] = addr_el.inner_text().strip().replace("\n", ", ")
                    except Exception:
                        pass

                    try:
                        web_el = card.query_selector("a.track-visit-website")
                        if web_el:
                            data["website"] = web_el.get_attribute("href") or ""
                    except Exception:
                        pass

                    if data.get("website"):
                        data["email"] = _extract_email_from_website(ctx, data["website"]) or data.get("email")

                    social = _check_social_real(name, city, ctx=ctx)
                    data.update(social)
                    data["quality_score"] = _score(data)
                    data["priority"] = _priority_label(data["quality_score"])
                    leads.append(data)

                except Exception:
                    continue

            try:
                nxt = page.query_selector('a[class*="next"]')
                if nxt:
                    nxt.click()
                    page.wait_for_timeout(2000)
                else:
                    break
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] YellowPages {city}: {e}")
    finally:
        page.close()
    return leads


# ─── BBB ──────────────────────────────────────────────────────────────────────

def _scrape_bbb(ctx, city, business_type, target, job_id):
    leads = []
    page = ctx.new_page()
    try:
        slug_city = city.replace(" ", "+")
        slug_type = business_type.replace(" ", "+")
        page.goto(
            f"https://www.bbb.org/search?find_text={slug_type}&find_loc={slug_city}",
            timeout=25000,
        )
        page.wait_for_timeout(3000)

        seen_names = set()
        page_num = 0

        while len(leads) < target and page_num < 4:
            page_num += 1
            cards = page.query_selector_all('[data-testid="search-result-card"], .result-card, .MuiPaper-root')

            for card in cards:
                if len(leads) >= target:
                    break
                try:
                    name_el = card.query_selector("h3 a, h2 a, a[data-testid='biz-name']")
                    if not name_el:
                        continue
                    name = name_el.inner_text().strip()
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)

                    biz_href = name_el.get_attribute("href") or ""
                    biz_url = biz_href if biz_href.startswith("http") else f"https://www.bbb.org{biz_href}"

                    detail = ctx.new_page()
                    try:
                        detail.goto(biz_url, timeout=18000)
                        detail.wait_for_timeout(2000)
                        lead = _extract_bbb_lead(detail, name, city, business_type)
                        if lead:
                            if lead.get("website"):
                                lead["email"] = _extract_email_from_website(ctx, lead["website"]) or lead.get("email")
                            leads.append(lead)
                    except Exception:
                        pass
                    finally:
                        detail.close()

                except Exception:
                    continue

            try:
                nxt = page.query_selector('button[aria-label="Next page"], a[aria-label="Next"]')
                if nxt:
                    nxt.click()
                    page.wait_for_timeout(2200)
                else:
                    break
            except Exception:
                break

    except Exception as e:
        print(f"[scraper] BBB {city}: {e}")
    finally:
        page.close()
    return leads


def _extract_bbb_lead(page, name, city, business_type):
    try:
        data = {"name": name, "city": city, "business_type": business_type, "source": "bbb"}

        body = page.inner_text("body")

        # Address
        try:
            el = page.query_selector('[data-testid="business-address"], address, .dtm-address')
            if el:
                data["address"] = el.inner_text().strip().replace("\n", ", ")
        except Exception:
            pass

        # Phone
        try:
            el = page.query_selector('[data-testid="business-phone"], a[href^="tel:"]')
            if el:
                href = el.get_attribute("href") or ""
                data["phone"] = href.replace("tel:", "") if href.startswith("tel:") else el.inner_text().strip()
        except Exception:
            pass

        # Website
        try:
            el = page.query_selector('a[data-testid="website-link"], a[href*="bbb.org/link"]')
            if el:
                data["website"] = el.get_attribute("href") or ""
        except Exception:
            pass

        # Owner name from "Principal" section (BBB lists owner/principal)
        try:
            m = re.search(r'Principal\s*[:\n]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', body)
            if m:
                data["owner_name"] = m.group(1).strip()
        except Exception:
            pass

        # BBB rating
        try:
            m = re.search(r'BBB Rating\s*[:\n\s]+([A-F][+-]?)', body)
            if m:
                data["bbb_rating"] = m.group(1)
        except Exception:
            pass

        # Years in business
        try:
            m = re.search(r'(?:Year Founded|In Business Since)[:\s]+(\d{4})', body, re.IGNORECASE)
            if m:
                data["years_in_business"] = datetime.now().year - int(m.group(1))
        except Exception:
            pass

        social = _check_social_real(name, city, ctx=page.context)
        data.update(social)
        data["quality_score"] = _score(data)
        data["priority"] = _priority_label(data["quality_score"])
        return data
    except Exception as e:
        print(f"[scraper] BBB extract '{name}': {e}")
        return None


# ─── Email extraction from website ────────────────────────────────────────────

def _extract_email_from_website(ctx, url: str) -> str | None:
    """Visit a business website and pull the first contact email found."""
    if not url or "yelp.com" in url or "google.com" in url:
        return None
    try:
        page = ctx.new_page()
        try:
            # Try homepage first
            page.goto(url, timeout=12000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            email = _find_email_on_page(page)

            # If not found on homepage, try /contact
            if not email:
                parsed = urlparse(url)
                contact_url = f"{parsed.scheme}://{parsed.netloc}/contact"
                try:
                    page.goto(contact_url, timeout=8000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)
                    email = _find_email_on_page(page)
                except Exception:
                    pass

            return email
        finally:
            page.close()
    except Exception:
        return None


def _find_email_on_page(page) -> str | None:
    try:
        # Check mailto links first (most reliable)
        for el in page.query_selector_all('a[href^="mailto:"]'):
            href = el.get_attribute("href") or ""
            email = href.replace("mailto:", "").split("?")[0].strip()
            if _valid_email(email):
                return email

        # Fall back to regex on visible text
        body = page.inner_text("body")
        matches = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', body)
        for m in matches:
            if _valid_email(m) and not _generic_email(m):
                return m
    except Exception:
        pass
    return None


def _valid_email(email: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email))


def _generic_email(email: str) -> bool:
    skip = ["example.com", "domain.com", "yoursite.com", "email.com", "sentry.io",
            "wixpress.com", "squarespace.com", "wordpress.com"]
    return any(s in email for s in skip)


# ─── Social media — real page visits ──────────────────────────────────────────

def _check_social_real(business_name: str, city: str, ctx=None) -> dict:
    result = {
        "facebook_url": None, "facebook_active": False, "facebook_followers": None,
        "instagram_url": None, "instagram_active": False, "instagram_followers": None,
    }
    try:
        from playwright.sync_api import sync_playwright

        def _do(page):
            safe = re.sub(r"[^a-zA-Z0-9 ]", "", business_name)

            # ── Facebook ──────────────────────────────────────────────
            page.goto(
                f"https://www.google.com/search?q={safe.replace(' ', '+')}+{city.replace(' ', '+')}+site:facebook.com",
                timeout=15000,
            )
            page.wait_for_timeout(1200)
            fb_url = None
            for link in page.query_selector_all("a[href*='facebook.com']"):
                href = link.get_attribute("href") or ""
                if (
                    "facebook.com/" in href
                    and "/l.facebook" not in href
                    and "facebook.com/search" not in href
                    and "facebook.com/login" not in href
                    and "facebook.com/groups" not in href
                ):
                    fb_url = href
                    break

            if fb_url:
                result["facebook_url"] = fb_url
                # Visit the page to check activity
                try:
                    page.goto(fb_url, timeout=12000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    body = page.inner_text("body")

                    # Follower count
                    m = re.search(r'([\d,]+)\s+(?:people follow|followers)', body, re.IGNORECASE)
                    if m:
                        result["facebook_followers"] = int(m.group(1).replace(",", ""))

                    # Active = posted within last 90 days
                    result["facebook_active"] = _has_recent_activity(body)
                except Exception:
                    pass

            # ── Instagram ─────────────────────────────────────────────
            page.goto(
                f"https://www.google.com/search?q={safe.replace(' ', '+')}+{city.replace(' ', '+')}+site:instagram.com",
                timeout=15000,
            )
            page.wait_for_timeout(1200)
            ig_url = None
            for link in page.query_selector_all("a[href*='instagram.com']"):
                href = link.get_attribute("href") or ""
                if (
                    "instagram.com/" in href
                    and "instagram.com/p/" not in href
                    and "instagram.com/explore" not in href
                    and "instagram.com/reel" not in href
                ):
                    ig_url = href
                    break

            if ig_url:
                result["instagram_url"] = ig_url
                try:
                    page.goto(ig_url, timeout=12000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    body = page.inner_text("body")

                    m = re.search(r'([\d,.]+[KkMm]?)\s+[Ff]ollowers', body)
                    if m:
                        result["instagram_followers"] = _parse_follower_count(m.group(1))

                    result["instagram_active"] = _has_recent_activity(body)
                except Exception:
                    pass

        if ctx:
            p = ctx.new_page()
            try:
                _do(p)
            finally:
                p.close()
        else:
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                p = b.new_context().new_page()
                _do(p)
                b.close()

    except Exception as e:
        print(f"[scraper] social check '{business_name}': {e}")

    return result


def _has_recent_activity(body: str) -> bool:
    """Check if page body contains timestamps suggesting recent posts."""
    patterns = [
        r'\b(just now|[1-9]\d?\s+(?:minute|hour|day|week)s?\s+ago)\b',
        r'\b([1-9]|[1-8]\d)\s+(?:days?|weeks?)\s+ago\b',
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,?\s+202[3-9])?\b',
    ]
    cutoff = datetime.now() - timedelta(days=90)
    for pat in patterns:
        if re.search(pat, body, re.IGNORECASE):
            return True
    return False


def _parse_follower_count(raw: str) -> int | None:
    try:
        raw = raw.replace(",", "").strip()
        if raw[-1].upper() == "K":
            return int(float(raw[:-1]) * 1000)
        if raw[-1].upper() == "M":
            return int(float(raw[:-1]) * 1_000_000)
        return int(raw)
    except Exception:
        return None


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _score(data: dict) -> int:
    """
    Score 0-100 based on web/social presence gaps — higher = weaker presence = hotter lead.
    """
    score = 0

    # No website = huge opportunity
    if not data.get("website"):
        score += 30
    else:
        score += 10  # has a website — still opportunity to improve social

    # Facebook
    if not data.get("facebook_url"):
        score += 20
    elif not data.get("facebook_active"):
        score += 15  # has page but dormant
    else:
        fb_followers = data.get("facebook_followers") or 0
        if fb_followers < 200:
            score += 8
        elif fb_followers < 1000:
            score += 4

    # Instagram
    if not data.get("instagram_url"):
        score += 20
    elif not data.get("instagram_active"):
        score += 15
    else:
        ig_followers = data.get("instagram_followers") or 0
        if ig_followers < 200:
            score += 8
        elif ig_followers < 1000:
            score += 4

    # Email found = more contact options
    if data.get("email"):
        score += 5

    # Phone found
    if data.get("phone"):
        score += 5

    # Many reviews = established business worth targeting
    reviews = data.get("review_count") or 0
    if reviews > 50:
        score += 5
    elif reviews > 10:
        score += 3

    return min(score, 100)


def _priority_label(score: int) -> str:
    if score >= 70:
        return "Hot"
    if score >= 45:
        return "Warm"
    return "Cold"


def _normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-10:] if phone else ""


# ─── Save ─────────────────────────────────────────────────────────────────────

def _save_lead(data: dict, job_id: int) -> bool:
    """Insert lead if not already in DB (by name+city). Returns True if inserted."""
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
            INSERT INTO leads (
                name, owner_name, address, city, phone, email, website,
                business_type, priority, quality_score,
                facebook_url, facebook_active, facebook_followers, facebook_last_post,
                instagram_url, instagram_active, instagram_followers, instagram_last_post,
                source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("name"),
                data.get("owner_name"),
                data.get("address"),
                data.get("city"),
                data.get("phone"),
                data.get("email"),
                data.get("website"),
                data.get("business_type"),
                data.get("priority", "Cold"),
                data.get("quality_score", 0),
                data.get("facebook_url"),
                int(bool(data.get("facebook_active"))),
                data.get("facebook_followers"),
                data.get("facebook_last_post"),
                data.get("instagram_url"),
                int(bool(data.get("instagram_active"))),
                data.get("instagram_followers"),
                data.get("instagram_last_post"),
                data.get("source", "google_maps"),
            ),
        )
    return True
