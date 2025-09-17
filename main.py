#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, json, logging, asyncio, pathlib, requests, re
from typing import Dict, List, Set, Optional, Tuple
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import timedelta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ========= ENV / Cadence =========
TG = os.getenv("TG", "").strip()
ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
TRADE_SUMMARY_SEC = int(os.getenv("TRADE_SUMMARY_SEC", "5"))       # auto /trade tick
UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))  # 🧊 price updates every 90s
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))  # stop updates after 60 min since first detection

# ========= Filters =========
MIN_LIQ_USD      = float(os.getenv("MIN_LIQ_USD",      "35000"))
MIN_MCAP_USD     = float(os.getenv("MIN_MCAP_USD",     "70000"))
MIN_VOL_H24_USD  = float(os.getenv("MIN_VOL_H24_USD",  "40000"))
MAX_AGE_MIN      = float(os.getenv("MAX_AGE_MIN",      "120"))     # alerts allowed only if pair age < 120m
CHAIN_ID         = "solana"

# Links
AXIOM_WEB_URL    = os.getenv("AXIOM_WEB_URL") or os.getenv("AXIOME_WEB_URL") or "https://axiom.trade/meme/{pair}"
GMGN_WEB_URL     = os.getenv("GMGN_WEB_URL",  "https://gmgn.ai/sol/token/{mint}")

# Posting limits (0 = unlimited)
TOP_N_PER_TICK   = int(os.getenv("TOP_N_PER_TICK", "0"))  # 0 → unlimited
NO_MATCH_PING    = int(os.getenv("NO_MATCH_PING", "0"))

# Files
SUBS_FILE        = os.getenv("SUBS_FILE", os.path.expanduser("~/telegram-bot/subscribers.txt"))
FIRST_SEEN_FILE  = os.getenv("FIRST_SEEN_FILE", os.path.expanduser("~/telegram-bot/first_seen_caps.json"))
FALLBACK_LOGO    = os.getenv("FALLBACK_LOGO",   os.path.expanduser("~/telegram-bot/solana_fallback.png"))

# Followed-by config
MY_FOLLOWING_TXT    = os.getenv("MY_FOLLOWING_TXT", "handles.partial.txt")
TW_BEARER           = os.getenv("TW_BEARER", "").strip()
FOLLOWERS_CACHE_DIR = pathlib.Path(os.path.expanduser(os.getenv("FOLLOWERS_CACHE_DIR", "~/telegram-bot/followers_cache")))
FOLLOWERS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ========= Dexscreener endpoints =========
TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL         = "https://api.dexscreener.com/tokens/v1/{chainId}/{addresses}"
SEARCH_NEW_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:solana%20new"
SEARCH_ALL_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:solana"
TOKEN_PAIRS_URL    = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"  # used by updater & refresh

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"tg-memebot/trade-{TRADE_SUMMARY_SEC}s", "Accept": "*/*"})
HTTP_TIMEOUT = 20

# ========= SUBSCRIPTIONS =========
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists(): return set()
    try:
        return {int(x.strip()) for x in p.read_text().splitlines() if x.strip()}
    except: return set()

