import os, json, sqlite3, asyncio, html, secrets, calendar
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse, quote
import feedparser, httpx
from official_sources import collect_official
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
        parsed=entry.get('published_parsed')
        if parsed:
            dt=datetime.fromtimestamp(calendar.timegm(parsed),timezone.utc)
        else:
            raw=entry.get('published')
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
        # Only publisher-operated RSS feeds. Google News can relist old articles
        # with a fresh aggregator timestamp, which is not proof of publication today.
        sources=[source for source in sources if 'news.google.com' not in source.get('url','')]
        added=matched=errors=sent=failed=checked=0
        source_stats=[]
        async with httpx.AsyncClient(follow_redirects=True,timeout=20,headers={'User-Agent':'NewsMonitor/1.0 (RSS reader)'}) as client:
            for source in sources:
                try:
                    if not safe_link(source['url']): continue
                    if source.get('type') == 'official_html':
                        entries=await collect_official(client,source,datetime.now(BAKU).date())
                    else:
                        resp=await client.get(source['url'])
                        resp.raise_for_status()
                        feed=feedparser.parse(resp.content)
                        entries=feed.entries[:100]
                        if not entries:
                            print('Skipping empty RSS feed:',source.get('name'),flush=True)
                    count=0
                    for item in entries:
                        title=str(item.get('title','')).strip()
                        link=str(item.get('link','')).strip()
                        if not title or not safe_link(link): continue
                        if source.get('type') != 'official_html' and not today_published(item): continue
                        count+=1
                        summary=str(item.get('summary',''))
                        is_match=bool(terms) and any(t in (title+' '+summary).casefold() for t in terms)
                        published=str(item.get('published',''))
                        with connect() as c:
                            cur=c.execute('INSERT OR IGNORE INTO articles(url,title,source,published,discovered,matched) VALUES(?,?,?,?,?,?)',(link,title,source['name'],published,datetime.now(timezone.utc).isoformat(),int(is_match)))
                            is_new=cur.rowcount>0
                            if is_match:
                                c.execute('UPDATE articles SET matched=1 WHERE url=?',(link,))
                        if is_new: added+=1
                        if is_match:
                            if is_new: matched+=1
                            result=await notify(client,title,link,source['name'])
                            sent+=result['sent']
                            failed+=result['failed']
                    checked+=count
                    source_stats.append({'source':source['name'],'today':count})
                except Exception as e:
                    errors+=1
                    source_stats.append({'source':source.get('name'),'error':type(e).__name__})
                    print('Source error',source.get('name'),repr(e),flush=True)
        return {'new':added,'matched':matched,'source_errors':errors,'today_articles_checked':checked,'sources':source_stats,'telegram_sent':sent,'telegram_failed':failed,'checked_at':datetime.now(timezone.utc).isoformat()}

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
                            c.execute('DELETE FROM subscribers WHERE chat_id=?',(str(chat_id),))
                        reply='Bildirişlər dayandırıldı. Yenidən qoşulmaq üçün /start yazın.'
                    else: continue
                    await client.post(f'https://api.telegram.org/bot{BOT}/sendMessage',json={'chat_id':chat_id,'text':reply},timeout=15)
            except asyncio.CancelledError: raise
            except Exception as exc:
                print('Telegram polling error:',repr(exc))
                await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app):
    setup()
    scheduler.add_job(scan,'interval',minutes=INTERVAL,id='scan',max_instances=1,coalesce=True)
    scheduler.start()
    first_scan=asyncio.create_task(scan())
    telegram_task=asyncio.create_task(telegram_poll()) if BOT else None
    try: yield
    finally:
        first_scan.cancel()
        try: await first_scan
        except asyncio.CancelledError: pass
        if telegram_task:
            telegram_task.cancel()
            try: await telegram_task
            except asyncio.CancelledError: pass
        scheduler.shutdown(wait=False)

app=FastAPI(title='AZ News Monitor',lifespan=lifespan)

