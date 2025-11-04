#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This file is generated as a full-length (>1000 lines) main.py implementing:
- Dexscreener mirror-style ingester
- Telegram bot (python-telegram-bot v20)
- FastAPI webhook
- Separation of detection (ingester) vs alerting (filters)
"""

from __future__ import annotations

import os, sys, re, json, time, asyncio, logging, pathlib
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import pandas as pd
from bs4 import BeautifulSoup

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
log = logging.getLogger("bot")
log.info(f"Python runtime: {sys.version}")

# -----------------------------------------------------------------------------
# Env & Config
# -----------------------------------------------------------------------------
TG = os.getenv("TG", "").strip()
if not TG:
    raise SystemExit("Missing TG token (env TG)")

ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
TRADE_SUMMARY_SEC       = int(os.getenv("TRADE_SUMMARY_SEC", "5"))
UPDATE_INTERVAL_SEC     = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
INGEST_INTERVAL_SEC     = int(os.getenv("INGEST_INTERVAL_SEC", "12"))  # NEW

DEBUG_FB = os.getenv("DEBUG_FB", "0") == "1"

MIN_LIQ_USD     = float(os.getenv("MIN_LIQ_USD",     "35000"))
MIN_MCAP_USD    = float(os.getenv("MIN_MCAP_USD",    "70000"))
MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
MAX_AGE_MIN     = float(os.getenv("MAX_AGE_MIN",     "120"))
CHAIN_ID        = os.getenv("CHAIN_ID", "solana").lower()

AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL") or os.getenv("AXIOME_WEB_URL") or "https://axiom.trade/meme/{pair}"
GMGN_WEB_URL  = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
DEXSCREENER_PAIR_URL  = os.getenv("DEXSCREENER_PAIR_URL",  "https://dexscreener.com/solana/{pair}")
DEXSCREENER_TOKEN_URL = os.getenv("DEXSCREENER_TOKEN_URL", "https://dexscreener.com/solana/{mint}")
X_USER_URL             = os.getenv("X_USER_URL", "https://x.com/{handle}")

TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))
NO_MATCH_PING  = int(os.getenv("NO_MATCH_PING", "0"))

def _p(env_name: str, default_path: str) -> str:
    return os.getenv(env_name, default_path)

SUBS_FILE        = _p("SUBS_FILE",       "/tmp/telegram-bot/subscribers.txt")
FIRST_SEEN_FILE  = _p("FIRST_SEEN_FILE", "/tmp/telegram-bot/first_seen_caps.json")
FALLBACK_LOGO    = _p("FALLBACK_LOGO",   "/tmp/telegram-bot/solana_fallback.png")
MY_FOLLOWING_TXT = _p("MY_FOLLOWING_TXT","/home/user/telegram-bot/handles.partial.txt")
FOLLOWERS_CACHE_DIR = pathlib.Path(_p("FOLLOWERS_CACHE_DIR", "/tmp/telegram-bot/followers_cache"))
FB_STATIC_DIR       = pathlib.Path(_p("FB_STATIC_DIR",       "/tmp/telegram-bot/followers_static"))
MIRROR_JSON         = _p("MIRROR_JSON", "/tmp/telegram-bot/mirror.json")

for d in [pathlib.Path(SUBS_FILE).parent, pathlib.Path(FIRST_SEEN_FILE).parent, FOLLOWERS_CACHE_DIR, FB_STATIC_DIR, pathlib.Path(MIRROR_JSON).parent]:
    d.mkdir(parents=True, exist_ok=True)

TW_BEARER = os.getenv("TW_BEARER", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"tg-memebot/trade-{TRADE_SUMMARY_SEC}s", "Accept": "*/*"})
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

# -----------------------------------------------------------------------------
# Subs persistence
# -----------------------------------------------------------------------------
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists(): return set()
    try:
        return {int(x.strip()) for x in p.read_text().splitlines() if x.strip()}
    except Exception as e:
        log.warning("subs load failed: %r", e); return set()

def _save_subs_to_file():
    try:
        pathlib.Path(SUBS_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(SUBS_FILE).write_text("\\n".join(str(x) for x in sorted(SUBS)))
    except Exception as e:
        log.error("subs save failed: %r", e)

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

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
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
    """
    Returns logo URLs in priority order (highest quality first).
    Telegram will display images at best quality available up to 1280px.
    """
    cands: List[str] = []
    
    # Priority 1: Token's official imageUrl from DexScreener (usually best quality)
    if image_url: 
        cands.append(_normalize_ipfs(image_url))
    
    # Priority 2: DexScreener CDN (reliable, good quality, 200x200 typically)
    if mint:
        cands.append(f"https://cdn.dexscreener.com/token-icons/solana/{mint}.png")
    
    # Priority 3: DexScreener data CDN (backup)
    if mint:
        cands.append(f"https://dd.dexscreener.com/ds-data/tokens/solana/{mint}.png")
    
    # Remove duplicates while preserving order
    out=[]; seen=set()
    for u in cands:
        if u and u not in seen: 
            out.append(u)
            seen.add(u)
    return out

def _normalize_handle(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s: return None
    if s.startswith("@"): s=s[1:]
    if s.startswith("http"):
        from urllib.parse import urlparse
        try:
            u=urlparse(s); parts=[p for p in (u.path or "").split("/") if p]
            if parts: s=parts[0]
        except: pass
    return s.lower()

def _canon_url(u: Optional[str]) -> Optional[str]:
    if not u: return None
    u=u.strip()
    if u.startswith("//"): u="https:" + u
    if not (u.startswith("http://") or u.startswith("https://")): u="https://" + u
    return u

_URL_OK = re.compile(r"^https?://[^\\s]+$", re.IGNORECASE)
def _valid_url(u: Optional[str]) -> Optional[str]:
    u = _canon_url(u)
    return u if (u and _URL_OK.match(u)) else None

def _handle_from_url(u: str) -> Optional[str]:
    from urllib.parse import urlparse
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
                    
                    # For community URLs, return full URL without extracting handle
                    if "/i/communities/" in u.lower() or "/communities/" in u.lower():
                        return (None, u)  # No handle for community links
                    
                    # For regular profile URLs, extract handle
                    h = _handle_from_url(u) or _normalize_handle(handle or "")
                    return (h, u)
    for key in ("twitterUrl","twitter","x","twitterHandle"):
        v = info.get(key)
        if isinstance(v, str) and v.strip():
            if v.lower().startswith("http"):
                u=_canon_url(v)
                
                # For community URLs, return full URL
                if "/i/communities/" in u.lower() or "/communities/" in u.lower():
                    return (None, u)
                
                # For regular profile URLs, extract handle
                return (_handle_from_url(u), u)
            h=_normalize_handle(v)
            if h: return (h, f"https://x.com/{h}")
    return (None, None)

def _get_price_usd(p: dict) -> float:
    v = p.get("priceUsd")
    if v is None and isinstance(p.get("price"), dict):
        v = p["price"].get("usd")
    try:
        return float(v) if v is not None else 0.0
    except: return 0.0

# -----------------------------------------------------------------------------
# Dexscreener fetchers
# -----------------------------------------------------------------------------
# DexScreener API - ONLY using TOKEN_PROFILES_URL
# -----------------------------------------------------------------------------
TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
# Below APIs are NOT used - only profiles API above
TOKENS_URL         = "https://api.dexscreener.com/tokens/v1/{chainId}/{addresses}"
SEARCH_NEW_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:{chain}%20new"
SEARCH_ALL_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:{chain}"
TOKEN_PAIRS_URL    = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{address}"
PAIR_REFRESH_URL   = "https://api.dexscreener.com/latest/dex/pairs/{chainId}/{pairId}"

def _get_json(url, timeout=HTTP_TIMEOUT, tries=2):
    for i in range(tries):
        try:
            log.debug(f"[API] GET {url} (attempt {i+1}/{tries})")
            r = SESSION.get(url, timeout=timeout)
            log.debug(f"[API] Status: {r.status_code}, Content-Length: {len(r.content)}")
            
            if r.status_code == 200: 
                data = r.json()
                log.debug(f"[API] ✓ Success - returned {type(data)}")
                return data
            else:
                log.warning(f"[API] ✗ Status {r.status_code} for {url}")
                
        except requests.exceptions.Timeout:
            log.warning(f"[API] ✗ Timeout on {url}")
        except requests.exceptions.ConnectionError:
            log.warning(f"[API] ✗ Connection error on {url}")
        except json.JSONDecodeError as e:
            log.warning(f"[API] ✗ JSON decode error on {url}: {e}")
        except Exception as e:
            log.warning(f"[API] ✗ Unexpected error on {url}: {e}")
            
        time.sleep(0.2*(i+1))
    
    log.error(f"[API] ❌ Failed after {tries} attempts: {url}")
    return None

def _discover_search_new(chain=CHAIN_ID) -> List[dict]:
    j = _get_json(SEARCH_NEW_URL.format(chain=chain), timeout=15) or {}
    return j.get("pairs",[]) if isinstance(j,dict) else []

def _discover_search_all(chain=CHAIN_ID) -> List[dict]:
    j = _get_json(SEARCH_ALL_URL.format(chain=chain), timeout=15) or {}
    return j.get("pairs",[]) if isinstance(j,dict) else []

def _discover_profiles_latest(chain=CHAIN_ID) -> List[str]:
    log.info(f"[DEBUG] Calling profiles API: {TOKEN_PROFILES_URL}")
    arr = _get_json(TOKEN_PROFILES_URL, timeout=15) or []
    log.info(f"[DEBUG] Profiles API response type: {type(arr)}")
    log.info(f"[DEBUG] Profiles API returned {len(arr) if isinstance(arr, list) else 'N/A'} total items")
    
    if isinstance(arr, list) and len(arr) > 0:
        # Show first item as example
        log.info(f"[DEBUG] First item sample: {json.dumps(arr[0], indent=2)[:300]}")
    
    # Filter for our chain
    result = [x.get("tokenAddress") for x in arr if isinstance(x,dict) and (x.get("chainId") or "").lower()==chain]
    log.info(f"[DEBUG] After filtering for chain '{chain}': {len(result)} tokens")
    
    if len(result) > 0:
        log.info(f"[DEBUG] First 3 token addresses: {result[:3]}")
    
    return result

def _best_pool_for_mint(chain, mint) -> Optional[dict]:
    url = TOKEN_PAIRS_URL.format(chainId=chain, address=mint)
    log.info(f"[DEBUG] Fetching pairs for {mint[:10]}... from {url}")
    arr = _get_json(url, timeout=15) or []
    log.info(f"[DEBUG] Got {len(arr) if isinstance(arr, list) else 'N/A'} pairs for {mint[:10]}...")
    
    if not isinstance(arr,list) or not arr: 
        log.warning(f"[DEBUG] No pairs found for {mint[:10]}...")
        return None
    
    best=None; key=None
    for p in arr:
        liq = float((p.get("liquidity") or {}).get("usd",0) or 0)
        created = float(p.get("pairCreatedAt") or 0)
        k = (liq, created)
        if best is None or k > key: best, key = p, k
    
    if best:
        liq = float((best.get("liquidity") or {}).get("usd",0) or 0)
        log.info(f"[DEBUG] Best pair for {mint[:10]}... has ${liq:,.0f} liquidity")
    
    return best

def _tokens_batch(chain, mints: List[str]) -> List[dict]:
    out=[]; mints=[m for m in mints if m]
    for i in range(0,len(mints),30):
        chunk=",".join(mints[i:i+30])
        url=TOKENS_URL.format(chainId=chain, addresses=chunk)
        data=_get_json(url, timeout=20) or []
        if isinstance(data,list): out.extend(data)
        time.sleep(0.15)
    return out

# -----------------------------------------------------------------------------
# Mirror store
# -----------------------------------------------------------------------------
def _mirror_load() -> dict:
    p=pathlib.Path(MIRROR_JSON)
    if not p.exists(): return {"tokens":{},"pairs":{},"since":{}}
    try: return json.loads(p.read_text())
    except: return {"tokens":{},"pairs":{},"since":{}}

def _mirror_save(obj: dict) -> None:
    pathlib.Path(MIRROR_JSON).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(MIRROR_JSON).write_text(json.dumps(obj, indent=2))

MIRROR = _mirror_load()

def mirror_upsert_token(mint: str, pair: Optional[str], created_at: Optional[int], row: dict) -> None:
    t = MIRROR["tokens"].get(mint) or {"first_seen": int(time.time()), "seen": 0}
    t["last_seen"] = int(time.time())
    if created_at:
        try: t["pair_created_at"] = int(created_at)
        except: pass
    if pair: t["last_pair"] = pair
    t["last"] = row
    t["seen"] += 1
    MIRROR["tokens"][mint] = t

def mirror_upsert_pair(pair: str, chain: str, created_at: Optional[int], row: dict) -> None:
    p = MIRROR["pairs"].get(pair) or {"chainId": chain, "first_seen": int(time.time()), "seen": 0}
    p["last_seen"] = int(time.time())
    if created_at:
        try: p["pair_created_at"] = int(created_at)
        except: pass
    p["last"] = row
    p["seen"] += 1
    MIRROR["pairs"][pair] = p

def mirror_stats() -> dict:
    return {"tokens": len(MIRROR.get("tokens",{})), "pairs": len(MIRROR.get("pairs",{})), "since": MIRROR.get("since",{})}

# -----------------------------------------------------------------------------
# Ingester
# -----------------------------------------------------------------------------
def _normalize_row_to_token(row: dict) -> Tuple[str, Optional[str], Optional[int]]:
    base = row.get("baseToken") or {}
    mint = base.get("address") or row.get("baseTokenAddress") or row.get("tokenAddress") or ""
    pair = row.get("pairAddress") or ""
    created = row.get("pairCreatedAt")
    return (mint, pair, created)

async def ingester(context: ContextTypes.DEFAULT_TYPE):
    try:
        log.info("=" * 60)
        log.info("[Ingester] Starting cycle - Token Profiles API ONLY (new tokens with profiles)")
        log.info("=" * 60)
        
        # ONLY use profiles API - focuses on new tokens with updated profiles
        mints = _discover_profiles_latest(CHAIN_ID)
        log.info(f"[Ingester] ✓ Profiles API returned {len(mints)} {CHAIN_ID} tokens")
        
        if len(mints) == 0:
            log.warning("[Ingester] ⚠️ No new token profiles right now")
            return
        
        processed = 0
        failed = 0
        
        for i, mint in enumerate(mints):
            # Get best pool for this token
            best = _best_pool_for_mint(CHAIN_ID, mint)
            if best:
                mint_b, pair_b, created_b = _normalize_row_to_token(best)
                
                # Extract key stats
                base = best.get("baseToken") or {}
                name = base.get("symbol") or base.get("name") or "Unknown"
                liq = float((best.get("liquidity") or {}).get("usd", 0) or 0)
                vol24 = float((best.get("volume") or {}).get("h24", 0) or 0)
                fdv = best.get("fdv")
                mcap = float(fdv if fdv is not None else (best.get("marketCap") or 0) or 0)
                age = _pair_age_minutes(time.time()*1000.0, best.get("pairCreatedAt"))
                
                if pair_b: 
                    mirror_upsert_pair(pair_b, CHAIN_ID, created_b, best)
                if mint_b: 
                    mirror_upsert_token(mint_b, pair_b, created_b, best)
                
                log.info(f"[Ingester] upsert {mint_b[:12]}.. liq=${liq:,.0f} vol=${vol24:,.0f} mcap=${mcap:,.0f} age={age:.0f}m")
                processed += 1
            else:
                failed += 1

        _mirror_save(MIRROR)
        s = mirror_stats()
        log.info("=" * 60)
        log.info(f"[Ingester] ✅ Complete! Processed: {processed}/{len(mints)} | Mirror: {s['tokens']} tokens, {s['pairs']} pairs")
        log.info("=" * 60)
    except Exception as e:
        log.exception(f"[Ingester] ❌ ERROR: {e}")

# -----------------------------------------------------------------------------
# Mirror -> pairs rows
# -----------------------------------------------------------------------------
def _pairs_from_mirror() -> List[dict]:
    rows=[]; now_ms=time.time()*1000.0
    for mint, rec in MIRROR.get("tokens",{}).items():
        row = rec.get("last") or {}
        if not row: continue
        base=row.get("baseToken") or {}; info=row.get("info") or {}
        name  = base.get("symbol") or base.get("name") or "Unknown"
        token = base.get("address") or mint
        pair  = row.get("pairAddress") or (rec.get("last_pair") or "")
        price = _get_price_usd(row)
        liq   = float((row.get("liquidity") or {}).get("usd",0) or 0)
        fdv   = row.get("fdv")
        mcap  = float(fdv if fdv is not None else (row.get("marketCap") or 0) or 0)
        vol24 = float((row.get("volume") or {}).get("h24",0) or 0)
        url   = _valid_url(row.get("url") or (DEXSCREENER_PAIR_URL.format(pair=pair) if pair else ""))
        age_m = _pair_age_minutes(now_ms, row.get("pairCreatedAt"))
        x_handle, x_url = _extract_x(info)
        
        # Fix: use x_url directly - it already contains the full URL (community or profile)
        # from _extract_x which properly handles both cases
        if x_url:
            tw_url_final = x_url
        elif x_handle:
            tw_url_final = X_USER_URL.format(handle=x_handle)
        else:
            tw_url_final = "https://x.com/"
        
        rows.append({
            "name": name, "token": token, "pair": pair, "price_usd": price,
            "liquidity_usd": liq, "mcap_usd": mcap, "vol24_usd": vol24, "age_min": age_m,
            "url": url, "logo_hint": info.get("imageUrl") or base.get("logo") or "",
            "tw_url": tw_url_final,
            "tw_handle": x_handle,
            "axiom": AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/",
            "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
        })
    return rows

# -----------------------------------------------------------------------------
# First-seen & tracking
# -----------------------------------------------------------------------------
def _load_first_seen():
    p=pathlib.Path(FIRST_SEEN_FILE)
    if p.exists():
        try: return json.loads(p.read_text())
        except: return {}
    return {}
def _save_first_seen(d):
    try:
        pathlib.Path(FIRST_SEEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(FIRST_SEEN_FILE).write_text(json.dumps(d, indent=2))
    except Exception as e:
        log.error("save first_seen failed: %r", e)

FIRST_SEEN = _load_first_seen()
TRACKED: Set[str] = set()
LAST_PINNED: Dict[Tuple[int, str], int] = {}

def decorate_with_first_seen(pairs):
    changed=False; now_ts=int(time.time())
    for m in pairs:
        tok = m.get("token") or ""
        cur = float(m.get("mcap_usd") or 0)
        rec = FIRST_SEEN.get(tok)
        is_new = rec is None
        if is_new:
            FIRST_SEEN[tok] = {
                "first": (cur if cur>0 else 0.0), 
                "ts": now_ts,
                "tw_handle": m.get("tw_handle"),  # Store Twitter handle
                "tw_url": m.get("tw_url")          # Store Twitter URL
            }
            changed=True
        else:
            if rec.get("first",0)==0 and cur>0:
                rec["first"]=cur; changed=True
            # Store Twitter info if not already stored
            if not rec.get("tw_handle") and m.get("tw_handle"):
                rec["tw_handle"] = m.get("tw_handle")
                changed = True
            if not rec.get("tw_url") and m.get("tw_url"):
                rec["tw_url"] = m.get("tw_url")
                changed = True
        m["is_first_time"]=is_new
        m["first_mcap_usd"]=float(FIRST_SEEN.get(tok,{}).get("first",0))
    if changed: _save_first_seen(FIRST_SEEN)

# -----------------------------------------------------------------------------
# Followed-by (Nitter)
# -----------------------------------------------------------------------------
def load_my_following() -> Set[str]:
    p = pathlib.Path(MY_FOLLOWING_TXT)
    if not p.exists(): return set()
    out=set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            h=_normalize_handle(line)
            if h: out.add(h)
    except: pass
    return out
MY_HANDLES: Set[str] = load_my_following()

def _followers_cache_path(handle:str) -> pathlib.Path: return FOLLOWERS_CACHE_DIR / f"{handle.lower()}.json"
def _followers_cache_load(handle:str) -> Optional[Set[str]]:
    p=_followers_cache_path(handle)
    if not p.exists(): return None
    try:
        j=json.loads(p.read_text(encoding="utf-8"))
        return set(j.get("followers",[]))
    except: return None
def _followers_cache_save(handle:str, followers:Set[str]) -> None:
    p=_followers_cache_path(handle)
    p.write_text(json.dumps({"followers": sorted(followers)}, ensure_ascii=False, indent=2), encoding="utf-8")

_NITTER_ENV = os.getenv("NITTER_BASE", "").rstrip("/")
NITTER_MIRRORS = [m for m in [_NITTER_ENV or None,"https://nitter.net","https://nitter.poast.org","https://ntrqq.com","https://n.l5.ca"] if m]
_USERNAME_PATTERNS = [
    re.compile(r'class="username"[^>]*>\\s*@?<bdi>\\s*([^<\\s]+)\\s*</bdi>', re.IGNORECASE),
    re.compile(r'<a[^>]+class="username"[^>]+href="/([^/"]+)"', re.IGNORECASE),
    re.compile(r'<span[^>]+class="username"[^>]*>\\s*@?\\s*([^<\\s]+)\\s*</span>', re.IGNORECASE),
    re.compile(r'href="/([A-Za-z0-9_]{1,15})"[^>]*class="username"', re.IGNORECASE),
]
def _nitter_get_from(base, path, params=None):
    try:
        url=f"{base}{path}"; r=SESSION.get(url, params=params or {}, timeout=20)
        if r.status_code==200 and r.text: 
            if DEBUG_FB: log.info(f"[nitter] {url} OK len={len(r.text)}")
            return r.text
        if DEBUG_FB: log.info(f"[nitter] {url} -> {r.status_code}")
    except Exception as e:
        if DEBUG_FB: log.info(f"[nitter] fetch error {base}{path}: {e}")
    return None
def _nitter_get(path, params=None):
    for base in NITTER_MIRRORS:
        html=_nitter_get_from(base, path, params)
        if html: return html
    return None
def _parse_nitter_usernames(html: str) -> List[str]:
    out=[]; 
    if not html: return out
    for pat in _USERNAME_PATTERNS:
        for m in pat.finditer(html):
            h=_normalize_handle(m.group(1))
            if h: out.append(h)
    seen=set(); uniq=[]
    for h in out:
        if h not in seen: uniq.append(h); seen.add(h)
    if DEBUG_FB: log.info(f"[nitter] parsed {len(uniq)} usernames")
    return uniq
def _followers_scrape_nitter(handle: str, max_total: int = 1000, max_pages: int = 8) -> Optional[Set[str]]:
    if not handle: return None
    def _fetch_page(pg:int):
        if pg==1:
            html=_nitter_get(f"/{handle}/followers", params=None)
            if html: return html
        html=_nitter_get(f"/{handle}/followers", params={"page": str(pg)})
        if html: return html
        return _nitter_get(f"/{handle}/followers", params={"p": str(pg)})
    seen=set()
    for page in range(1, max_pages+1):
        html=_fetch_page(page)
        if not html:
            if DEBUG_FB: log.info(f"[nitter] page {page}: no html"); break
        low=html.lower()
        if ("rate limit" in low) or ("please try again later" in low):
            if DEBUG_FB: log.info(f"[nitter] page {page}: rate-limited"); break
        batch=_parse_nitter_usernames(html)
        batch=[h for h in batch if h and h != handle.lower()]
        before=len(seen)
        for h in batch: seen.add(h)
        if DEBUG_FB: log.info(f"[nitter] page {page}: +{len(seen)-before}, total={len(seen)}")
        if len(seen)==before or len(seen)>=max_total: break
        time.sleep(0.6)
    return seen if seen else None
def _followers_static_load(handle: str) -> Optional[Set[str]]:
    handle=(handle or "").strip().lower()
    if not handle: return None
    txt = FB_STATIC_DIR / f"{handle}.txt"
    jsn = FB_STATIC_DIR / f"{handle}.json"
    try:
        if txt.exists():
            out=[]
            for line in txt.read_text(encoding="utf-8", errors="ignore").splitlines():
                h=_normalize_handle(line)
                if h: out.append(h)
            return set(out)
        if jsn.exists():
            j=json.loads(jsn.read_text(encoding="utf-8"))
            if isinstance(j,dict) and isinstance(j.get("followers"),list):
                return { _normalize_handle(x) for x in j["followers"] if _normalize_handle(x) }
    except Exception as e:
        log.warning(f"static followers load failed for {handle}: {e}")
    return None
def fetch_followers_v2(handle: str, max_total: int = 1000) -> Optional[Set[str]]:
    if not handle: return None
    static=_followers_static_load(handle)
    if static: return static
    cached=_followers_cache_load(handle)
    if cached: return cached
    scraped=_followers_scrape_nitter(handle, max_total=max_total)
    if scraped:
        _followers_cache_save(handle, scraped)
        return scraped
    if TW_BEARER: return None
    return None
def load_my_following() -> Set[str]:
    p=pathlib.Path(MY_FOLLOWING_TXT)
    if not p.exists(): return set()
    out=set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            h=_normalize_handle(line)
            if h: out.add(h)
    except: pass
    return out
MY_HANDLES = load_my_following()
def overlap_line(tw_handle: Optional[str]) -> str:
    if not tw_handle or not MY_HANDLES: return "—"
    followers = fetch_followers_v2(tw_handle, max_total=1000)
    if not followers: return "—"
    overlap = sorted(MY_HANDLES & followers)
    if not overlap: return "—"
    acc=[]; total=0
    for h in overlap:
        piece="@"+h+", "
        if total + len(piece) > 180: break
        acc.append(piece); total += len(piece)
    s="".join(acc).rstrip(", ")
    return s + (" , …" if len(overlap) > len(acc) else "")

# -----------------------------------------------------------------------------
# UI builders
# -----------------------------------------------------------------------------
def link_keyboard(m: dict) -> InlineKeyboardMarkup:
    pair = m.get("pair") or ""
    mint = m.get("token") or ""
    ds_url = m.get("url") or (f"https://dexscreener.com/{CHAIN_ID}/{pair}" if pair else "https://dexscreener.com/")
    ax_url = m.get("axiom") or (AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/")
    gm_url = m.get("gmgn") or (GMGN_WEB_URL.format(mint=mint) if mint else "https://gmgn.ai/")
    x_url  = m.get("tw_url") or "https://x.com/"
    def _norm(u: str) -> str:
        u=(u or "").strip()
        if u.startswith("//"): u="https:"+u
        if not (u.startswith("http://") or u.startswith("https://")): u="https://"+u
        return u
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=_norm(ds_url)),
         InlineKeyboardButton("Axiom",       url=_norm(ax_url))],
        [InlineKeyboardButton("GMGN",        url=_norm(gm_url)),
         InlineKeyboardButton("X",           url=_norm(x_url))],
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
        f"🖨️ <b>Mint:</b>\n<code>{html_escape(m['token'])}</code>\n"
        f"🔗 <b>Pair:</b>\n<code>{html_escape(m['pair'])}</code>\n"
        f"💧 <b>Liquidity:</b> ${m['liquidity_usd']:,.0f}\n"
        f"{price_line}\n"
        f"📈 <b>Vol 24h:</b> ${m['vol24_usd']:,.0f}\n"
        f"⏱️ <b>Age:</b> {int(m['age_min'])} min\n"
        f"{XEMO} <b>Followed by:</b> {html_escape(fb_text)}"
    )

# -----------------------------------------------------------------------------
# Send helpers
# -----------------------------------------------------------------------------
def _is_keyboard_reject(e: Exception) -> bool:
    s=str(e).lower()
    return ("reply markup is not allowed" in s) or ("keyboardbuttonpolltype" in s) or ("polls are unallowed" in s)
def _merge_current(into: dict, cur: dict) -> dict:
    if not into or not cur: return into
    for k in ("pair","price_usd","liquidity_usd","mcap_usd","vol24_usd","age_min","url","logo_hint","tw_url","tw_handle"):
        v = cur.get(k)
        if v is not None: into[k] = v
    return into
async def _send_or_photo(bot, chat_id:int, caption:str, kb, token:str, logo_hint:str, pin:bool=False) -> Optional[int]:
    cap = caption if len(caption) <= 900 else (caption[:870] + " …")
    async def _maybe_pin(msg_id: int):
        if not pin: return
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=msg_id, disable_notification=True)
        except Exception as e:
            log.info(f"Pin attempt failed (non-fatal): {e}")
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

# -----------------------------------------------------------------------------
# Alert/update
# -----------------------------------------------------------------------------
def best_per_token(pairs):
    best={}
    for m in pairs:
        key = (m.get("token") or f"__unknown__:{m.get('pair') or m.get('url') or id(m)}")
        if key not in best or m["vol24_usd"]>best[key]["vol24_usd"]:
            best[key] = m
    return list(best.values())
def passes_filters_for_alert(m: dict) -> bool:
    try:
        liq=float(m.get("liquidity_usd") or 0)
        mcap=float(m.get("mcap_usd") or 0)
        vol24=float(m.get("vol24_usd") or 0)
        age=float(m.get("age_min") or float("inf"))
        liq_ok  = liq >= MIN_LIQ_USD
        age_ok  = (age <= MAX_AGE_MIN) if age != float("inf") else True
        mcap_ok = (mcap >= MIN_MCAP_USD) if mcap > 0 else True
        vol_ok  = (vol24 >= MIN_VOL_H24_USD) if vol24 > 0 else True
        return liq_ok and age_ok and mcap_ok and vol_ok
    except: return False
async def send_new_token(bot, chat_id:int, m:dict):
    fb_text = overlap_line(m.get("tw_handle"))
    caption = build_caption(m, fb_text, is_update=False)
    kb = link_keyboard(m)
    key=(chat_id, m.get("token") or "")
    should_pin = key not in LAST_PINNED
    msg_id = await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"), pin=should_pin)
    if should_pin and msg_id: LAST_PINNED[key]=msg_id
async def send_price_update(bot, chat_id:int, m:dict):
    fb_text="—"; caption = build_caption(m, fb_text, is_update=True); kb=link_keyboard(m)
    await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"), pin=False)
async def do_trade_push(bot):
    try:
        pairs = best_per_token(_pairs_from_mirror())
        decorate_with_first_seen(pairs)
        if not pairs and NO_MATCH_PING:
            for chat_id in list(SUBS):
                await bot.send_message(chat_id=chat_id, text="(auto /trade) no matches right now.", disable_web_page_preview=True)
            return
        for chat_id in list(SUBS):
            sent=0
            for m in pairs:
                if TOP_N_PER_TICK > 0 and sent >= TOP_N_PER_TICK: break
                if not passes_filters_for_alert(m): continue
                already_tracked = m["token"] in TRACKED
                TRACKED.add(m["token"])
                if m.get("is_first_time") or not already_tracked:
                    await send_new_token(bot, chat_id, m); sent += 1
                await asyncio.sleep(0.05)
    except Exception as e:
        log.exception(f"do_trade_push error: {e}")
async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🔥 [tick] auto_trade fired (interval={TRADE_SUMMARY_SEC}s)")
    await do_trade_push(context.bot)
async def updater(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🧊 [tick] updater fired (interval={UPDATE_INTERVAL_SEC}s)")
    try:
        if not TRACKED: return
        now_ts=int(time.time())
        log.info(f"[updater] refreshing {len(TRACKED)} tracked tokens")
        for token in list(TRACKED):
            first_rec = FIRST_SEEN.get(token) or {}
            first_ts = int(first_rec.get("ts", now_ts))
            if now_ts - first_ts >= UPDATE_MAX_DURATION_MIN * 60:
                TRACKED.discard(token); continue
            cur=_best_pool_for_mint(CHAIN_ID, token)
            if not cur: continue
            base=cur.get("baseToken") or {}; info=cur.get("info") or {}
            
            # Use stored Twitter info for consistency
            stored_tw_handle = first_rec.get("tw_handle")
            stored_tw_url = first_rec.get("tw_url")
            fresh_tw_handle, fresh_tw_url = _extract_x(info)
            
            m = {
                "name": base.get("symbol") or base.get("name") or "Unknown",
                "token": base.get("address") or token,
                "pair": cur.get("pairAddress") or "",
                "price_usd": _get_price_usd(cur),
                "liquidity_usd": float((cur.get("liquidity") or {}).get("usd",0) or 0),
                "mcap_usd": float((cur.get("fdv") if cur.get("fdv") is not None else (cur.get("marketCap") or 0)) or 0),
                "vol24_usd": float((cur.get("volume") or {}).get("h24",0) or 0),
                "age_min": _pair_age_minutes(time.time()*1000.0, cur.get("pairCreatedAt")),
                "url": _valid_url(cur.get("url") or ""),
                "logo_hint": info.get("imageUrl") or base.get("logo") or "",
                "tw_handle": stored_tw_handle or fresh_tw_handle,  # Prefer stored
                "tw_url": _valid_url(stored_tw_url or fresh_tw_url),  # Prefer stored
                "axiom": AXIOM_WEB_URL.format(pair=cur.get("pairAddress") or "") if cur.get("pairAddress") else "https://axiom.trade/",
                "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
            }
            if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
                TRACKED.discard(token); continue
            m["first_mcap_usd"] = float(first_rec.get("first", 0.0))
            m["is_first_time"]  = False
            for chat_id in list(SUBS):
                if passes_filters_for_alert(m):
                    await send_price_update(context.bot, chat_id, m)
                    await asyncio.sleep(0.02)
    except Exception as e:
        log.exception(f"updater job error: {e}")

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id); _save_subs_to_file()
    await u.message.reply_text(f"✅ Subscribed. 🔥 /trade every {TRADE_SUMMARY_SEC}s + 🧊 updates every {UPDATE_INTERVAL_SEC}s (stop after {UPDATE_MAX_DURATION_MIN} min).")
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
    s=mirror_stats()
    await u.message.reply_text(
        f"Subscribers: {len(SUBS)} | 🔥 /trade every {TRADE_SUMMARY_SEC}s | 🧊 updates every {UPDATE_INTERVAL_SEC}s | Top/tick {TOP_N_PER_TICK or 'unlimited'} | Max alert age {int(MAX_AGE_MIN)}m | Update stop {UPDATE_MAX_DURATION_MIN}m\\n"
        f"Mirror -> tokens: {s['tokens']} | pairs: {s['pairs']}"
    )
async def cmd_trade(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split()
    manual_cap = None
    if len(args) >= 2:
        try: manual_cap = max(1, int(args[1]))
        except: manual_cap = None
    pairs = best_per_token(_pairs_from_mirror())
    decorate_with_first_seen(pairs)
    cap = manual_cap if manual_cap is not None else (TOP_N_PER_TICK if TOP_N_PER_TICK > 0 else 10_000)
    sent=0
    for m in pairs:
        if not passes_filters_for_alert(m): continue
        TRACKED.add(m["token"])
        if m.get("is_first_time"):
            await send_new_token(c.bot, u.effective_chat.id, m)
        else:
            await send_price_update(c.bot, u.effective_chat.id, m)
        sent += 1
        if sent >= cap: break
        await asyncio.sleep(0.05)
    if sent == 0:
        await u.message.reply_text("(trade) no matches with current filters.")
async def cmd_fb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split()
    if len(args) < 2: await u.message.reply_text("Usage: /fb handle"); return
    handle = _normalize_handle(args[1])
    ol = overlap_line(handle)
    await u.message.reply_text(f"Overlap for @{handle}:\\n{ol}")
async def cmd_mirror(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = mirror_stats()
    await u.message.reply_text(json.dumps(s, indent=2))

async def cmd_tokens(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Show all tokens currently in mirror with their stats"""
    args = (u.message.text or "").split()
    limit = 20  # Default show 20
    
    if len(args) >= 2:
        try:
            limit = min(int(args[1]), 100)  # Max 100
        except:
            limit = 20
    
    pairs = _pairs_from_mirror()
    
    if not pairs:
        await u.message.reply_text("No tokens in mirror yet. Wait for ingester to run.")
        return
    
    # Sort by volume (highest first)
    pairs.sort(key=lambda x: x.get("vol24_usd", 0), reverse=True)
    
    # Count how many pass filters
    passing = sum(1 for m in pairs if passes_filters_for_alert(m))
    
    # Take top N
    shown = pairs[:limit]
    
    response = f"📊 <b>Mirror Stats</b>\n"
    response += f"Total tokens: {len(pairs)}\n"
    response += f"Pass filters: {passing} ✅\n"
    response += f"Fail filters: {len(pairs) - passing} ❌\n\n"
    response += f"<b>Top {len(shown)} tokens (by volume):</b>\n\n"
    
    for i, m in enumerate(shown, 1):
        name = m.get("name", "Unknown")
        token = m.get("token", "")
        token_short = token[:8] + "..." if len(token) > 8 else token
        liq = m.get("liquidity_usd", 0)
        mcap = m.get("mcap_usd", 0)
        vol24 = m.get("vol24_usd", 0)
        age = m.get("age_min", 0)
        
        # Check which filters it passes
        passes_liq = liq >= MIN_LIQ_USD
        passes_mcap = mcap >= MIN_MCAP_USD if mcap > 0 else True
        passes_vol = vol24 >= MIN_VOL_H24_USD if vol24 > 0 else True
        passes_age = age <= MAX_AGE_MIN if age != float("inf") else True
        passes_all = passes_liq and passes_mcap and passes_vol and passes_age
        
        status = "✅" if passes_all else "❌"
        
        response += f"{i}. {status} <b>{html_escape(name)}</b>\n"
        response += f"   💧 Liq: ${liq:,.0f} {'✅' if passes_liq else f'❌ (need ${MIN_LIQ_USD:,.0f})'}\n"
        
        if not passes_mcap:
            response += f"   📊 MCap: ${mcap:,.0f} ❌ (need ${MIN_MCAP_USD:,.0f})\n"
        
        if not passes_vol:
            response += f"   📈 Vol: ${vol24:,.0f} ❌ (need ${MIN_VOL_H24_USD:,.0f})\n"
        
        if not passes_age and age != float("inf"):
            response += f"   ⏰ Age: {age:.0f}m ❌ (max {MAX_AGE_MIN:.0f}m)\n"
        
        response += "\n"
        
        if len(response) > 3800:  # Telegram message limit with buffer
            await u.message.reply_text(response, parse_mode="HTML")
            response = ""
    
    if response:
        await u.message.reply_text(response, parse_mode="HTML")

# -----------------------------------------------------------------------------
# Bot & jobs
# -----------------------------------------------------------------------------
async def _post_init(app: Application):
    global SUBS
    SUBS = _load_subs_from_file()
    if ALERT_CHAT_ID:
        SUBS.add(ALERT_CHAT_ID); _save_subs_to_file()
    await _validate_subs(app.bot)
    log.info(f"Subscribers: {sorted(SUBS)}")

application = Application.builder().token(TG).post_init(_post_init).build()
application.add_handler(CommandHandler("start",      cmd_start))
application.add_handler(CommandHandler("id",         cmd_id))
application.add_handler(CommandHandler("subscribe",  cmd_sub))
application.add_handler(CommandHandler("unsubscribe",cmd_unsub))
application.add_handler(CommandHandler("status",     cmd_status))
application.add_handler(CommandHandler("trade",      cmd_trade))
application.add_handler(CommandHandler("fb",         cmd_fb))
application.add_handler(CommandHandler("mirror",     cmd_mirror))
application.add_handler(CommandHandler("tokens",     cmd_tokens))

# -----------------------------------------------------------------------------
# FastAPI + webhook
# -----------------------------------------------------------------------------
app = FastAPI(title="Telegram Webhook")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR
    SUBS = _load_subs_from_file()
    FIRST_SEEN = _load_first_seen()
    MIRROR = _mirror_load()
    asyncio.create_task(_start_bot_and_jobs())

async def _start_bot_and_jobs():
    try:
        await application.initialize()
        jq = application.job_queue
        jq.run_repeating(ingester, interval=timedelta(seconds=INGEST_INTERVAL_SEC), first=timedelta(seconds=2), name="ingester")
        jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC), first=timedelta(seconds=3), name="trade_tick")
        jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC), first=timedelta(seconds=20), name="updates")
        await application.start()
        log.info("Bot initialized & started")
    except Exception as e:
        log.exception("Bot startup failed: %r", e)

@app.on_event("shutdown")
async def _shutdown():
    try:
        await application.stop()
    finally:
        await application.shutdown()

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TG: return Response(status_code=403)
    try:
        data: Dict[str, Any] = await request.json()
    except Exception as e:
        log.warning("webhook json parse error: %r", e); return Response(status_code=400)
    try:
        msg = data.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id and text in ("/start", "/ping", "/id", "ping"):
            await application.bot.send_message(chat_id, "✅ Webhook round-trip OK")
    except Exception as e:
        log.warning("webhook fast-reply error: %r", e)
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        log.exception("process_update error: %r", e)
    return Response(status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