def _save_subs_to_file():
    pathlib.Path(SUBS_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(SUBS_FILE).write_text("\n".join(str(x) for x in sorted(SUBS)))

async def _validate_subs(bot) -> None:
    global SUBS
    bad=set()
    for cid in list(SUBS):
        try:
            await bot.get_chat(cid)
        except BadRequest as e:
            log.warning(f"Removing invalid subscriber {cid}: {getattr(e, 'message', str(e))}"); bad.add(cid)
        except Exception as e:
            log.warning(f"Subscriber check error for {cid}: {e}")
    if bad:
        SUBS -= bad
        _save_subs_to_file()

def _remove_bad_sub(cid:int):
    global SUBS
    if cid in SUBS:
        SUBS.remove(cid); _save_subs_to_file()

# ========= Utility =========
def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _pair_age_minutes(now_ms, created_ms):
    try:
        return float("inf") if not created_ms else max(0.0, (now_ms - float(created_ms)) / 60000.0)
    except: return float("inf")

def _normalize_ipfs(url: str) -> Optional[str]:
    if not url: return None
    if url.startswith("ipfs://"):
        cid = url[len("ipfs://"):].lstrip("/")
        return f"https://cloudflare-ipfs.com/ipfs/{cid}"
    return url

def _is_svg(url: str, ct: str) -> bool:
    return url.lower().endswith(".svg") or "image/svg" in (ct or "").lower()

def _fetch_image_bytes(url: str) -> Optional[bytes]:
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200: return None
        if _is_svg(url, r.headers.get("Content-Type","")): return None
        data = r.content
        return data if data and len(data) < 8*1024*1024 else None
    except Exception:
        return None

def _logo_candidates(mint: str, image_url: Optional[str]) -> List[str]:
    cands: List[str] = []
    if image_url: cands.append(_normalize_ipfs(image_url))
    if mint:
        cands.append(f"https://cdn.dexscreener.com/token-icons/solana/{mint}.png")
        cands.append(f"https://dd.dexscreener.com/ds-data/tokens/solana/{mint}.png")
    out=[]; seen=set()
    for u in cands:
        if u and u not in seen: out.append(u); seen.add(u)
    return out

def _normalize_handle(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s: return None
    if s.startswith("@"): s=s[1:]
    if s.startswith("http"):
        try:
            u=urlparse(s); parts=[p for p in (u.path or "").split("/") if p]
            if parts: s=parts[0]
        except: pass
    return s.lower()

def _canon_url(u: Optional[str]) -> Optional[str]:
    if not u: return None
    u=u.strip()
    if u.startswith("//"): u="https:" + u
    if not (u.startswith("http://") or u.startswith("https://")):
        u="https://" + u
    return u

_URL_OK = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
def _valid_url(u: Optional[str]) -> Optional[str]:
    u = _canon_url(u)
    return u if (u and _URL_OK.match(u)) else None

def _handle_from_url(u: str) -> Optional[str]:
    try:
        pu=urlparse(u); parts=[p for p in (pu.path or "").split("/") if p]
        return _normalize_handle(parts[0] if parts else "")
    except: return None

def _extract_x(info: dict) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(info, dict): return (None, None)
    for key in ("socials","links","websites"):
        arr = info.get(key)
        if isinstance(arr, list):
            for it in arr:
                if not isinstance(it, dict): continue
                url = it.get("url") or it.get("link")
                plat = (it.get("platform") or it.get("type") or it.get("label") or "").lower()
                handle = it.get("handle")
                if url and ("twitter" in url.lower() or "x.com" in url.lower() or "twitter" in plat or "x" == plat):
                    u = _canon_url(url)
                    h = _handle_from_url(u) or _normalize_handle(handle or "")
                    return (h, u)
    for key in ("twitterUrl","twitter","x","twitterHandle"):
        v = info.get(key)
        if isinstance(v, str) and v.strip():
            if v.lower().startswith("http"):
                u=_canon_url(v); return (_handle_from_url(u), u)
            h=_normalize_handle(v)
            if h: return (h, f"https://x.com/{h}")
    return (None, None)

def _get_price_usd(p: dict) -> float:
    v = p.get("priceUsd")
    if v is None and isinstance(p.get("price"), dict):
        v = p["price"].get("usd")
    try:
        return float(v) if v is not None else 0.0
    except:
        return 0.0

# ========= Dexscreener fetch =========
def _get_json(url, timeout=HTTP_TIMEOUT, tries=2):
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200: return r.json()
        except Exception: pass
        time.sleep(0.2*(i+1))
    return None

def fetch_latest_solana_token_addresses(limit=1000) -> List[str]:
    j = _get_json(TOKEN_PROFILES_URL, timeout=15) or []
    addrs=[]
    for it in j:
        if isinstance(it, dict) and it.get("chainId") == "solana":
            a = it.get("tokenAddress")
            if a: addrs.append(a)
        if len(addrs) >= limit: break
    return addrs

def fetch_pairs_for_addresses(chain_id, addrs) -> List[dict]:
    out=[]
    for i in range(0,len(addrs),30):
        chunk=",".join(addrs[i:i+30])
        url=TOKENS_URL.format(chainId=chain_id, addresses=chunk)
        data=_get_json(url, timeout=20) or []
        if isinstance(data,list): out.extend(data)
    return out

def fetch_search(url: str) -> List[dict]:
    j=_get_json(url, timeout=15) or {}
    return j.get("pairs",[]) if isinstance(j,dict) else []

def _choose_best_pair(p_list: List[dict]) -> Optional[dict]:
    best = None
    best_key = None
    for p in p_list or []:
        if (p.get("chainId") or "").lower() != CHAIN_ID:
            continue
        liq = float((p.get("liquidity") or {}).get("usd",0) or 0)
        vol = float((p.get("volume") or {}).get("h24",0) or 0)
        created = float(p.get("pairCreatedAt") or 0)
        key = (liq, created, vol)
        if (best is None) or (key > best_key):
            best, best_key = p, key
    return best

def _current_for_token(token_addr: str) -> Optional[dict]:
    """Dexscreener refresh to ensure post-migration link/price."""
    if not token_addr:
        return None
    url = TOKEN_PAIRS_URL.format(address=token_addr)
    data = _get_json(url, timeout=15) or []
    if not isinstance(data, list) or not data:
        return None
    best = _choose_best_pair(data) or data[0]

    base=best.get("baseToken",{}) or {}
    info=best.get("info",{}) or {}
    fdv = best.get("fdv")
    price = _get_price_usd(best)
    x_handle, x_url = _extract_x(info)
    pair_addr = best.get("pairAddress") or ""

    return {
        "name": base.get("symbol") or base.get("name") or "Unknown",
        "token": base.get("address") or token_addr,
        "pair": pair_addr,
        "price_usd": float(price or 0),
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd",0) or 0),
        "mcap_usd": float(fdv if fdv is not None else (best.get("marketCap") or 0) or 0),
        "vol24_usd": float((best.get("volume") or {}).get("h24",0) or 0),
        "age_min": _pair_age_minutes(time.time()*1000.0, best.get("pairCreatedAt")),
        "url": _valid_url(best.get("url") or f"https://dexscreener.com/{CHAIN_ID}/{pair_addr}"),
        "logo_hint": info.get("imageUrl") or base.get("logo") or "",
        "axiom": AXIOM_WEB_URL.format(pair=pair_addr) if pair_addr else "https://axiom.trade/",
        "gmgn": GMGN_WEB_URL.format(mint=base.get("address","")) if base.get("address") else "https://gmgn.ai/",
        "tw_url": _valid_url(x_url) or "https://x.com/",
        "tw_handle": x_handle,
    }

# ---------- Enrichment helpers ----------
def _enrich_if_needed(m: dict) -> dict:
    """If age/mcap/vol are missing/zero, fetch live pair and merge into m."""
    need_age = (m.get("age_min") in (None, float("inf")))
    need_mcap = float(m.get("mcap_usd") or 0) <= 0
    need_vol = float(m.get("vol24_usd") or 0) <= 0
    if not (need_age or need_mcap or need_vol):
        return m
    cur = _current_for_token(m.get("token"))
    if not cur:
        return m
    for k in ("pair","price_usd","liquidity_usd","mcap_usd","vol24_usd","age_min","url","logo_hint","tw_url","tw_handle"):
        if cur.get(k) is not None:
            m[k] = cur[k]
    return m

def passes_filters(p, now_ms):
    """Soft filter: keep liq strict; age/mcap/vol pass when missing (handled by enrichment later)."""
    try:
        if (p.get("chainId") or "").lower() != CHAIN_ID: return False
        liq=float((p.get("liquidity") or {}).get("usd",0) or 0)

        fdv=p.get("fdv"); mcap=float(fdv if fdv is not None else (p.get("marketCap") or 0) or 0)
        vol24=float((p.get("volume") or {}).get("h24",0) or 0)
        age=_pair_age_minutes(now_ms, p.get("pairCreatedAt"))

        liq_ok  = liq >= MIN_LIQ_USD
        age_ok  = (age <= MAX_AGE_MIN) if age != float("inf") else True
        mcap_ok = (mcap >= MIN_MCAP_USD) if mcap > 0 else True
        vol_ok  = (vol24 >= MIN_VOL_H24_USD) if vol24 > 0 else True
        return liq_ok and age_ok and mcap_ok and vol_ok
    except:
        return False

def fetch_matches() -> List[dict]:
    addrs = fetch_latest_solana_token_addresses(limit=1000)
    pairs  = fetch_pairs_for_addresses(CHAIN_ID, addrs) if addrs else []
    pairs += fetch_search(SEARCH_NEW_URL)
    pairs += fetch_search(SEARCH_ALL_URL)

    seen_pairs=set(); uniq=[]
    for p in pairs:
        if not isinstance(p,dict): continue
        k = p.get("pairAddress") or p.get("url")
        if not k or k in seen_pairs: continue
        seen_pairs.add(k); uniq.append(p)

    now_ms=time.time()*1000.0
    matches=[]
    for p in uniq:
        if not passes_filters(p, now_ms):
            # Build minimal 'm' and attempt enrichment (cases with missing age/mcap/vol).
            base_tmp = (p.get("baseToken") or {})
            mint_tmp = base_tmp.get("address") or ""
            m_try = {
                "name": base_tmp.get("symbol") or base_tmp.get("name") or "Unknown",
                "token": mint_tmp,
                "pair": p.get("pairAddress") or "",
                "price_usd": _get_price_usd(p),
                "liquidity_usd": float((p.get("liquidity") or {}).get("usd",0) or 0),
                "mcap_usd": float((p.get("fdv") if p.get("fdv") is not None else (p.get("marketCap") or 0)) or 0),
                "vol24_usd": float((p.get("volume") or {}).get("h24",0) or 0),
                "age_min": _pair_age_minutes(now_ms, p.get("pairCreatedAt")),
                "url": _valid_url(p.get("url") or ""),
                "logo_hint": (p.get("info") or {}).get("imageUrl") or base_tmp.get("logo") or "",
                "tw_handle": None, "tw_url": None,
                "axiom": AXIOM_WEB_URL.format(pair=p.get("pairAddress") or "") if p.get("pairAddress") else "https://axiom.trade/",
                "gmgn": GMGN_WEB_URL.format(mint=mint_tmp) if mint_tmp else "https://gmgn.ai/",
            }
            m_try = _enrich_if_needed(m_try)
            # Re-check with hard filter on enriched fields
            if float(m_try["liquidity_usd"]) < MIN_LIQ_USD: 
                continue
            if (m_try.get("age_min") not in (None, float("inf"))) and (m_try["age_min"] > MAX_AGE_MIN):
                continue
            if m_try["mcap_usd"] > 0 and m_try["mcap_usd"] < MIN_MCAP_USD:
                continue
            if m_try["vol24_usd"] > 0 and m_try["vol24_usd"] < MIN_VOL_H24_USD:
                continue
            # Accept enriched candidate
            matches.append(m_try)
            continue

        # Normal assembly path
        base=p.get("baseToken",{}) or {}
        info=p.get("info",{}) or {}
        name  = base.get("symbol") or base.get("name") or "Unknown"
        token = base.get("address") or "Unknown"
        pair  = p.get("pairAddress") or ""
        liq   = float((p.get("liquidity") or {}).get("usd",0) or 0)
        fdv   = p.get("fdv")
        mcap  = float(fdv if fdv is not None else (p.get("marketCap") or 0) or 0)
        vol24 = float((p.get("volume") or {}).get("h24",0) or 0)
        url   = _valid_url(p.get("url") or f"https://dexscreener.com/{CHAIN_ID}/{pair}")
        age_m = _pair_age_minutes(now_ms, p.get("pairCreatedAt"))
        price = _get_price_usd(p)
        logo_hint = info.get("imageUrl") or base.get("logo") or ""
        x_handle, x_url = _extract_x(info)
        m = {
            "name": name, "token": token, "pair": pair,
            "price_usd": price,
            "liquidity_usd": liq, "mcap_usd": mcap, "vol24_usd": vol24,
            "age_min": age_m, "url": url,
            "axiom": AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/",
            "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
            "tw_url": _valid_url(x_url) or "https://x.com/",
            "tw_handle": x_handle,
            "logo_hint": logo_hint,
        }
        # Final enrichment if needed
        m = _enrich_if_needed(m)
        matches.append(m)

    # best per mint by 24h vol
    best={}
    for m in matches:
        if m["token"] not in best or m["vol24_usd"]>best[m["token"]]["vol24_usd"]:
            best[m["token"]]=m
    return list(best.values())

# ========= First-seen & tracking =========
def _load_first_seen():
    p=pathlib.Path(FIRST_SEEN_FILE)
    if p.exists():
        try: return json.loads(p.read_text())
        except: return {}
    return {}
def _save_first_seen(d):
    pathlib.Path(FIRST_SEEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(FIRST_SEEN_FILE).write_text(json.dumps(d, indent=2))

FIRST_SEEN = _load_first_seen()
TRACKED: Set[str] = set()  # tokens to generate price updates for
# Remember the very first pinned message per (chat, token)
LAST_PINNED: Dict[Tuple[int, str], int] = {}

def decorate_with_first_seen(pairs):
    changed = False
    now_ts = int(time.time())
    for m in pairs:
        tok = m.get("token") or ""
        cur = float(m.get("mcap_usd") or 0)
        rec = FIRST_SEEN.get(tok)

        is_new = rec is None
        if is_new:
            FIRST_SEEN[tok] = {"first": (cur if cur > 0 else 0.0), "ts": now_ts}
            changed = True
        else:
            if rec.get("first", 0) == 0 and cur > 0:
                rec["first"] = cur
                changed = True

        m["is_first_time"] = is_new
        m["first_mcap_usd"] = float(FIRST_SEEN.get(tok, {}).get("first", 0))
    if changed:
        _save_first_seen(FIRST_SEEN)

# ========= Followed-by (X/Twitter v2 + cache) =========
def load_my_following() -> Set[str]:
    p = pathlib.Path(MY_FOLLOWING_TXT)
    if not p.exists(): return set()
    out:set[str]=set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            h = _normalize_handle(line)
            if h: out.add(h)
    except: pass
    return out

MY_HANDLES: Set[str] = load_my_following()

def _followers_cache_path(handle:str) -> pathlib.Path:
    return FOLLOWERS_CACHE_DIR / f"{handle.lower()}.json"
def _followers_cache_load(handle:str) -> Optional[Set[str]]:
    p = _followers_cache_path(handle)
    if not p.exists(): return None
    try:
        j=json.loads(p.read_text(encoding="utf-8"))
        return set(j.get("followers",[]))
    except: return None
def _followers_cache_save(handle:str, followers:Set[str]) -> None:
    p = _followers_cache_path(handle)
    p.write_text(json.dumps({"followers": sorted(followers)}, ensure_ascii=False, indent=2), encoding="utf-8")
def _tw_api_get(url:str, params:Dict[str,str]) -> Optional[dict]:
    if not TW_BEARER: return None
    try:
        r = SESSION.get(url, params=params, headers={"Authorization": f"Bearer {TW_BEARER}"}, timeout=20)
        if r.status_code==200: return r.json()
    except: pass
    return None
def fetch_followers_v2(handle:str, max_total:int=1000) -> Optional[Set[str]]:
    if not TW_BEARER or not handle: return None
    cached = _followers_cache_load(handle)
    if cached: return cached
    j=_tw_api_get(f"https://api.twitter.com/2/users/by/username/{handle}", {"user.fields":"id"})
    if not j or "data" not in j: return None
    uid=j["data"]["id"]
    out:set[str]=set()
    url=f"https://api.twitter.com/2/users/{uid}/followers"
    params={"max_results":"1000","user.fields":"username"}
    next_token=None
    tries=0
    while len(out)<max_total and tries<5:
        tries+=1
        if next_token: params["pagination_token"]=next_token
        j=_tw_api_get(url, params)
        if not j or "data" not in j: break
        for u in j["data"]:
            h=_normalize_handle(u.get("username",""))
            if h: out.add(h)
        next_token=j.get("meta",{}).get("next_token")
        if not next_token: break
    if out: _followers_cache_save(handle, out)
    return out if out else None
def overlap_line(tw_handle: Optional[str]) -> str:
    if not tw_handle or not MY_HANDLES: return "—"
    followers = fetch_followers_v2(tw_handle, max_total=1000)
    if not followers: return "—"
    overlap = sorted(MY_HANDLES & followers)
    if not overlap: return "—"
    acc = []; total = 0
    for h in overlap:
        piece = "@" + h + ", "
        if total + len(piece) > 180: break
        acc.append(piece); total += len(piece)
    s = "".join(acc).rstrip(", ")
    return s + (" , …" if len(overlap) > len(acc) else "")

# ========= UI helpers =========
def link_keyboard(m: dict) -> InlineKeyboardMarkup:
    pair = m.get("pair") or ""
    mint = m.get("token") or ""
    ds_url = m.get("url") or (f"https://dexscreener.com/{CHAIN_ID}/{pair}" if pair else "https://dexscreener.com/")
    ax_url = m.get("axiom") or (AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/")
    gm_url = m.get("gmgn") or (GMGN_WEB_URL.format(mint=mint) if mint else "https://gmgn.ai/")
    x_url  = m.get("tw_url") or "https://x.com/"

    def _norm(u: str) -> str:
        u = (u or "").strip()
        if u.startswith("//"): u = "https:" + u
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://" + u
        return u

    ds_url = _norm(ds_url)
    ax_url = _norm(ax_url)
    gm_url = _norm(gm_url)
    x_url  = _norm(x_url)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=ds_url),
         InlineKeyboardButton("Axiom",       url=ax_url)],
        [InlineKeyboardButton("GMGN",        url=gm_url),
         InlineKeyboardButton("X",           url=x_url)],
    ])

