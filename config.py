# -*- coding: utf-8 -*-
"""설정 로더 — GitHub Secret 'CONFIG_JSON' 1개(FOOD CALL ENGINE 방식) 또는 개별 환경변수."""
import json
import os
from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))

_DEFAULTS = {
    "SHEET_ID": "",                 # 판정용 구글시트 ID (URL의 /d/<여기>/edit) — 가볍게 유지
    "ARCHIVE_SHEET_ID": "",         # 뉴스원문 보관용 별도 시트 ID(월별 탭 자동). 비우면 판정 시트에 월별 탭으로 저장
    "GCP_SA_JSON": "",              # 서비스계정 JSON 전체 (문자열 또는 dict)
    "LLM_PROVIDERS": "anthropic",   # 쉼표 구분. 첫 번째 = 주(알림 발송), 나머지 = 그림자(기록만). 'vendor:model' 형식 가능. 예: "anthropic:claude-opus-5-5,anthropic:claude-sonnet-5-5"
    "LLM_TEMPERATURE": 0,
    "SCREENER_PROVIDER": "gemini",  # 1차 선별(저가/무료 모델): 시장 무관 헤드라인 제거 → 본 판단 입력 토큰 70~90% 절감. "" = 비활성
    "ANTHROPIC_SCREENER_MODEL": "claude-haiku-5-5",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "claude-sonnet-5-5",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-3.5-flash",
    "NAVER_CLIENT_ID": "",          # 선택: 네이버 뉴스 API 보강
    "NAVER_CLIENT_SECRET": "",
    "TELEGRAM_BOT_TOKEN": "",       # 선택: 알림
    "TELEGRAM_CHAT_ID": "",
    "PRIVATE_PROMPT": "",           # 판단 원칙 전문 — 별도 Secret 권장(저장소 비공개 유지)
    "DART_API_KEY": "",             # 선택: DART 주요사항보고서(자사주·증자·CB) 이벤트 신호(H9 표본 외 추적). 비우면 비활성
    "NEWS_LOOKBACK_HOURS": 2,       # 매시간 실행 + 2시간 창 (누락 방지, 중복은 해시로 제거)
    "MAX_HEADLINES": 150,   # LLM 1회 입력 상한(라운드로빈 선별, 미선별분은 다음 시간에 재기회)
    "MIN_MATERIALITY": 3,           # 1~5, 이 미만 사건은 기록 안 함
    "MEASURE_HOUR_KST": 18,         # 측정은 하루 1회 (장 마감 후)
    "ALERT_MIN_CONFIDENCE": 4,      # 텔레그램 알림 기준
    "API_MONTHLY_BUDGET_USD": 100,  # 월 API 예산(Max 플랜 포함 크레딧). 월말 추정이 80% 넘으면 일일요약에 경고
    # $/백만 토큰 [입력, 출력] — 2026-10 공식 가격표(platform.claude.com/docs/en/about-claude/pricing). 바뀌면 CONFIG_JSON에서 덮어쓰기
    "PRICES_PER_MTOK": {"claude-opus-5-5": [4, 20], "claude-sonnet-5-5": [2, 10], "claude-haiku-5-5": [0.1, 0.5]},
}


def load_config():
    cfg = dict(_DEFAULTS)
    raw = os.environ.get("CONFIG_JSON", "").strip()
    if raw:
        cfg.update(json.loads(raw))
    for k in _DEFAULTS:
        v = os.environ.get(k)
        if v not in (None, ""):
            cfg[k] = v
    for k in ("NEWS_LOOKBACK_HOURS", "MAX_HEADLINES", "MIN_MATERIALITY", "MEASURE_HOUR_KST", "ALERT_MIN_CONFIDENCE"):
        cfg[k] = int(cfg[k])
    cfg["LLM_TEMPERATURE"] = float(cfg["LLM_TEMPERATURE"])
    cfg["API_MONTHLY_BUDGET_USD"] = float(cfg["API_MONTHLY_BUDGET_USD"])
    if isinstance(cfg["PRICES_PER_MTOK"], str):
        cfg["PRICES_PER_MTOK"] = json.loads(cfg["PRICES_PER_MTOK"])
    cfg["PROVIDERS"] = [p.strip().lower() for p in str(cfg["LLM_PROVIDERS"]).split(",") if p.strip()]
    if isinstance(cfg["GCP_SA_JSON"], str) and cfg["GCP_SA_JSON"].strip():
        cfg["GCP_SA_JSON"] = json.loads(cfg["GCP_SA_JSON"])
    return cfg
