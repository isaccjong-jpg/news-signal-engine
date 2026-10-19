# -*- coding: utf-8 -*-
"""사건 분류·영향 판단 — LLM(Anthropic/Gemini) + 출력 검증(유니버스 밖 코드·형식 오류 차단)."""
import json
import re

import requests

CATEGORIES = [
    "지정학_전쟁", "지정학_제재", "무역_관세", "통화정책_금리", "원자재_에너지",
    "공급망_기술통제", "중국_정책", "북한", "재난_보건", "정치_선거", "기타",
]

# 판단 원칙(=전략의 핵심)은 저장소에 두지 않는다 → Secret PRIVATE_PROMPT 또는 private/prompt.txt(.gitignore)
DEFAULT_PRINCIPLES = "너는 한국 주식시장 이벤트 분석가다. 뉴스 헤드라인에서 한국 상장 종목에 영향을 줄 사건을 구조화한다. 영향 종목은 UNIVERSE 목록의 code만 사용한다."

OUTPUT_SCHEMA = """반드시 JSON만 출력:
{"events":[{"event_key":"영문대문자_스네이크_핵심어","is_new":true,"category":"<CATEGORIES 중 하나>","summary":"한국어 1문장","news_ids":["n0"],"materiality":3,"horizon_days":5,"impacts":[{"code":"000000","direction":"+","confidence":3,"rationale":"한국어 1문장 인과"}]}]}"""


def _build_user(headlines, recent_events, universe, returns=None):
    """returns: {code: (5d%, 20d%)} — 최근 수익률. '이미 반영됐는지(과소/과대반응)' 판단 근거로 제공."""
    returns = returns or {}
    uni_lines = []
    for sec, rows in universe["sectors"].items():
        parts = []
        for r in rows:
            rt = returns.get(r["code"])
            parts.append(f"{r['code']} {r['name']}" + (f"(5d {rt[0]:+.1f}%/20d {rt[1]:+.1f}%)" if rt else ""))
        uni_lines.append(f"[{sec}] " + ", ".join(parts))
    hl = "\n".join(f"{h['id']} | {h['ts_kst']} KST | {h['source']} | {h['title']}" for h in headlines)
    rec = "\n".join(f"- {e['event_key']}: {e['summary']}" for e in recent_events) or "(없음)"
    note = "\n(괄호 안은 최근 5/20영업일 수익률. 사건이 이미 가격에 반영됐는지 판단에 사용)" if returns else ""
    return (f"CATEGORIES: {', '.join(CATEGORIES)}\n\nUNIVERSE:{note}\n" + "\n".join(uni_lines) +
            f"\n\nRECENT_EVENTS (최근 72시간 기록):\n{rec}\n\nHEADLINES:\n{hl}")


def _system_prompt(cfg):
    from pathlib import Path
    p = cfg.get("PRIVATE_PROMPT") or ""
    f = Path(__file__).parent / "private" / "prompt.txt"
    if not p and f.exists():
        p = f.read_text(encoding="utf-8")
    if not p:
        import os
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError("Secret PRIVATE_PROMPT 미등록 — 기본 원칙으로 돌리면 검증 데이터 오염되므로 중단")
        print("[classifier] 경고: 비공개 판단원칙 없음 → 기본 원칙으로 실행(로컬 전용)")
        p = DEFAULT_PRINCIPLES
    return p.strip() + "\n" + OUTPUT_SCHEMA


def _raise_with_api_message(r, vendor):
    """HTTP 오류 시 API가 준 오류 메시지(키 미포함)를 예외에 실어 시트 실행로그에서 원인을 바로 볼 수 있게."""
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"{vendor} HTTP {r.status_code}: {str(msg)[:200]}")


def _call_anthropic(cfg, system, user):
    # Claude 5.5 계열은 temperature 파라미터를 거부함("deprecated for this model") → 보내지 않는다(모델 기본값).
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["ANTHROPIC_MODEL"], "max_tokens": 4000,
              "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=120)
    _raise_with_api_message(r, "anthropic")
    return "".join(b.get("text", "") for b in r.json()["content"]), cfg["ANTHROPIC_MODEL"]


def _call_gemini(cfg, system, user):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['GEMINI_MODEL']}:generateContent"
    r = requests.post(
        url, headers={"x-goog-api-key": cfg["GEMINI_API_KEY"]},  # URL에 키 노출 금지(공개 로그 대비)
        json={"systemInstruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"temperature": cfg["LLM_TEMPERATURE"], "responseMimeType": "application/json"}},
        timeout=120)
    _raise_with_api_message(r, "gemini")
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts), cfg["GEMINI_MODEL"]


def _extract_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"LLM 출력에 JSON 없음 (길이 {len(text)})")  # 공개 로그에 판단 내용 노출 금지
    return json.loads(m.group(0))


def validate(raw, headlines, universe, recent_keys, min_materiality):
    """LLM 출력 → 검증된 사건 리스트. 유니버스 밖 코드/형식 오류/기존 사건 중복은 제거."""
    names = {r["code"]: r["name"] for rows in universe["sectors"].values() for r in rows}
    hl_by_id = {h["id"]: h for h in headlines}
    out, dropped = [], []
    for ev in raw.get("events", []):
        key = re.sub(r"[^A-Z0-9_]", "", str(ev.get("event_key", "")).upper())[:60]
        if not key or not ev.get("is_new", True) or key in recent_keys:
            dropped.append(f"중복/후속:{key}")
            continue
        try:
            mat = int(ev.get("materiality", 0))
            hz = max(1, min(20, int(ev.get("horizon_days", 5))))
        except (TypeError, ValueError):
            dropped.append(f"형식:{key}")
            continue
        if mat < min_materiality:
            dropped.append(f"중요도{mat}:{key}")
            continue
        cat = ev.get("category") if ev.get("category") in CATEGORIES else "기타"
        imps, seen = [], set()
        for im in ev.get("impacts", [])[:5]:
            code = str(im.get("code", "")).strip().zfill(6)
            d = str(im.get("direction", "")).strip()
            if code not in names or d not in ("+", "-") or code in seen:
                dropped.append(f"종목무효:{key}:{code}")
                continue
            try:
                conf = max(1, min(5, int(im.get("confidence", 1))))
            except (TypeError, ValueError):
                conf = 1
            seen.add(code)
            imps.append({"code": code, "name": names[code], "direction": d, "confidence": conf,
                         "rationale": str(im.get("rationale", ""))[:200]})
        if not imps:
            dropped.append(f"종목없음:{key}")
            continue
        news = [hl_by_id[i] for i in ev.get("news_ids", []) if i in hl_by_id]
        out.append({"event_key": key, "category": cat, "summary": str(ev.get("summary", ""))[:300],
                    "materiality": mat, "horizon_days": hz, "impacts": imps, "news": news})
        recent_keys.add(key)
    return out, dropped


SCREEN_SYSTEM = """너는 뉴스 1차 선별기다. 헤드라인 목록에서 '한국 주식시장(업종·종목·환율·금리·원자재)에 영향을 줄 가능성이 조금이라도 있는 국제정세·경제 뉴스'의 id만 고른다.
- 재현율 우선: 애매하면 남긴다. 확실히 무관한 것(연예·스포츠·게임·지역 사건·생활정보·단순 시세 중계·논평)만 제거.
- 같은 사건의 중복 헤드라인은 가장 정보가 많은 1~2개만 남긴다.
- JSON만 출력: {"keep":["n0","n5"]}"""


def screen(cfg, headlines):
    """1차 선별. 실패 시 예외 → 호출측이 fail-open(전체 통과)."""
    prov = (cfg.get("SCREENER_PROVIDER") or "").lower()
    if not prov or not headlines:
        return headlines, "-"
    user = "HEADLINES:\n" + "\n".join(f"{h['id']} | {h['source']} | {h['title']}" for h in headlines)
    if prov == "gemini":
        text, model = _call_gemini(cfg, SCREEN_SYSTEM, user)
    elif prov == "anthropic":
        c2 = dict(cfg); c2["ANTHROPIC_MODEL"] = cfg["ANTHROPIC_SCREENER_MODEL"]
        text, model = _call_anthropic(c2, SCREEN_SYSTEM, user)
    else:
        raise ValueError(f"screener 미지원: {prov}")
    try:
        keep = set(str(x) for x in _extract_json(text).get("keep", []))
    except (json.JSONDecodeError, ValueError):
        raise ValueError("screener JSON 파싱 실패") from None
    return [h for h in headlines if h["id"] in keep], model


def screener_id(cfg):
    prov = (cfg.get("SCREENER_PROVIDER") or "").lower()
    if not prov:
        return "none"
    return cfg["GEMINI_MODEL"] if prov == "gemini" else cfg["ANTHROPIC_SCREENER_MODEL"]


def parse_provider(cfg, provider):
    """'anthropic' | 'gemini' | 'anthropic:claude-opus-5-5' → (vendor, model). 같은 벤더의 여러 모델을 주/그림자로 동시 운용 가능."""
    vendor, _, model = provider.partition(":")
    vendor = vendor.strip().lower()
    if vendor not in ("anthropic", "gemini"):
        raise ValueError(f"LLM provider 미지원: {provider}")
    return vendor, (model.strip() or (cfg["GEMINI_MODEL"] if vendor == "gemini" else cfg["ANTHROPIC_MODEL"]))


def model_id(cfg, provider):
    return parse_provider(cfg, provider)[1]


def provider_tag(cfg, provider):
    """신호ID·버전 접두: 벤더 3자 + 모델 패밀리 (ant-opus / ant-sonn / gem-3.5f)."""
    vendor, model = parse_provider(cfg, provider)
    fam = model.replace("claude-", "").replace("gemini-", "").split("-")[0][:4]
    return f"{vendor[:3]}{fam}"


def prompt_version(cfg, universe, provider):
    """실험 버전 = 판단원칙 + 유니버스 + 모델ID + temperature + 스크리너 모델 해시.
    하나라도 바뀌면 새 실험(섞어 집계 금지). 모델ID는 날짜 고정 스냅샷 ID 사용 권장 — 같은 이름이라도 업데이트되면 다른 모델."""
    import hashlib
    body = (_system_prompt(cfg) + json.dumps(universe, sort_keys=True, ensure_ascii=False)
            + f"|{model_id(cfg, provider)}|{cfg['LLM_TEMPERATURE']}|screen={screener_id(cfg)}")
    return f"{provider_tag(cfg, provider)}-{hashlib.sha1(body.encode()).hexdigest()[:8]}"


def classify(cfg, headlines, recent_events, universe, returns=None, provider="anthropic"):
    if not headlines:
        return [], [], "-"
    user = _build_user(headlines, recent_events, universe, returns)
    vendor, model = parse_provider(cfg, provider)
    c2 = dict(cfg)
    c2["GEMINI_MODEL" if vendor == "gemini" else "ANTHROPIC_MODEL"] = model
    if vendor == "gemini":
        text, model = _call_gemini(c2, _system_prompt(cfg), user)
    else:
        text, model = _call_anthropic(c2, _system_prompt(cfg), user)
    try:
        raw = _extract_json(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM JSON 파싱 실패 (pos {e.pos})") from None
    recent_keys = {e["event_key"] for e in recent_events}
    events, dropped = validate(raw, headlines, universe, recent_keys, cfg["MIN_MATERIALITY"])
    return events, dropped, model