def _pct_str(first: float, cur: float) -> str:
    if first > 0 and cur >= 0:
        d = (cur - first) / first * 100.0
        return f"{'+' if d>=0 else ''}{d:.1f}%"
    return "n/a"

def build_caption(m: dict, fb_text:str, is_update: bool) -> str:
    BLUE, BANK, XEMO = "🔵","🏦","𝕏"
    fire_or_ice = "🧊" if is_update else ("🔥" if m.get("is_first_time") else "🧊")
    first = float(m.get("first_mcap_usd") or 0)
    cur   = float(m.get("mcap_usd") or 0)
    pct   = _pct_str(first, cur)
    circle= "🟢" if (first>0 and cur>=first) else "🔴"
    price = float(m.get("price_usd") or 0)

    header = f"{fire_or_ice} <b>{html_escape(m['name'])}</b>"
    price_line = f"💵 <b>Price:</b> " + (f"${price:.8f}" if price < 1 else f"${price:,.4f}")

    return (
        f"{header}\n"
        f"{BANK} <b>First Mcap:</b> {BLUE} ${first:,.0f}\n"
        f"{BANK} <b>Current Mcap:</b> {circle} ${cur:,.0f} <b>({pct})</b>\n"
        f"🖨️ <b>Mint:</b> <code>{html_escape(m['token'])}</code>\n"
        f"🔗 <b>Pair:</b> <code>{html_escape(m['pair'])}</code>\n"
        f"💧 <b>Liquidity:</b> ${m['liquidity_usd']:,.0f}\n"
        f"{price_line}\n"
        f"📈 <b>Vol 24h:</b> ${m['vol24_usd']:,.0f}\n"
        f"⏱️ <b>Age:</b> {int(m['age_min'])} min\n"
        f"{XEMO} <b>Followed by:</b> {html_escape(fb_text)}"
    )

