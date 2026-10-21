"""Publisher website monitoring. Only articles with a verifiable Baku-local publication date."""
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

MONTHS = {
    "yanvar":1,"fevral":2,"mart":3,"aprel":4,"may":5,"iyun":6,
    "iyul":7,"avqust":8,"sentyabr":9,"oktyabr":10,"noyabr":11,"dekabr":12
}
MONTH_PATTERN = "|".join(MONTHS)
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+("+MONTH_PATTERN+r")\s+(20\d{2})(?!\d)", re.I)
NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](20\d{2})(?!\d)")
ISO_DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2}))?")
def parse_date(text):
    if not text: return None
    m = DATE_RE.search(text)
    if m:
        try: return datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).date()
        except ValueError: return None
    m = NUMERIC_DATE_RE.search(text)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
        except ValueError: return None
    m = ISO_DATE_RE.search(text)
    if m:
        try: return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError: return None
    return None

def extract_candidates(html, listing_url, limit=30):
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(listing_url).hostname or ""
    seen=set()
    results=[]
    for a in soup.select("a[href]"):
        href = urljoin(listing_url, a.get("href",""))
        parsed=urlparse(href)
        if parsed.scheme != "https" or parsed.hostname != host: continue
        if not (parsed.path.startswith("/post/") or "/media/xeberler/" in parsed.path): continue
        if href in seen: continue
        title = a.get_text(" ", strip=True)
        if len(title) < 22: continue
        seen.add(href)
        results.append((title, href))
        if len(results)>=limit: break
    return results

def article_is_today(html, expected_day):
    soup=BeautifulSoup(html,"html.parser")
    # Prefer publisher's explicit publication metadata; never use modified timestamps.
    for selector in ('meta[property="article:published_time"]','meta[name="datePublished"]',
                     'meta[itemprop="datePublished"]','time[datetime]'):
        for tag in soup.select(selector):
            value=tag.get("content") or tag.get("datetime") or ""
            day=parse_date(value)
            if day: return day == expected_day
    for tag in soup.select("script[type='application/ld+json']"):
        raw=tag.get_text(" ",strip=True)
        m=re.search(r'"datePublished"\s*:\s*"([^"]+)"',raw)
        if m:
            day=parse_date(m.group(1))
            if day: return day == expected_day
    # Visible publication date near the article heading, not arbitrary dates in the body.
    h=soup.find("h1")
    if h:
        node=h
        for _ in range(3):
            if node.parent is None: break
            node=node.parent
            text=node.get_text(" ",strip=True)[:900]
            day=parse_date(text)
            if day: return day == expected_day
    # A short leading text region is allowed only when a single date is unambiguous.
    for el in soup(["script","style","nav","footer","header"]): el.decompose()
    text=soup.get_text(" ",strip=True)[:1600]
    days={d for m in DATE_RE.finditer(text) if (d:=parse_date(m.group(0)))}
    return len(days)==1 and expected_day in days

async def collect_official(client, source, today, max_articles=12):
    resp=await client.get(source["url"])
    resp.raise_for_status()
    candidates=extract_candidates(resp.text,source["url"])
    results=[]
    for title,url in candidates[:max_articles]:
        try:
            r=await client.get(url)
            r.raise_for_status()
            if article_is_today(r.text,today):
                results.append({"title":title,"link":url,"published":today.isoformat(),"summary":title})
        except Exception as exc:
            print("Official article error",url,repr(exc),flush=True)
    return results
