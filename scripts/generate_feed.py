#!/usr/bin/env python3
"""
Generuje kanał RSS na podstawie strony https://zero.pl/najnowsze.

Zero.pl nie udostępnia oficjalnego RSS, więc ten skrypt parsuje HTML
strony z listą najnowszych artykułów i zapisuje/aktualizuje plik RSS
(domyślnie docs/feed.xml).

Każde uruchomienie DOŁĄCZA nowo znalezione artykuły do istniejącego
pliku (na podstawie linku jako unikalnego identyfikatora), więc feed
rośnie w czasie zamiast ograniczać się tylko do tego, co akurat widać
na stronie w danym momencie. Liczba pozycji jest ograniczona przez
MAX_ITEMS.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sax
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://zero.pl"
LIST_URL = f"{BASE_URL}/najnowsze"
FEED_PATH = os.environ.get("FEED_PATH", "docs/feed.xml")
FEED_SELF_URL = os.environ.get("FEED_SELF_URL", LIST_URL)
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "150"))
DEBUG_HTML_PATH = os.environ.get("DEBUG_HTML_PATH", "debug_page.html")
WARSAW = ZoneInfo("Europe/Warsaw")

CATEGORIES = {
    "Kraj", "Świat", "Sport", "Biznes", "Technologia", "Wojsko",
    "Zdrowie", "Kultura", "Nauka", "Moto", "Opinie", "Program TV",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# "Dzisiaj 10:08" / "Wczoraj 21:41" / "24 lipca 09:00"
DATE_RE = re.compile(r"^(Dzisiaj|Wczoraj|\d{1,2}\s+\S+)\s+(\d{1,2}):(\d{2})$")
# "27.07.2026 21:59" — używane dla starszych pozycji na liście
DATE_FULL_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})$")
MIN_RE = re.compile(r"^\d+\s*min$")

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}


def parse_pl_datetime(text: str, now: datetime) -> datetime:
    """Parsuje polski znacznik czasu w stylu 'Dzisiaj HH:MM' na obiekt datetime."""
    text = text.strip()

    m_full = DATE_FULL_RE.match(text)
    if m_full:
        dd, mm, yyyy, hh, mi = m_full.groups()
        return datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), tzinfo=WARSAW)

    m = DATE_RE.match(text)
    if not m:
        return now
    day_part, hh, mm = m.groups()
    hh, mm = int(hh), int(mm)

    if day_part == "Dzisiaj":
        d = now.date()
    elif day_part == "Wczoraj":
        d = (now - timedelta(days=1)).date()
    else:
        parts = day_part.split()
        if len(parts) == 2 and parts[1].lower() in MONTHS_PL:
            day = int(parts[0])
            month = MONTHS_PL[parts[1].lower()]
            candidate = now.replace(month=month, day=day, hour=0, minute=0,
                                     second=0, microsecond=0)
            if candidate.date() > now.date():
                candidate = candidate.replace(year=now.year - 1)
            d = candidate.date()
        else:
            d = now.date()

    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=WARSAW)


def fetch_rendered_html(url: str) -> str:
    """Otwiera stronę w headless Chromium i zwraca w pełni wyrenderowany HTML.

    zero.pl jest aplikacją, która renderuje/doładowuje listę artykułów
    po stronie klienta i stoi za Cloudflare, więc zwykłe `requests.get`
    (bez wykonania JS) może dostać puste/inne dane albo zostać
    zablokowane. Playwright uruchamia prawdziwą przeglądarkę.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        # "networkidle" na stronach z reklamami/analityką (ciągły ruch
        # w tle) potrafi nigdy nie nastąpić i tylko czeka do timeoutu —
        # czekamy więc na sam załadowany DOM, a potem jawnie na
        # pojawienie się kart artykułów.
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_selector('a[href*="/news/"]', timeout=30_000)
        except Exception:
            # Dajemy jeszcze chwilę na doładowanie JS-em, zanim się poddamy
            # — parse_articles i tak zdiagnozuje brak wyników.
            page.wait_for_timeout(5_000)
        html = page.content()
        browser.close()
    return html


def parse_articles(html: str):
    """Parsuje wyrenderowany HTML strony listy artykułów.

    Każdy artykuł to <article class="post ..."> zawierający m.in.:
    - .post__title              -> tytuł
    - .post__author-name        -> autor(zy)
    - .post__info .font-bold    -> kategoria
    - .post__info__item (x2)    -> znacznik czasu, czas czytania
    - a.post__link               -> link do artykułu (cała karta jest klikalna)
    - figure img                 -> miniatura
    """
    soup = BeautifulSoup(html, "lxml")

    now = datetime.now(WARSAW)
    items = []
    seen_links = set()

    for article in soup.select("article.post"):
        link_el = article.select_one("a.post__link[href]")
        if not link_el:
            continue
        href = link_el["href"]
        link = href if href.startswith("http") else BASE_URL + href
        if link in seen_links:
            continue

        title_el = article.select_one(".post__title")
        title = title_el.get_text(strip=True) if title_el else ""
        if len(title) < 8:
            continue

        author_el = article.select_one(".post__author-name")
        author = author_el.get_text(strip=True) if author_el else None

        category_el = article.select_one(".post__info .font-bold")
        category = category_el.get_text(strip=True) if category_el else None

        info_items = article.select(".post__info__item")
        pub_dt = now
        if info_items:
            pub_dt = parse_pl_datetime(info_items[0].get_text(strip=True), now)

        thumb_el = article.select_one("figure img")
        thumb_src = thumb_el.get("src") if thumb_el else None

        items.append({
            "title": title,
            "link": link,
            "category": category,
            "author": author,
            "pub_dt": pub_dt,
            "image": thumb_src,
        })
        seen_links.add(link)

    return items