# ========= Telegram send helper =========
def _is_keyboard_reject(e: Exception) -> bool:
    s = str(e).lower()
    return ("reply markup is not allowed" in s) or ("keyboardbuttonpolltype" in s) or ("polls are unallowed" in s)

def _merge_current(into: dict, cur: dict) -> dict:
    if not into or not cur: return into
    for k in ("pair","price_usd","liquidity_usd","mcap_usd","vol24_usd","age_min","url","logo_hint","tw_url","tw_handle"):
        v = cur.get(k)
        if v is not None:
            into[k] = v
    return into

async def _send_or_photo(bot, chat_id:int, caption:str, kb, token:str, logo_hint:str, pin:bool=False) -> Optional[int]:
    cap = caption if len(caption) <= 900 else (caption[:870] + " …")

    async def _maybe_pin(msg_id: int):
        if not pin: return
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=msg_id, disable_notification=True)
        except Exception as e:
            log.info(f"Pin attempt failed (non-fatal): {e}")

    # try sending photo with keyboard; on true keyboard rejection, retry without
    for u in _logo_candidates(token, logo_hint):
        if not u: continue
        try:
            msg = await bot.send_photo(chat_id=chat_id, photo=u, caption=cap, parse_mode="HTML", reply_markup=kb)
            await _maybe_pin(msg.message_id); return msg.message_id
        except BadRequest as e:
            if _is_keyboard_reject(e):
                try:
                    msg = await bot.send_photo(chat_id=chat_id, photo=u, caption=cap, parse_mode="HTML")
                    await _maybe_pin(msg.message_id); return msg.message_id
                except Exception as e2:
                    log.error(f"retry send_photo no-kb failed: {e2}")
        except Exception as e:
            log.warning(f"send_photo error: {e}")

        data=_fetch_image_bytes(u)
        if data:
            try:
                msg = await bot.send_photo(chat_id=chat_id, photo=data, caption=cap, parse_mode="HTML", reply_markup=kb)
                await _maybe_pin(msg.message_id); return msg.message_id
            except BadRequest as e:
                if _is_keyboard_reject(e):
                    try:
                        msg = await bot.send_photo(chat_id=chat_id, photo=data, caption=cap, parse_mode="HTML")
                        await _maybe_pin(msg.message_id); return msg.message_id
                    except Exception as e2:
                        log.error(f"retry send_photo bytes no-kb failed: {e2}")
            except Exception as e:
                log.warning(f"send_photo bytes error: {e}")

    # local fallback image
    if os.path.exists(FALLBACK_LOGO):
        try:
            with open(FALLBACK_LOGO,"rb") as f:
                msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=cap, parse_mode="HTML", reply_markup=kb)
                await _maybe_pin(msg.message_id); return msg.message_id
        except BadRequest as e:
            if _is_keyboard_reject(e):
                try:
                    with open(FALLBACK_LOGO,"rb") as f:
                        msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=cap, parse_mode="HTML")
                        await _maybe_pin(msg.message_id); return msg.message_id
                except Exception as e2:
                    log.error(f"retry send_photo fallback no-kb failed: {e2}")
        except Exception as e:
            log.warning(f"send_photo fallback error: {e}")

    # text fallback
    try:
        msg = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML",
                                     disable_web_page_preview=True, reply_markup=kb)
        await _maybe_pin(msg.message_id); return msg.message_id
    except BadRequest as e:
        if _is_keyboard_reject(e):
            try:
                msg = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML",
                                             disable_web_page_preview=True)
                await _maybe_pin(msg.message_id); return msg.message_id
            except Exception as e2:
                log.error(f"retry send_message no-kb failed: {e2}")
        try:
            msg = await bot.send_message(chat_id=chat_id, text=caption, disable_web_page_preview=True)
            await _maybe_pin(msg.message_id); return msg.message_id
        except BadRequest as e2:
            if "chat not found" in str(e2).lower():
                _remove_bad_sub(chat_id)
        except Exception as e2:
            log.warning(f"send_message plain error: {e2}")

    return None

# ========= Message wrappers =========
async def send_new_token(bot, chat_id:int, m:dict):
    # pre-send refresh to avoid pre-/post-migration mismatch
    try:
        cur = _current_for_token(m.get("token"))
        if cur:
            m = _merge_current(m, cur)
    except Exception as e:
        log.info(f"refresh before 🔥 send failed (non-fatal): {e}")

    fb_text = overlap_line(m.get("tw_handle"))
    caption = build_caption(m, fb_text, is_update=False)
    kb = link_keyboard(m)

    # Pin only the first time we ever pin this token in this chat
    key = (chat_id, m.get("token") or "")
    should_pin = key not in LAST_PINNED

    msg_id = await _send_or_photo(
        bot, chat_id, caption, kb,
        token=m.get("token"), logo_hint=m.get("logo_hint"), pin=should_pin
    )

    if should_pin and msg_id:
        LAST_PINNED[key] = msg_id


async def send_price_update(bot, chat_id:int, m:dict):
    fb_text = "—"
    caption = build_caption(m, fb_text, is_update=True)
    kb = link_keyboard(m)
    await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"), pin=False)

# ========= /trade ENGINE =========
def best_per_token(pairs):
    best={}
    for m in pairs:
        if m["token"] not in best or m["vol24_usd"]>best[m["token"]]["vol24_usd"]:
            best[m["token"]] = m
    return list(best.values())

async def do_trade_push(bot):
    try:
        pairs = best_per_token(fetch_matches())
        decorate_with_first_seen(pairs)
        if not pairs and NO_MATCH_PING:
            for chat_id in list(SUBS):
                await bot.send_message(chat_id=chat_id, text="(auto /trade) no matches right now.", disable_web_page_preview=True)
            return

        for chat_id in list(SUBS):
            sent = 0
            for m in pairs:
                if TOP_N_PER_TICK > 0 and sent >= TOP_N_PER_TICK:
                    break
                if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
                    continue

                # track first, then decide whether to pin
                already_tracked = m["token"] in TRACKED
                TRACKED.add(m["token"])

                # 🔥 pin if first time OR not yet tracked this run
                if m.get("is_first_time") or not already_tracked:
                    await send_new_token(bot, chat_id, m)  # 🔥 + pin
                    sent += 1

                await asyncio.sleep(0.05)

    except Exception as e:
        log.exception(f"do_trade_push error: {e}")


# ========= Jobs =========
async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🔥 [tick] auto_trade fired (interval={TRADE_SUMMARY_SEC}s)")
    await do_trade_push(context.bot)


async def updater(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🧊 [tick] updater fired (interval={UPDATE_INTERVAL_SEC}s)")
    """Send updates every UPDATE_INTERVAL_SEC; stop after UPDATE_MAX_DURATION_MIN since first detection."""
    try:
        if not TRACKED:
            return
        now_ts = int(time.time())
        log.info(f"[updater] refreshing {len(TRACKED)} tracked tokens")

        for token in list(TRACKED):
            first_rec = FIRST_SEEN.get(token) or {}
            first_ts = int(first_rec.get("ts", now_ts))
            if now_ts - first_ts >= UPDATE_MAX_DURATION_MIN * 60:
                TRACKED.discard(token)
                continue

            current = _current_for_token(token)
            if not current:
                continue

            if float(current.get("age_min", 1e9)) >= MAX_AGE_MIN:
                TRACKED.discard(token)
                continue

            first_mcap = float(first_rec.get("first", 0.0))
            current["first_mcap_usd"] = first_mcap
            current["is_first_time"]  = False

            for chat_id in list(SUBS):
                await send_price_update(context.bot, chat_id, current)
                await asyncio.sleep(0.02)
    except Exception as e:
        log.exception(f"updater job error: {e}")


# ========= Commands =========
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id); _save_subs_to_file()
    await u.message.reply_text(
        f"✅ Subscribed. 🔥 /trade every {TRADE_SUMMARY_SEC}s + 🧊 updates every {UPDATE_INTERVAL_SEC}s (stop after {UPDATE_MAX_DURATION_MIN} min)."
    )