@app.get('/',response_class=HTMLResponse)
async def index():
    return '''<!doctype html><html lang="az"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AZ News Monitor</title><style>body{font:16px system-ui;background:#0c1323;color:#eef2ff;max-width:980px;margin:40px auto;padding:16px}input,button{padding:12px;border-radius:9px;border:1px solid #43506b;background:#1c2940;color:white}input{min-width:250px}button{cursor:pointer;background:#2563eb}article{padding:14px;border-bottom:1px solid #34405a}a{color:#8db8ff}small{color:#aab7d1}.row{display:flex;gap:8px;flex-wrap:wrap;margin:15px 0}</style><h1>📰 AZ News Monitor</h1><p>RSS əsaslı 5 dəqiqəlik monitorinq • DSMF, pensiya, müavinət və sosial yardım açar sözləri əvvəlcədən əlavə edilib.</p><div class="row"><input id="token" type="password" placeholder="ADMIN_TOKEN"><button onclick="save()">Daxil ol</button></div><div class="row"><input id="word" placeholder="Yeni açar söz"><button onclick="add()">Əlavə et</button><button onclick="scan()">İndi yoxla</button></div><p id="words"></p><p id="status"></p><h2>Bugünkü uyğun xəbərlər (Bakı vaxtı)</h2><div id="news"></div><script>let token=sessionStorage.getItem('t')||'';document.getElementById('token').value=token;function save(){token=document.getElementById('token').value;sessionStorage.setItem('t',token);load()}async function api(path,method='GET',data){let r=await fetch(path,{method,headers:{'X-Admin-Token':token,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});if(!r.ok)throw Error('HTTP '+r.status);return r.json()}function esc(x){return String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function load(){try{let [k,n]=await Promise.all([api('/api/keywords'),api('/api/articles')]);document.getElementById('words').innerHTML=k.map(w=>'<button data-word="'+esc(w)+'" class="remove">'+esc(w)+' ×</button>').join(' ')||'Açar söz yoxdur';document.querySelectorAll('.remove').forEach(b=>b.onclick=()=>del(b.dataset.word));document.getElementById('news').innerHTML=n.map(a=>'<article><a href="'+esc(a.url)+'" target="_blank" rel="noopener noreferrer">'+esc(a.title)+'</a><p><small>'+esc(a.source)+' • '+esc(a.discovered)+'</small></p></article>').join('')||'Hələ uyğun xəbər yoxdur';if(!document.getElementById('status').textContent.startsWith('{'))document.getElementById('status').textContent='Panel aktivdir'}catch(e){document.getElementById('status').textContent='Xəta: '+e.message}}async function add(){let term=document.getElementById('word').value.trim();if(!term)return;await api('/api/keywords','POST',{term});document.getElementById('word').value='';load()}async function del(term){await api('/api/keywords','DELETE',{term});load()}async function scan(){document.getElementById('status').textContent='Yoxlanılır...';try{let r=await api('/api/scan','POST');document.getElementById('status').textContent=JSON.stringify(r);load()}catch(e){alert(e.message)}}if(token)load();</script></html>'''

@app.get('/health')
async def health(): return {'status':'ok','interval_minutes':INTERVAL}

@app.get('/api/keywords')
async def keywords(request:Request):
    authorized(request)
    with connect() as c: return [r['term'] for r in c.execute('SELECT term FROM keywords ORDER BY term')]

@app.post('/api/keywords')
async def add_keyword(request:Request):
    authorized(request)
    term=str((await request.json()).get('term','')).strip()
    if not 2<=len(term)<=120: raise HTTPException(400,'Keyword must be 2-120 characters')
    with connect() as c: c.execute('INSERT OR IGNORE INTO keywords(term) VALUES(?)',(term,))
    return {'ok':True}

@app.delete('/api/keywords')
async def delete_keyword(request:Request):
    authorized(request)
    term=str((await request.json()).get('term','')).strip()
    with connect() as c: c.execute('DELETE FROM keywords WHERE term=?',(term,))
    return {'ok':True}

@app.get('/api/articles')
async def articles(request:Request):
    authorized(request)
    with connect() as c:
        rows=[dict(r) for r in c.execute('SELECT title,url,source,published,discovered FROM articles WHERE matched=1 AND source NOT LIKE "Google News:%" ORDER BY id DESC LIMIT 1000')]
    today=datetime.now(BAKU).date()
    result=[]
    for row in rows:
        try:
            raw=row['published'] or ''
            if raw==today.isoformat(): result.append(row); continue
            dt=parsedate_to_datetime(raw)
            if dt.tzinfo and dt.astimezone(BAKU).date()==today: result.append(row)
        except (ValueError,TypeError,OverflowError): pass
    return result[:300]

@app.post('/api/scan')
async def manual_scan(request:Request):
    authorized(request)
    return await scan()