def fetch_articles():
    """Pobiera wyrenderowaną stronę i zwraca sparsowaną listę artykułów."""
    html = fetch_rendered_html(LIST_URL)
    items = parse_articles(html)
    if not items:
        # Zapisz HTML do pliku, żeby dało się zdiagnozować co poszło nie
        # tak (np. w workflow jako artefakt do pobrania).
        with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print(
            f"Nie znaleziono artykułów. Zapisano wyrenderowany HTML do "
            f"{DEBUG_HTML_PATH} (długość: {len(html)} znaków) do diagnozy.",
            file=sys.stderr,
        )
    return items


def rfc822(dt: datetime) -> str:
    return format_datetime(dt)


def load_existing(path):
    """Wczytuje istniejący plik RSS (jeśli jest), zwraca dict link -> dane pozycji."""
    if not os.path.exists(path):
        return {}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {}

    channel = tree.getroot().find("channel")
    if channel is None:
        return {}

    existing = {}
    for item in channel.findall("item"):
        link_el = item.find("link")
        if link_el is None or not link_el.text:
            continue
        link = link_el.text.strip()
        existing[link] = {
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "category": (item.findtext("category") or "").strip() or None,
            "pub_date_raw": (item.findtext("pubDate") or "").strip(),
            "description": (item.findtext("description") or "").strip() or None,
        }
    return existing


def sort_key(entry):
    try:
        return parsedate_to_datetime(entry["pub_date_raw"])
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def build_feed(items, path):
    existing = load_existing(path)
    merged = dict(existing)

    for it in items:
        description = None
        if it["category"] and it["author"]:
            description = f'{it["category"]} — {it["author"]}'
        elif it["category"]:
            description = it["category"]

        merged[it["link"]] = {
            "title": it["title"],
            "link": it["link"],
            "category": it["category"],
            "pub_date_raw": rfc822(it["pub_dt"]),
            "description": description,
        }

    all_entries = sorted(merged.values(), key=sort_key, reverse=True)[:MAX_ITEMS]

    now_rfc = rfc822(datetime.now(WARSAW))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{sax.escape('Zero.pl — Najnowsze')}</title>",
        f"<link>{sax.escape(LIST_URL)}</link>",
        f"<description>{sax.escape('Najnowsze artykuly z Zero.pl (nieoficjalny kanal RSS)')}</description>",
        "<language>pl</language>",
        f"<lastBuildDate>{now_rfc}</lastBuildDate>",
        f'<atom:link rel="self" type="application/rss+xml" href="{sax.escape(FEED_SELF_URL)}"/>',
    ]

    for e in all_entries:
        parts.append("<item>")
        parts.append(f"<title>{sax.escape(e['title'])}</title>")
        parts.append(f"<link>{sax.escape(e['link'])}</link>")
        parts.append(f'<guid isPermaLink="true">{sax.escape(e["link"])}</guid>')
        if e.get("category"):
            parts.append(f"<category>{sax.escape(e['category'])}</category>")
        if e.get("description"):
            parts.append(f"<description>{sax.escape(e['description'])}</description>")
        if e.get("pub_date_raw"):
            parts.append(f"<pubDate>{e['pub_date_raw']}</pubDate>")
        parts.append("</item>")

    parts.append("</channel>")
    parts.append("</rss>")

    out_dir = os.path.dirname(path) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")

    # GitHub Pages domyślnie próbuje zbudować docs/ jako stronę Jekyll,
    # co wywala się przy braku pełnej struktury Jekyll (np. na
    # assets/css/style.scss). .nojekyll wyłącza to całkowicie i każe
    # Pages serwować pliki as-is.
    nojekyll_path = os.path.join(out_dir, ".nojekyll")
    if not os.path.exists(nojekyll_path):
        open(nojekyll_path, "w").close()


def main():
    items = fetch_articles()
    if not items:
        print(
            "Nie udało się sparsować żadnych artykułów — przerywam, "
            "żeby nie nadpisać istniejącego feeda pustym plikiem.",
            file=sys.stderr,
        )
        sys.exit(1)

    build_feed(items, FEED_PATH)
    print(f"Zapisano {FEED_PATH}: {len(items)} artykułów z bieżącego skanu.")


if __name__ == "__main__":
    main()