async def cmd_id(u:Update,c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(str(u.effective_chat.id))

async def cmd_sub(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id); _save_subs_to_file()
    await u.message.reply_text("✅ Subscribed.")

async def cmd_unsub(u:Update,c:ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.discard(u.effective_chat.id); _save_subs_to_file()
    await u.message.reply_text("❎ Unsubscribed.")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"Subscribers: {len(SUBS)} | 🔥 /trade every {TRADE_SUMMARY_SEC}s | 🧊 updates every {UPDATE_INTERVAL_SEC}s | "
        f"Top/tick {TOP_N_PER_TICK or 'unlimited'} | Max alert age {int(MAX_AGE_MIN)}m | Update stop {UPDATE_MAX_DURATION_MIN}m"
    )


async def cmd_trade(u:Update,c:ContextTypes.DEFAULT_TYPE):
    pairs = best_per_token(fetch_matches()); decorate_with_first_seen(pairs)
    sent = 0
    for m in pairs:
        if TOP_N_PER_TICK > 0 and sent >= TOP_N_PER_TICK: break
        if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
            continue
        TRACKED.add(m["token"])
        if m.get("is_first_time"):
            await send_new_token(c.bot, u.effective_chat.id, m)  # 🔥 + pin
            sent += 1
        await asyncio.sleep(0.05)
async def cmd_repin(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Re-post currently tracked tokens as 'first-time' so they pin with 🔥.
    If nothing is tracked yet, tell the user.
    """
    if not TRACKED:
        await u.message.reply_text("Nothing to repin yet — no tracked tokens.")
        return

    sent = 0
    for token in list(TRACKED):
        cur = _current_for_token(token)
        if not cur:
            continue
        # Force fire emoji & treat as first-time so the message gets pinned with 🔥
        cur["is_first_time"] = True
        await send_new_token(c.bot, u.effective_chat.id, cur)
        sent += 1
        await asyncio.sleep(0.2)

    if sent == 0:
        await u.message.reply_text("Tried to repin, but couldn’t refresh any tokens.")

# ========= Main =========
async def _post_init(app: Application):
    global SUBS
    SUBS = _load_subs_from_file()
    if ALERT_CHAT_ID:
        SUBS.add(ALERT_CHAT_ID); _save_subs_to_file()
    await _validate_subs(app.bot)
    log.info(f"Subscribers: {sorted(SUBS)}")

# ========= Webhook server for Cloud Run =========
from fastapi import FastAPI, Request

if not TG:
    raise SystemExit("Missing TG token")

application = Application.builder().token(TG).post_init(_post_init).build()

application.add_handler(CommandHandler("start",      cmd_start))
application.add_handler(CommandHandler("id",         cmd_id))
application.add_handler(CommandHandler("subscribe",  cmd_sub))
application.add_handler(CommandHandler("unsubscribe",cmd_unsub))
application.add_handler(CommandHandler("status",     cmd_status))
application.add_handler(CommandHandler("trade",      cmd_trade))
application.add_handler(CommandHandler("repin",      cmd_repin))

app = FastAPI()

@app.get("/")
async def health():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    # Ensure subscriptions + first_seen cache are loaded at startup
    global SUBS, FIRST_SEEN
    SUBS = _load_subs_from_file()
    FIRST_SEEN = _load_first_seen()

    await application.initialize()

    jq = application.job_queue
    # Use timedelta to ensure these are seconds, not minutes.
    jq.run_repeating(
        auto_trade,
        interval=timedelta(seconds=TRADE_SUMMARY_SEC),
        first=timedelta(seconds=3),
        name="trade_tick",
    )
    jq.run_repeating(
        updater,
        interval=timedelta(seconds=UPDATE_INTERVAL_SEC),
        first=timedelta(seconds=20),
        name="updates",
    )


    await application.start()
    log.info(f"Bot started with TRADE_SUMMARY_SEC={TRADE_SUMMARY_SEC}, UPDATE_INTERVAL_SEC={UPDATE_INTERVAL_SEC}")

@app.on_event("shutdown")
async def _shutdown():
    await application.stop()
    await application.shutdown()

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TG:
        return {"ok": False, "reason": "token mismatch"}
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

