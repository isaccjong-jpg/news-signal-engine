# -*- coding: utf-8 -*-
"""국내외 정세·산업·시장 뉴스 수집 — Google News RSS(한/영) + 네이버 뉴스 API(선택)."""
import hashlib
import json
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
    # 거시·지정학
    "국제정세", "전쟁 OR 공습 OR 휴전", "관세 OR 무역협상 OR 수출규제", "제재 러시아 OR 이란 OR 중국",
    "연준 금리 OR FOMC", "유가 OR OPEC OR 호르무즈", "북한 미사일 OR 북한 도발",
    "중국 경기부양 OR 중국 규제", "환율 급등 OR 달러 강세",
    # 업종·산업 (한국 유니버스 직결)
    "반도체 HBM OR 메모리 가격 OR 수출통제", "조선 수주 OR LNG선", "석유화학 구조조정 OR 화학 스프레드",
    "2차전지 OR 배터리 수주 OR 전기차 수요", "방산 수출 OR K방산", "원전 수주 OR SMR",
    "자동차 관세 OR 완성차 판매", "해운 운임 OR 컨테이너 운임", "철강 반덤핑 OR 철강 관세",
    "바이오 FDA 승인 OR 임상 결과", "특허 소송 OR ITC 판정", "중국 소비 OR 면세 OR 화장품 수출",
    # 제약·바이오 정책
    "기술수출 OR 라이선스 아웃 OR 비만치료제", "의약품 관세 OR 약가 인하 OR 생물보안법",
    # 원자재
    "구리 가격 OR 니켈 OR LME", "리튬 가격 OR 희토류 OR 핵심광물", "금값 OR 은값", "곡물 가격 OR 밀 OR 옥수수", "천연가스 OR LNG 가격",
    # 국내 정책·실적·수급
    "한국은행 기준금리", "밸류업 OR 상법 개정 OR 배당소득 분리과세 OR 공매도", "잠정실적 OR 어닝 서프라이즈 OR 어닝 쇼크",
    "외국인 순매도 OR 외국인 순매수 코스피", "AI 데이터센터 전력 OR 전력망 투자",
    # 해외 증시(맥락용)
    "뉴욕증시 마감 OR 필라델피아 반도체지수",
]
EN_QUERIES = [
    "geopolitics", "war OR airstrike OR ceasefire", "tariff OR trade deal OR export controls",
    "sanctions Russia OR Iran OR China", "Federal Reserve rate", "oil prices OPEC",
    "Taiwan Strait OR South China Sea", "chip export restrictions",
    "TSMC OR Nvidia OR Micron earnings", "HBM OR DRAM prices", "shipbuilding orders LNG carrier",
    "EV battery demand OR lithium prices", "FDA approval", "patent lawsuit ITC",
    "stock market close Nasdaq semiconductor index",
    "copper prices OR LME nickel", "gold price OR silver price", "rare earths OR critical minerals",
    "pharmaceutical tariffs OR drug pricing OR BIOSECURE", "biotech licensing deal OR obesity drug",
    "hyperscaler capex OR data center power", "Bank of Japan OR yen", "China economic data OR PBOC",
]
# 네이버: 국내 매체 커버리지가 강한 업종·정책 축 위주 (일 25,000회 한도 대비 하루 ~400회)
NAVER_QUERIES = ["국제정세", "관세", "제재", "유가", "연준 금리", "북한", "반도체", "조선 수주", "방산 수출",
                 "2차전지", "원전", "석유화학", "제약 기술수출", "임상", "원자재 가격", "한국은행 금리", "밸류업",
                 "잠정실적"]
EN_TOPICS = [
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
]


def query_fingerprint():
    """수집 축(쿼리 세트) 해시 — 바뀌면 입력 분포가 바뀌므로 실험 버전에 포함."""
    import hashlib
    return hashlib.sha1(json.dumps([KO_QUERIES, EN_QUERIES, EN_TOPICS, NAVER_QUERIES], ensure_ascii=False).encode()).hexdigest()[:6]


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
    for q in NAVER_QUERIES:
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
