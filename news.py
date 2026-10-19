# -*- coding: utf-8 -*-
"""국제정세 뉴스 수집 — Google News RSS(한/영) + 네이버 뉴스 API(선택)."""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from config import KST

UA = {"User-Agent": "Mozilla/5.0 (news-signal-engine)"}

# 한국 시장에 전이되는 국제정세 축 위주. when:Nh 는 Google News 서버측 시간 필터.
KO_QUERIES = [
    "국제정세", "전쟁 OR 공습 OR 휴전", "관세 OR 무역협상 OR 수출규제", "제재 러시아 OR 이란 OR 중국",
    "연준 금리 OR FOMC", "유가 OR OPEC OR 호르무즈", "북한 미사일 OR 북한 도발",
    "중국 경기부양 OR 중국 규제", "반도체 수출통제 OR 엔비디아", "환율 급등 OR 달러 강세",
]
EN_QUERIES = [
    "geopolitics", "war OR airstrike OR ceasefire", "tariff OR trade deal OR export controls",
    "sanctions Russia OR Iran OR China", "Federal Reserve rate", "oil prices OPEC",
    "Taiwan Strait OR South China Sea", "chip export restrictions",
]
EN_TOPICS = [
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
]


def _gn_search(q, lang, hours):
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(q + f' when:{hours}h')}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(q + f' when:{hours}h')}&hl=en-US&gl=US&ceid=US:en"


def _parse_rss(xml_text, lang):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except Exception:
            ts = None
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        if title:
            out.append({"title": html.unescape(title), "link": link, "source": src, "ts": ts, "lang": lang})
    return out


def _naver(cfg, hours):
    cid, sec = cfg.get("NAVER_CLIENT_ID"), cfg.get("NAVER_CLIENT_SECRET")
    if not (cid and sec):
        return []
    out = []
    for q in ["국제정세", "전쟁", "관세", "제재", "유가", "연준 금리", "북한"]:
        try:
            r = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                params={"query": q, "display": 30, "sort": "date"},
                headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}, timeout=10)
            r.raise_for_status()
            for it in r.json().get("items", []):
                ts = parsedate_to_datetime(it["pubDate"]).astimezone(timezone.utc)
                title = html.unescape(re.sub(r"<[^>]+>", "", it["title"]))
                out.append({"title": title, "link": it.get("originallink") or it["link"],
                            "source": "naver", "ts": ts, "lang": "ko"})
        except Exception as e:
            print(f"[news] naver '{q}' 실패: {e}")
    return out


def _norm(t):
    return re.sub(r"[\W_]+", "", t.lower())[:60]


def news_hash(item):
    return hashlib.sha1(_norm(item["title"]).encode()).hexdigest()[:12]


def collect(cfg, seen_hashes):
    hours = cfg["NEWS_LOOKBACK_HOURS"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    urls = [(_gn_search(q, "ko", hours), "ko") for q in KO_QUERIES]
    urls += [(_gn_search(q, "en", hours), "en") for q in EN_QUERIES]
    urls += [(u, "en") for u in EN_TOPICS]
    buckets = []  # 쿼리(축)별 묶음 → 라운드로빈 선별로 특정 축 쏠림 방지
    for u, lang in urls:
        try:
            r = requests.get(u, headers=UA, timeout=15)
            r.raise_for_status()
            buckets.append(_parse_rss(r.text, lang))
        except Exception as e:
            print(f"[news] RSS 실패 {u[:80]}: {e}")
    buckets.append(_naver(cfg, hours))

    batch_seen = set()
    clean = []
    for b in buckets:
        keep = []
        for it in sorted(b, key=lambda x: x["ts"] or cutoff, reverse=True):
            if it["ts"] is None or it["ts"] < cutoff:
                continue
            h = news_hash(it)
            if h in seen_hashes or h in batch_seen:
                continue
            batch_seen.add(h)
            it["hash"] = h
            keep.append(it)
        clean.append(keep)
    uniq, depth = [], 0
    while len(uniq) < cfg["MAX_HEADLINES"] and any(depth < len(b) for b in clean):
        for b in clean:
            if depth < len(b) and len(uniq) < cfg["MAX_HEADLINES"]:
                uniq.append(b[depth])
        depth += 1
    uniq.sort(key=lambda x: x["ts"], reverse=True)
    for i, it in enumerate(uniq):
        it["id"] = f"n{i}"
        it["ts_kst"] = it["ts"].astimezone(KST).strftime("%m-%d %H:%M")
    return uniq
