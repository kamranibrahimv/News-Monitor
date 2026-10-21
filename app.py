import os, json, sqlite3, asyncio, html, secrets, calendar
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse, quote
import feedparser, httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

load_dotenv()
DB=os.getenv('DATABASE_PATH','news.db')
TOKEN=os.getenv('ADMIN_TOKEN','')
INTERVAL=max(1,int(os.getenv('CHECK_MINUTES','5')))
BOT=os.getenv('TELEGRAM_BOT_TOKEN','')
CHAT=os.getenv('TELEGRAM_CHAT_ID','')
SOURCES=Path(__file__).with_name('sources.json')
scheduler=AsyncIOScheduler(timezone='UTC')
lock=asyncio.Lock()
BAKU=ZoneInfo('Asia/Baku')

def today_published(entry):
    try:
        parsed=entry.get('published_parsed') or entry.get('updated_parsed')
        if parsed:
            dt=datetime.fromtimestamp(calendar.timegm(parsed),timezone.utc)
        else:
            raw=entry.get('published') or entry.get('updated')
            if not raw: return False
            dt=parsedate_to_datetime(str(raw))
            if dt.tzinfo is None: return False
        local=dt.astimezone(BAKU)
        now=datetime.now(BAKU)
        return local.date()==now.date() and local<=now+timedelta(minutes=5)
    except (ValueError,TypeError,OverflowError): return False


def connect():
    Path(DB).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB,timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def setup():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS articles (id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, source TEXT, published TEXT, discovered TEXT, matched INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS keywords (term TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS deliveries (url TEXT, chat_id TEXT, sent_at TEXT, PRIMARY KEY(url,chat_id))')
        defaults=['DSMF','Dövlət Sosial Müdafiə Fondu','Dövlət Sosial Müdafiə Fondunun','pensiya','pensiyalar','pensiyaçı','pensiya kapitalı','pensiya ödənişi','pensiya artımı','müavinət','sosial müavinət','sosial yardım','ünvanlı dövlət sosial yardımı','sosial sığorta']
        for term in defaults:
            c.execute('INSERT OR IGNORE INTO keywords(term) VALUES(?)',(term,))

def authorized(request):
    if not TOKEN or not secrets.compare_digest(request.headers.get('x-admin-token',''),TOKEN):
        raise HTTPException(401,'Invalid or missing X-Admin-Token')

async def notify(client,title,url,source):
    if not BOT: return {'sent':0,'failed':0}
    with connect() as c:
        chats=[r[0] for r in c.execute("SELECT chat_id FROM subscribers")]
    if CHAT and CHAT not in chats: chats.append(CHAT)
    sent=failed=0
    message=f'📰 {title}\nMənbə: {source}\n{url}'
    for chat_id in set(chats):
        with connect() as c:
            if c.execute('SELECT 1 FROM deliveries WHERE url=? AND chat_id=?',(url,chat_id)).fetchone():
                continue
        try:
            r=await client.post(f'https://api.telegram.org/bot{BOT}/sendMessage',
                json={'chat_id':chat_id,'text':message[:4000],'disable_web_page_preview':True},timeout=15)
            r.raise_for_status()
            if not r.json().get('ok'): raise ValueError('Telegram rejected message')
            with connect() as c:
                c.execute('INSERT OR IGNORE INTO deliveries(url,chat_id,sent_at) VALUES(?,?,?)',
                    (url,chat_id,datetime.now(timezone.utc).isoformat()))
            sent+=1
        except Exception as e:
            failed+=1
            print('Telegram delivery failed:',repr(e),flush=True)
    return {'sent':sent,'failed':failed}

def safe_link(link):
    try:
        p=urlparse(link)
        return p.scheme in ('http','https') and bool(p.netloc) and not p.username
    except Exception: return False

async def scan():
    if lock.locked(): return {'status':'already_running'}
    async with lock:
        with connect() as c:
            terms=[r['term'].casefold() for r in c.execute('SELECT term FROM keywords')]
        sources=json.loads(SOURCES.read_text(encoding='utf-8'))
        # Google News RSS search gives coverage beyond individual RSS endpoints.
        for query in ['DSMF', 'pensiya Azərbaycan', 'sosial müavinət Azərbaycan', 'Dövlət Sosial Müdafiə Fondu']:
            sources.append({'name':'Google News: '+query,'url':'https://news.google.com/rss/search?q='+quote(query)+'&hl=az&gl=AZ&ceid=AZ:az&when=1d'})
        added=matched=errors=sent=failed=0
        async with httpx.AsyncClient(follow_redirects=True,timeout=20,headers={'User-Agent':'NewsMonitor/1.0 (RSS reader)'}) as client:
            for source in sources:
                try:
                    url=source['url']
                    if not safe_link(url): continue
                    resp=await client.get(url)
                    resp.raise_for_status()
                    feed=feedparser.parse(resp.content)
                    if not feed.entries: raise ValueError('Empty or invalid RSS feed')
                    for item in feed.entries[:100]:
                        title=str(item.get('title','')).strip()
                        link=str(item.get('link','')).strip()
                        if not title or not safe_link(link): continue
                        if not today_published(item): continue
                        summary=str(item.get('summary',''))
                        is_match=bool(terms) and any(t in (title+' '+summary).casefold() for t in terms)
                        published=str(item.get('published',item.get('updated','')))
                        with connect() as c:
                            cur=c.execute('INSERT OR IGNORE INTO articles(url,title,source,published,discovered,matched) VALUES(?,?,?,?,?,?)',(link,title,source['name'],published,datetime.now(timezone.utc).isoformat(),int(is_match)))
                            is_new=cur.rowcount>0
                        if is_new: added+=1
                        if is_match:
                            if is_new: matched+=1
                            result=await notify(client,title,link,source['name'])
                            sent+=result['sent']
                            failed+=result['failed']
                except Exception as e:
                    errors+=1
                    print('Source error',source.get('name'),repr(e))
        return {'new':added,'matched':matched,'source_errors':errors,'telegram_sent':sent,'telegram_failed':failed,'checked_at':datetime.now(timezone.utc).isoformat()}

async def telegram_poll():
    """Long-poll Telegram; /start automatically registers the user's chat ID."""
    offset=None
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                payload={'timeout':25,'allowed_updates':['message']}
                if offset is not None: payload['offset']=offset
                response=await client.get(f'https://api.telegram.org/bot{BOT}/getUpdates',params=payload)
                response.raise_for_status()
                data=response.json()
                if not data.get('ok'):
                    print('Telegram polling rejected:',data.get('description')); await asyncio.sleep(10); continue
                for update in data.get('result',[]):
                    offset=update['update_id']+1
                    message=update.get('message') or {}
                    chat=message.get('chat') or {}
                    chat_id=chat.get('id')
                    command=str(message.get('text') or '').split(' ')[0].split('@')[0].lower()
                    if chat_id is None: continue
                    if command=='/start':
                        with connect() as c:
                            c.execute('INSERT OR IGNORE INTO subscribers(chat_id) VALUES(?)',(str(chat_id),))
                        reply='✅ DSMF xəbər monitorinqinə qoşuldunuz. Yeni uyğun xəbərlər burada görünəcək. /stop — bildirişləri dayandır.'
                    elif command=='/stop':
                        with connect() as c:
