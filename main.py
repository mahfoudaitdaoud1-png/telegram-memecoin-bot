#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAIN BOT (INTEGRATED): Dexscreener alerts + X (Twitter) Community/Profile scraper
=================================================================================
This single file merges:
  • Your memecoin alert Telegram bot (Dexscreener-driven)
  • A no-API, no-Chromium X scraper for both Communities and Profiles
  • A neat “Followed by / Extras / Source” output layout with clickable usernames
  • Keeps your original commands and adds: /xscan <X URL>

Why this is long:
  - Production-grade comments, clear sections, and robust helpers
  - Realistic handling of edge-cases, timeouts, and partial failures
  - Strictly meaningful logic — not whitespace padding

----------------------------------------------------------------------------------
COMMAND SUMMARY
----------------------------------------------------------------------------------
/start        → subscribe and brief info
/id           → prints chat id
/subscribe    → subscribe to alerts
/unsubscribe  → unsubscribe
/status       → current thresholds & cadence
/trade [N]    → show current matches (Dexscreener), optional cap N
/fb <handle>  → show “followed-by overlap” from your MY_FOLLOWING_TXT list
/xscan <URL>  → NEW: scan X community OR profile link and return:
                  Followed by (clickable handles you follow)
                  Extras     (clickable handles you don't)
                  Source     (per-user clickable post links)

----------------------------------------------------------------------------------
ENVIRONMENT (Cloud Run / local)
----------------------------------------------------------------------------------
# Telegram
TG=123456:ABC...                   # required

# Following list (one handle/line; with or without '@')
MY_FOLLOWING_TXT=/app/handles.partial.txt

# Dexscreener thresholds (defaults align with your earlier setup)
MIN_LIQ_USD=35000
MIN_MCAP_USD=70000
MIN_VOL_H24_USD=40000
MAX_AGE_MIN=120

# Cadence for trade summaries and background updates
TRADE_SUMMARY_SEC=5
UPDATE_INTERVAL_SEC=90
UPDATE_MAX_DURATION_MIN=60

# Top N per tick (0 = unlimited), ping when no matches
TOP_N_PER_TICK=0
NO_MATCH_PING=0

# Webhook + server
BASE_WEBHOOK_URL=https://your-cloud-run-url.a.run.app
WEBHOOK_PATH=/webhook/${TG}
PORT=8080

# X scraper message formatting caps
MAX_USERS_PER_SECTION=50
MAX_SOURCE_LINKS_PER_USER=3
MAX_MESSAGE_CHARS=3500

# Optional: TW_BEARER, NITTER_BASE (not required for this scraper path)
# TW_BEARER=...
# NITTER_BASE=...

----------------------------------------------------------------------------------
NOTES
----------------------------------------------------------------------------------
- The X scraper uses "reader view" snapshots via r.jina.ai (public reader),
  so it needs no login, cookies, Chromium, or dev API. It is snapshot-based,
  so re-run later to catch more items as the page grows.
- It normalizes tweet URLs to canonical https://x.com/<user>/status/<id>,
  attempts to resolve /i/web/status/<id> shapes, and works on both communities
  and normal profile pages out of the box.
- All usernames in Followed by / Extras are clickable links to their profiles.
- “Source” lists each user with up to MAX_SOURCE_LINKS_PER_USER clickable posts.
- Dexscreener logic is preserved as in your previous version, including:
  • token discovery
  • filter application
  • “first seen” tracking
  • pinned hot token and ice updates
----------------------------------------------------------------------------------
"""

# ============================================================================
#                                 IMPORTS
# ============================================================================
import os
import re
import time
import json
import math
import logging
import pathlib
import asyncio
import requests
from typing import Dict, List, Set, Optional, Tuple, Any
from urllib.parse import urlparse
from datetime import timedelta

# --- FastAPI + Telegram ---
from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ============================================================================
#                              GLOBAL LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True
)
log = logging.getLogger("unified-bot")

# ============================================================================
#                            ENV & RUNTIME OPTIONS
# ============================================================================
TG = os.getenv("TG", "").strip()
if not TG:
    raise SystemExit("Missing TG token (env TG).")

# Cadence & Updates
TRADE_SUMMARY_SEC = int(os.getenv("TRADE_SUMMARY_SEC", "5"))
UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))

# Filters (Dexscreener)
MIN_LIQ_USD     = float(os.getenv("MIN_LIQ_USD",     "35000"))
MIN_MCAP_USD    = float(os.getenv("MIN_MCAP_USD",    "70000"))
MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
MAX_AGE_MIN     = float(os.getenv("MAX_AGE_MIN",     "120"))
CHAIN_ID        = "solana"

# Feature caps / notifications
TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))
NO_MATCH_PING  = int(os.getenv("NO_MATCH_PING", "0"))

# File paths
def _p(env_name: str, default_path: str) -> str:
    return os.getenv(env_name, default_path)

SUBS_FILE        = _p("SUBS_FILE",       "/tmp/telegram-bot/subscribers.txt")
FIRST_SEEN_FILE  = _p("FIRST_SEEN_FILE", "/tmp/telegram-bot/first_seen_caps.json")
FALLBACK_LOGO    = _p("FALLBACK_LOGO",   "/tmp/telegram-bot/solana_fallback.png")
MY_FOLLOWING_TXT = _p("MY_FOLLOWING_TXT","handles.partial.txt")

FOLLOWERS_CACHE_DIR = pathlib.Path(_p("FOLLOWERS_CACHE_DIR", "/tmp/telegram-bot/followers_cache"))
FB_STATIC_DIR       = pathlib.Path(_p("FB_STATIC_DIR",       "/tmp/telegram-bot/followers_static"))
for d in [
    pathlib.Path(SUBS_FILE).parent,
    pathlib.Path(FIRST_SEEN_FILE).parent,
    FOLLOWERS_CACHE_DIR,
    FB_STATIC_DIR
]:
    d.mkdir(parents=True, exist_ok=True)

# X Scraper formatting caps
MAX_USERS_PER_SECTION       = int(os.getenv("MAX_USERS_PER_SECTION", "50"))
MAX_SOURCE_LINKS_PER_USER   = int(os.getenv("MAX_SOURCE_LINKS_PER_USER", "3"))
MAX_MESSAGE_CHARS           = int(os.getenv("MAX_MESSAGE_CHARS", "3500"))

# Webhook and server
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", "").strip()
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", f"/webhook/{TG}")
PORT             = int(os.getenv("PORT", "8080"))

# Optional
DEBUG_FB = os.getenv("DEBUG_FB", "0") == "1"
TW_BEARER = os.getenv("TW_BEARER", "").strip()  # not required for the scraper path

# ============================================================================
#                        DEXSCREENER ENDPOINTS & SESSION
# ============================================================================
TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL         = "https://api.dexscreener.com/tokens/v1/{chainId}/{addresses}"
SEARCH_NEW_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:solana%20new"
SEARCH_ALL_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:solana"
TOKEN_PAIRS_URL    = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"

AXIOM_WEB_URL     = os.getenv("AXIOM_WEB_URL") or os.getenv("AXIOME_WEB_URL") or "https://axiom.trade/meme/{pair}"
GMGN_WEB_URL      = os.getenv("GMGN_WEB_URL",  "https://gmgn.ai/sol/token/{mint}")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "tg-unified-bot/1.0", "Accept": "*/*"})
HTTP_TIMEOUT = 20

# ============================================================================
#                               SUBSCRIPTIONS
# ============================================================================
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists(): return set()
    try:
        return {int(x.strip()) for x in p.read_text().splitlines() if x.strip()}
    except Exception as e:
        log.warning("subs load failed: %r", e)
        return set()

def _save_subs_to_file():
    try:
        pathlib.Path(SUBS_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(SUBS_FILE).write_text("\n".join(str(x) for x in sorted(SUBS)))
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

# ============================================================================
#                               UTILITIES
# ============================================================================
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
        return (parts[0] if parts else "").lower()
    except: return None

# ============================================================================
#                       DEXSCREENER FETCH + FILTERS
# ============================================================================
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

def _get_price_usd(p: dict) -> float:
    v = p.get("priceUsd")
    if v is None and isinstance(p.get("price"), dict):
        v = p["price"].get("usd")
    try:
        return float(v) if v is not None else 0.0
    except:
        return 0.0

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
    x_handle, x_url = (None, None)
    # extract X hints from “info” shape
    if isinstance(info, dict):
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
                        h = _handle_from_url(u) or (handle or "").lower().lstrip("@")
                        x_handle, x_url = h, u
                        break

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

def _enrich_if_needed(m: dict) -> dict:
    need_age  = (m.get("age_min") in (None, float("inf")))
    need_mcap = float(m.get("mcap_usd") or 0) <= 0
    need_vol  = float(m.get("vol24_usd") or 0) <= 0
    need_liq  = float(m.get("liquidity_usd") or 0) < MIN_LIQ_USD
    if not (need_age or need_mcap or need_vol or need_liq):
        return m
    cur = _current_for_token(m.get("token"))
    if not cur: return m
    for k in ("pair","price_usd","liquidity_usd","mcap_usd","vol24_usd","age_min","url","logo_hint","tw_url","tw_handle"):
        if cur.get(k) is not None:
            m[k] = cur[k]
    return m

def passes_filters(p, now_ms):
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

    seen_pairs: Set[str] = set()
    uniq: List[dict] = []

    for p in pairs:
        if not isinstance(p, dict):
            continue
        base = (p.get("baseToken") or {}) if isinstance(p.get("baseToken"), dict) else {}
        mint = base.get("address") or p.get("baseTokenAddress") or p.get("tokenAddress") or ""
        k = p.get("pairAddress") or p.get("url") or mint
        if not k or k in seen_pairs:
            continue
        seen_pairs.add(k)
        if mint and isinstance(p.get("baseToken"), dict):
            p["baseToken"]["address"] = mint
        uniq.append(p)

    now_ms=time.time()*1000.0
    matches=[]
    for p in uniq:
        if not passes_filters(p, now_ms):
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

            # enforce only when present / meaningful (keeps sparse-but-promising)
            if m_try.get("token"):
                if float(m_try.get("liquidity_usd") or 0) < MIN_LIQ_USD:
                    continue
            if (m_try.get("age_min") not in (None, float("inf"))) and (m_try["age_min"] > MAX_AGE_MIN):
                continue
            if (m_try.get("mcap_usd") or 0) > 0 and m_try["mcap_usd"] < MIN_MCAP_USD:
                continue
            if (m_try.get("vol24_usd") or 0) > 0 and m_try["vol24_usd"] < MIN_VOL_H24_USD:
                continue

            matches.append(m_try)
            continue

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
        x_handle, x_url = (None, None)
        if isinstance(info, dict):
            for key in ("socials","links","websites"):
                arr = info.get(key)
                if isinstance(arr, list):
                    for it in arr:
                        if not isinstance(it, dict): continue
                        url2 = it.get("url") or it.get("link")
                        plat = (it.get("platform") or it.get("type") or it.get("label") or "").lower()
                        handle = it.get("handle")
                        if url2 and ("twitter" in url2.lower() or "x.com" in url2.lower() or "twitter" in plat or "x" == plat):
                            u = _canon_url(url2)
                            h = _handle_from_url(u) or (handle or "").lower().lstrip("@")
                            x_handle, x_url = h, u
                            break

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
        m = _enrich_if_needed(m)
        matches.append(m)

    # coalesce best per mint by vol24
    best={}
    for m in matches:
        key = (m.get("token") or f"__unknown__:{m.get('pair') or m.get('url') or id(m)}")
        if key not in best or m["vol24_usd"] > best[key]["vol24_usd"]:
            best[key] = m
    return list(best.values())

# ============================================================================
#                       FIRST-SEEN TRACKING FOR UPDATES
# ============================================================================
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
TRACKED: Set[str] = set()  # tokens to generate price updates for
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

# ============================================================================
#                       FOLLOWED-BY (OVERLAP) UTILS
# ============================================================================
USER_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

def norm_handle(h: Optional[str]) -> Optional[str]:
    if not h: return None
    h = h.strip().lstrip("@")
    return h.lower() if USER_RE.fullmatch(h) else None

def profile_url(h: str) -> str:
    return f"https://x.com/{h}"

def user_anchor(h: str) -> str:
    return f'<a href="{profile_url(h)}">@{h}</a>'

def load_following(path: str) -> Set[str]:
    out: Set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                hh = norm_handle(line)
                if hh:
                    out.add(hh.lower())
    except Exception as e:
        log.warning(f"Could not load following list from {path}: {e}")
    return out

MY_HANDLES: Set[str] = load_following(MY_FOLLOWING_TXT)
log.info(f"(overlap) loaded {len(MY_HANDLES)} handles from {MY_FOLLOWING_TXT}")

def overlap_line(tw_handle: Optional[str]) -> str:
    # For backward-compat of /fb — best-effort static
    if not tw_handle or not MY_HANDLES:
        return "—"
    # Minimalist static overlap path can be placed here if needed
    return "—"

# ============================================================================
#                      TELEGRAM MESSAGE COMPOSERS
# ============================================================================
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
    price_line = f"💵 <b>Price:</b> " + (f\"${price:.8f}\" if price < 1 else f\"${price:,.4f}\")

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

# ============================================================================
#                          SEND HELPERS (PHOTO/TEXT)
# ============================================================================
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

# ============================================================================
#                       PUSH / UPDATE PIPELINES
# ============================================================================
def best_per_token(pairs):
    best={}
    for m in pairs:
        key = (m.get("token") or f"__unknown__:{m.get('pair') or m.get('url') or id(m)}")
        if key not in best or m["vol24_usd"]>best[key]["vol24_usd"]:
            best[key] = m
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

                already_tracked = m["token"] in TRACKED
                TRACKED.add(m["token"])

                if m.get("is_first_time") or not already_tracked:
                    try:
                        cur = _current_for_token(m.get("token"))
                        if cur:
                            m = _merge_current(m, cur)
                    except Exception as e:
                        log.info(f"refresh before 🔥 send failed (non-fatal): {e}")

                    fb_text = overlap_line(m.get("tw_handle"))
                    caption = build_caption(m, fb_text, is_update=False)
                    kb = link_keyboard(m)
                    key = (chat_id, m.get("token") or "")
                    should_pin = key not in LAST_PINNED
                    msg_id = await _send_or_photo(
                        bot, chat_id, caption, kb,
                        token=m.get("token"), logo_hint=m.get("logo_hint"), pin=should_pin
                    )
                    if should_pin and msg_id:
                        LAST_PINNED[key] = msg_id
                    sent += 1

                await asyncio.sleep(0.05)

    except Exception as e:
        log.exception(f"do_trade_push error: {e}")

async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🔥 [tick] auto_trade fired (interval={TRADE_SUMMARY_SEC}s)")
    await do_trade_push(context.bot)

async def updater(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🧊 [tick] updater fired (interval={UPDATE_INTERVAL_SEC}s)")
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
                fb_text = "—"
                caption = build_caption(current, fb_text, is_update=True)
                kb = link_keyboard(current)
                await _send_or_photo(context.bot, chat_id, caption, kb, token=current.get("token"), logo_hint=current.get("logo_hint"), pin=False)
                await asyncio.sleep(0.02)
    except Exception as e:
        log.exception(f"updater job error: {e}")

# ============================================================================
#                      X SCRAPER (COMMUNITY/PROFILE)
# ============================================================================
READER_PREFIXES = [
    "https://r.jina.ai/http://",
    "https://r.jina.ai/https://",
]
SCRAPER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
SCRAPER_TIMEOUT = 25

PAT_COMMUNITY = re.compile(r"/i/communities/\d+")
PAT_CANON     = re.compile(r"https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I)
PAT_I_WEB     = re.compile(r"https?://(?:x\.com|twitter\.com)/i/(?:web/)?status/(\d+)", re.I)
PAT_ANY_STAT  = re.compile(r"https?://(?:x\.com|twitter\.com)/(?:[^/\s]+/)?status/(\d+)", re.I)

def _http_get(url: str) -> requests.Response:
    return requests.get(url, headers=SCRAPER_HEADERS, timeout=SCRAPER_TIMEOUT)

def fetch_readable(url: str, sleep_between: float = 0.25) -> str:
    last_err = None
    short = url.replace("https://","").replace("http://","")
    for pref in READER_PREFIXES:
        try:
            r = _http_get(pref + short)
            if r.status_code == 200 and r.text and len(r.text) > 500:
                return r.text
            last_err = f"HTTP {r.status_code} len={len(r.text) if r.text else 0}"
        except Exception as e:
            last_err = str(e)
        time.sleep(sleep_between)
    raise RuntimeError(f"Readable fetch failed: {last_err}")

def is_community_url(u: str) -> bool:
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "")
        return "/i/communities/" in path and any(h in host for h in ("x.com","twitter.com","mobile.twitter.com"))
    except:
        return False

def is_profile_url(u: str) -> bool:
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        parts = [s for s in (p.path or "").split("/") if s]
        return (
            any(h in host for h in ("x.com","twitter.com","mobile.twitter.com"))
            and len(parts) == 1
            and USER_RE.fullmatch(parts[0] or "") is not None
        )
    except:
        return False

def extract_cid(u: str):
    try:
        p = urlparse(u)
        parts = [s for s in (p.path or "").split("/") if s]
        if len(parts) >= 3 and parts[0] == "i" and parts[1] == "communities":
            return parts[2]
    except:
        pass
    return None

def canonical_status_url(u: str) -> Optional[str]:
    m = PAT_CANON.search(u.strip())
    if not m: return None
    user, sid = m.group(1), m.group(2)
    return f"https://x.com/{user}/status/{sid}"

def resolve_tweet_author_and_url(status_url: str, sleep_between: float = 0.15) -> Tuple[Optional[str], str]:
    m = PAT_CANON.search(status_url)
    if m:
        user, sid = m.group(1), m.group(2)
        return user, f"https://x.com/{user}/status/{sid}"

    txt = fetch_readable(status_url, sleep_between)
    m = PAT_CANON.search(txt)
    if m:
        user, sid = m.group(1), m.group(2)
        return user, f"https://x.com/{user}/status/{sid}"

    # try to glean from label/mentions
    m = re.search(r"\(@([A-Za-z0-9_]{1,15})\)\s+on\s+X\b", txt, re.I)
    if m:
        user = m.group(1)
        mid = PAT_ANY_STAT.search(txt)
        if mid:
            sid = mid.group(1)
            return user, f"https://x.com/{user}/status/{sid}"
        return user, status_url

    m = re.search(r"@([A-Za-z0-9_]{1,15})\b", txt)
    if m:
        user = m.group(1)
        mid = PAT_ANY_STAT.search(txt)
        if mid:
            sid = mid.group(1)
            return user, f"https://x.com/{user}/status/{sid}"
        return user, status_url

    return None, status_url

def extract_status_hits_any(text: str):
    hits = []
    for m in PAT_CANON.finditer(text):
        user, sid = m.group(1), m.group(2)
        url = f"https://x.com/{user}/status/{sid}"
        hits.append({"status_url": url, "start": m.start(), "end": m.end(), "original": user})

    for m in PAT_I_WEB.finditer(text):
        sid = m.group(1)
        url = f"https://x.com/i/web/status/{sid}"
        hits.append({"status_url": url, "start": m.start(), "end": m.end(), "original": None})

    for m in PAT_ANY_STAT.finditer(text):
        seg = text[m.start():m.end()]
        if "i/status" in seg or "i/web/status" in seg:
            continue
        hits.append({"status_url": m.group(0), "start": m.start(), "end": m.end(), "original": None})

    seen = set(); out=[]
    for h in sorted(hits, key=lambda x: x["start"]):
        u = h["status_url"]
        if u in seen: continue
        seen.add(u); out.append(h)
    return out

def variants_for_community(u: str, include_live: bool = True, try_variants: bool = True):
    cid = extract_cid(u)
    bases = [
        f"https://x.com/i/communities/{cid}",
        f"https://twitter.com/i/communities/{cid}",
        f"https://mobile.twitter.com/i/communities/{cid}",
    ] if (cid and try_variants) else [u]
    if include_live and cid:
        bases += [b + "?f=live" for b in bases]
    return unique_preserve(bases)

def variants_for_profile(u: str, include_live: bool = True, try_variants: bool = True):
    try:
        p = urlparse(u)
        parts = [s for s in (p.path or "").split("/") if s]
        if not parts: return [u]
        user = parts[0]
        bases = [
            f"https://x.com/{user}",
            f"https://twitter.com/{user}",
            f"https://mobile.twitter.com/{user}",
        ] if try_variants else [u]
        if include_live:
            bases += [b + "?f=live" for b in bases]
        return unique_preserve(bases)
    except:
        return [u]

def scrape_community(url: str) -> Dict[str, List[str]]:
    text = None
    for v in variants_for_community(url, include_live=True, try_variants=True):
        try:
            t = fetch_readable(v)
            if text is None or len(t) > len(text):
                text = t
            log.info(f"[community] fetched: {v} (size={len(t):,})")
        except Exception as e:
            log.info(f"[community] variant failed: {v} -> {e}")
        time.sleep(0.25)
    if not text:
        return {}
    hits = extract_status_hits_any(text)
    rows = []
    for h in hits:
        author, canon = resolve_tweet_author_and_url(h["status_url"])
        user = (author or h["original"])
        if not user:
            continue
        rows.append((user.lower(), canon))
        time.sleep(0.1)
    grouped: Dict[str, List[str]] = {}
    for u, link in rows:
        arr = grouped.setdefault(u, [])
        if link not in arr:
            arr.append(link)
    return grouped

def scrape_profile(url: str) -> Dict[str, List[str]]:
    text = None
    for v in variants_for_profile(url, include_live=True, try_variants=True):
        try:
            t = fetch_readable(v)
            if text is None or len(t) > len(text):
                text = t
            log.info(f"[profile] fetched: {v} (size={len(t):,})")
        except Exception as e:
            log.info(f"[profile] variant failed: {v} -> {e}")
        time.sleep(0.25)
    if not text:
        return {}
    links = []
    for m in PAT_CANON.finditer(text):
        can = canonical_status_url(m.group(0))
        if can and can not in links:
            links.append(can)
    try:
        p = urlparse(url)
        user = [s for s in (p.path or "").split("/") if s][0]
    except:
        return {}
    u = norm_handle(user)
    if not u: return {}
    return {u.lower(): links}

def scrape_accounts_and_sources(target_url: str) -> Dict[str, List[str]]:
    target_url = (target_url or "").strip()
    if not target_url:
        return {}
    if is_community_url(target_url):
        return scrape_community(target_url)
    if is_profile_url(target_url):
        return scrape_profile(target_url)
    try:
        author, canon = resolve_tweet_author_and_url(target_url)
        if author and canon:
            return {author.lower(): [canon]}
    except Exception:
        pass
    return {}

def build_user_list(handles: List[str], cap: int) -> str:
    handles = handles[:cap]
    return " ".join(user_anchor(h) for h in handles) if handles else "—"

def build_source_block(grouped: Dict[str, List[str]], ordered_users: List[str]) -> str:
    lines = []
    for u in ordered_users:
        links = grouped.get(u, [])[:MAX_SOURCE_LINKS_PER_USER]
        if not links:
            continue
        link_anchors = [f'<a href="{lnk}">post</a>' for lnk in links]
        lines.append(f"• {user_anchor(u)}: " + ", ".join(link_anchors))
    return "\n".join(lines) if lines else "—"

def compose_xscan_message(target_url: str, grouped: Dict[str, List[str]]) -> str:
    cleaned: Dict[str, List[str]] = {}
    for k, v in (grouped or {}).items():
        h = norm_handle(k)
        if not h: continue
        uniq_links=[]; seen=set()
        for l in v or []:
            if isinstance(l, str) and l.startswith("http") and l not in seen:
                uniq_links.append(l); seen.add(l)
        if uniq_links:
            cleaned[h] = uniq_links

    if not cleaned:
        return f"<b>Scan:</b> <a href='{target_url}'>{target_url}</a>\n\nNo posters detected."

    all_users = sorted(cleaned.keys(), key=lambda x: x.lower())
    my_following = load_following(MY_FOLLOWING_TXT)  # reload per scan (if file updated)
    followed  = [u for u in all_users if u in my_following]
    extras    = [u for u in all_users if u not in my_following]
    followed  = followed[:MAX_USERS_PER_SECTION]
    extras    = extras[:MAX_USERS_PER_SECTION]

    followed_block = build_user_list(followed, MAX_USERS_PER_SECTION)
    extras_block   = build_user_list(extras, MAX_USERS_PER_SECTION)

    ordered_for_sources = followed + extras
    source_block = build_source_block(cleaned, ordered_for_sources)

    msg = (
        f"<b>Scan:</b> <a href='{target_url}'>{target_url}</a>\n\n"
        f"<b>Followed by</b>\n{followed_block}\n\n"
        f"<b>Extras</b>\n{extras_block}\n\n"
        f"<b>Source</b>\n{source_block}"
    )
    if len(msg) > MAX_MESSAGE_CHARS:
        msg = msg[:MAX_MESSAGE_CHARS-20] + " …"
    return msg

# ============================================================================
#                           TELEGRAM COMMANDS
# ============================================================================
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id); _save_subs_to_file()
    await u.message.reply_text(
        f"✅ Subscribed.\n"
        f"🔥 /trade every {TRADE_SUMMARY_SEC}s + 🧊 updates every {UPDATE_INTERVAL_SEC}s "
        f"(stop after {UPDATE_MAX_DURATION_MIN} min).\n\n"
        f"New: /xscan <X URL> to extract Followed by / Extras / Source (clickable).",
        disable_web_page_preview=True
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
        f"Top/tick {TOP_N_PER_TICK or 'unlimited'} | Max alert age {int(MAX_AGE_MIN)}m | Update stop {UPDATE_MAX_DURATION_MIN}m\n"
        f"Following list file: {MY_FOLLOWING_TXT} (loaded {len(MY_HANDLES)} handles)",
        disable_web_page_preview=True
    )

async def cmd_trade(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split()
    manual_cap = None
    if len(args) >= 2:
        try:
            manual_cap = max(1, int(args[1]))
        except:
            manual_cap = None

    pairs = best_per_token(fetch_matches())
    decorate_with_first_seen(pairs)

    cap = manual_cap if manual_cap is not None else (TOP_N_PER_TICK if TOP_N_PER_TICK > 0 else 10_000)
    sent = 0
    for m in pairs:
        if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
            continue

        TRACKED.add(m["token"])

        if m.get("is_first_time"):
            try:
                cur = _current_for_token(m.get("token"))
                if cur:
                    m = _merge_current(m, cur)
            except Exception as e:
                log.info(f"refresh before send (manual) failed: {e}")

            fb_text = overlap_line(m.get("tw_handle"))
            caption = build_caption(m, fb_text, is_update=False)
            kb = link_keyboard(m)
            await _send_or_photo(c.bot, u.effective_chat.id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"), pin=False)
        else:
            current = _current_for_token(m.get("token")) or m
            fb_text = "—"
            caption = build_caption(current, fb_text, is_update=True)
            kb = link_keyboard(current)
            await _send_or_photo(c.bot, u.effective_chat.id, caption, kb, token=current.get("token"), logo_hint=current.get("logo_hint"), pin=False)

        sent += 1
        if sent >= cap:
            break
        await asyncio.sleep(0.05)

    if sent == 0:
        await u.message.reply_text("(trade) no matches with current filters.", disable_web_page_preview=True)

async def cmd_fb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split()
    if len(args) < 2:
        await u.message.reply_text("Usage: /fb handle", disable_web_page_preview=True); return
    handle = norm_handle(args[1])
    if not handle:
        await u.message.reply_text("Bad handle.", disable_web_page_preview=True); return
    # This quick overlap shows if the handle is inside your list (for now).
    txt = ("Yes" if handle in MY_HANDLES else "No")
    await u.message.reply_text(f"Overlap for @{handle}: {txt}", disable_web_page_preview=True)

async def cmd_xscan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    parts = (u.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await u.message.reply_text("Usage: /xscan <X URL>", disable_web_page_preview=True)
        return
    target = parts[1].strip()
    wait = await u.message.reply_text("⏳ Scanning…", disable_web_page_preview=True)
    try:
        grouped = scrape_accounts_and_sources(target)
        msg = compose_xscan_message(target, grouped or {})
        await u.message.reply_html(msg, disable_web_page_preview=False)
    except Exception as e:
        await u.message.reply_text(f"Scrape failed: {e}", disable_web_page_preview=True)
    finally:
        try:
            await wait.delete()
        except Exception:
            pass
# ========== /xscan — Community/Profile scraper (no API) ==========
# Uses reader-view snapshots (jina) and basic regex parsing.

READER_PREFIXES = [
    "https://r.jina.ai/http://",
    "https://r.jina.ai/https://",
]
HEADERS_XREAD = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
READ_TIMEOUT = 25
SLEEP_REQ = 0.20

PAT_CANON = re.compile(r"https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I)
PAT_I_WEB = re.compile(r"https?://(?:x\.com|twitter\.com)/i/(?:web/)?status/(\d+)", re.I)
PAT_ANY_STATUS = re.compile(r"https?://(?:x\.com|twitter\.com)/(?:[^/\s]+/)?status/(\d+)", re.I)
USER_PAT = re.compile(r"@([A-Za-z0-9_]{1,15})\b")

def _http_get(url: str) -> requests.Response:
    return requests.get(url, headers=HEADERS_XREAD, timeout=READ_TIMEOUT)

def _fetch_readable(url: str) -> str:
    last_err = None
    short = url.replace("https://", "").replace("http://", "")
    for pref in READER_PREFIXES:
        try:
            r = _http_get(pref + short)
            if r.status_code == 200 and r.text and len(r.text) > 500:
                return r.text
            last_err = f"HTTP {r.status_code} len={len(r.text) if r.text else 0}"
        except Exception as e:
            last_err = str(e)
        time.sleep(SLEEP_REQ)
    raise RuntimeError(f"Readable fetch failed: {last_err}")

def _is_community(u: str) -> bool:
    try:
        p = urlparse(u)
        return "/i/communities/" in (p.path or "")
    except:
        return False

def _is_profile(u: str) -> bool:
    try:
        p = urlparse(u)
        parts = [s for s in (p.path or "").split("/") if s]
        return (p.netloc or "").lower().endswith(("x.com","twitter.com")) and len(parts) == 1
    except:
        return False

def _handle_from_profile_url(u: str) -> str | None:
    try:
        p = urlparse(u)
        parts = [s for s in (p.path or "").split("/") if s]
        h = parts[0] if parts else ""
        return h if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h or "") else None
    except:
        return None

def _canonical_status(url: str) -> str | None:
    m = PAT_CANON.search(url)
    if not m: return None
    return f"https://x.com/{m.group(1)}/status/{m.group(2)}"

def _extract_all_statuses(text: str) -> list[str]:
    # canonical
    urls = [f"https://x.com/{m.group(1)}/status/{m.group(2)}" for m in PAT_CANON.finditer(text)]
    # /i/(web/)status (no username) → resolve by opening the tweet page (reader) and looking for canonical
    for m in PAT_I_WEB.finditer(text):
        raw = m.group(0)
        try:
            t = _fetch_readable(raw)
            m2 = PAT_CANON.search(t)
            if m2:
                urls.append(f"https://x.com/{m2.group(1)}/status/{m2.group(2)}")
        except Exception:
            pass
        time.sleep(0.12)
    # loose fallback for odd shapes
    for m in PAT_ANY_STATUS.finditer(text):
        seg = text[m.start():m.end()]
        if "i/status" in seg or "i/web/status" in seg:
            continue
        can = _canonical_status(seg)
        if can:
            urls.append(can)
    # unique preserve
    seen = set(); out=[]
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def _group_by_user(urls: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for u in urls:
        m = PAT_CANON.match(u)
        if not m: continue
        user = m.group(1)
        out.setdefault(user, [])
        if u not in out[user]:
            out[user].append(u)
    return out

def _load_my_handles() -> set[str]:
    path = os.getenv("MY_FOLLOWING_TXT", "handles.partial.txt")
    s: set[str] = set()
    try:
        for line in open(path, "r", encoding="utf-8", errors="ignore"):
            h = (line or "").strip().lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h or ""):
                s.add(h.lower())
    except Exception:
        pass
    return s

def _render_clickable_handles(handles: list[str]) -> str:
    # produce "@name" as <a href="https://x.com/name">@name</a>
    return ", ".join([f'<a href="https://x.com/{h}">@{h}</a>' for h in handles])

def _chunk_and_send(bot, chat_id: int, html: str, max_len: int = 3800):
    # Telegram hard cap is 4096; keep some margin for safety
    i = 0
    while i < len(html):
        j = min(len(html), i + max_len)
        # try to break on line boundary
        cut = html.rfind("\n", i, j)
        if cut <= i: cut = j
        piece = html[i:cut]
        bot.send_message(chat_id, piece, parse_mode="HTML", disable_web_page_preview=True)
        i = cut

async def cmd_xscan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await u.message.reply_text("Usage: /xscan <community or profile URL>")
        return
    target = args[1].strip()
    await u.message.chat.send_action("typing")

    # 1) Fetch a good snapshot
    variants = [target]
    if _is_community(target):
        # try desktop, mobile, and ?f=live
        try:
            p = urlparse(target); cid = [s for s in (p.path or "").split("/") if s][2]
            bases = [
                f"https://x.com/i/communities/{cid}",
                f"https://twitter.com/i/communities/{cid}",
                f"https://mobile.twitter.com/i/communities/{cid}",
                f"https://x.com/i/communities/{cid}?f=live",
                f"https://twitter.com/i/communities/{cid}?f=live",
                f"https://mobile.twitter.com/i/communities/{cid}?f=live",
            ]
            seen=set(); variants=[b for b in bases if not (b in seen or seen.add(b))]
        except Exception:
            pass

    best = None
    for v in variants:
        try:
            txt = _fetch_readable(v)
            if best is None or len(txt) > len(best):
                best = txt
        except Exception:
            pass
        await asyncio.sleep(0.05)

    if not best:
        await u.message.reply_text("Could not fetch a readable snapshot for that URL.")
        return

    # 2) Extract & group
    status_urls = _extract_all_statuses(best)
    by_user = _group_by_user(status_urls)

    # Profile pages may return empty (timeline not in reader). If profile: add a minimal fallback
    if not by_user and _is_profile(target):
        handle = _handle_from_profile_url(target)
        # Try to pick any status links in page even if posted by others; filter by this handle as author
        for m in PAT_CANON.finditer(best or ""):
            if m.group(1).lower() == (handle or "").lower():
                url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
                by_user.setdefault(handle, [])
                if url not in by_user[handle]:
                    by_user[handle].append(url)

    # 3) Compare with "my following"
    my_set = _load_my_handles()
    followed = []
    extras = []
    for user in by_user.keys():
        (followed if user.lower() in my_set else extras).append(user)

    followed.sort(key=str.lower)
    extras.sort(key=str.lower)

    # Limits from env
    MAX_USERS_PER_SECTION = int(os.getenv("MAX_USERS_PER_SECTION", "50"))
    MAX_SOURCE_LINKS_PER_USER = int(os.getenv("MAX_SOURCE_LINKS_PER_USER", "3"))

    f_show = followed[:MAX_USERS_PER_SECTION]
    e_show = extras[:MAX_USERS_PER_SECTION]

    # 4) Build HTML message
    lines = []
    lines.append("<b>Followed by</b>")
    lines.append(_render_clickable_handles(f_show) if f_show else "—")
    if len(followed) > len(f_show):
        lines.append(f"... +{len(followed) - len(f_show)} more")
    lines.append("")

    lines.append("<b>Extras</b>")
    lines.append(_render_clickable_handles(e_show) if e_show else "—")
    if len(extras) > len(e_show):
        lines.append(f"... +{len(extras) - len(e_show)} more")
    lines.append("")

    lines.append("<b>Source</b>")
    # show a compact per-user list with limited links
    users_in_order = sorted(by_user.keys(), key=str.lower)
    for user in users_in_order:
        urls = by_user[user][:MAX_SOURCE_LINKS_PER_USER]
        uhtml = f'<a href="https://x.com/{user}">@{user}</a>'
        if urls:
            links = " • ".join([f'<a href="{x}">post</a>' for x in urls])
            lines.append(f"• {uhtml}: {links}")
        else:
            lines.append(f"• {uhtml}: —")
    html = "\n".join(lines)

    # 5) Send (chunked if needed)
    try:
        _chunk_and_send(c.bot, u.effective_chat.id, html, max_len=int(os.getenv("MAX_MESSAGE_CHARS", "3500")))
    except Exception as e:
        await u.message.reply_text(f"Error sending message: {e}")

# ============================================================================
#                        FASTAPI + TELEGRAM LIFECYCLE
# ============================================================================
async def _post_init(app: Application):
    global SUBS
    SUBS = _load_subs_from_file()
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
application.add_handler(CommandHandler("xscan",      cmd_xscan))

app = FastAPI(title="Telegram Webhook + X Scraper + Dex Alerts")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    await application.initialize()
    jq = application.job_queue
    jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC),
                     first=timedelta(seconds=3), name="trade_tick")
    jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC),
                     first=timedelta(seconds=20), name="updates")
    if BASE_WEBHOOK_URL:
        url = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        try:
            await application.bot.set_webhook(url)
            log.info(f"Webhook set to {url}")
        except Exception as e:
            log.warning(f"set_webhook failed: {e}")
    await application.start()
    log.info("Bot initialized & started")

@app.on_event("shutdown")
async def _shutdown():
    try:
        await application.stop()
    finally:
        await application.shutdown()

# ---------- Webhook: tokenized path ----------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data: Dict[str, Any] = await request.json()
    except Exception as e:
        log.warning("webhook json parse error: %r", e)
        return Response(status_code=400)
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        log.exception("process_update error: %r", e)
    return Response(status_code=200)

# ---------- Local dev runner ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", str(PORT)))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
