# -*- coding: utf-8 -*-
"""DART 주요사항보고서 → 기업 행동 이벤트 신호 (H9 표본 외 추적). LLM 없음, 비용 0.

백테스트(QUANT/event_backtest, 2015~2026)에서 자사주 취득 공시 뒤 20일 β조정 초과 +1.3~1.5%(t 2.5~3.6),
유상증자·CB·자사주처분 공시 뒤 60일 −3~−6%(t 4~11)가 나와 사전 등록 기준을 통과한 유일한 가설.
여기서는 매일 18시에 당일 접수 공시를 신호로 기록만 하고(방향 +/−), 측정은 기존 measure 경로가 1/5/20d로 한다.
판정(2027-01-15): 'dart-v1' 버전의 20d 방향보정 평균 > +0.4%, t > 2 이면 표본 외 확인.
"""
import re

import requests

KINDS = {
    "자기주식취득결정": ("BUYBACK", "+"), "자기주식취득신탁계약체결결정": ("BUYBACK_TRUST", "+"),
    "유상증자결정": ("RIGHTS", "-"), "유무상증자결정": ("RIGHTS", "-"), "전환사채권발행결정": ("CB", "-"),
    "자기주식처분결정": ("DISPOSAL", "-"), "무상증자결정": ("BONUS", "-"),  # 무상증자: 5일 반등 후 60일 −7% → 회피(−)
}


def fetch_events(cfg, yyyymmdd):
    """해당 일자 접수된 주요사항보고서 중 분류 대상 → [{code, corp, kind, direction, rcept_no, dt, report}]"""
    key = cfg.get("DART_API_KEY")
    if not key:
        return []
    out, page = [], 1
    while True:
        r = requests.get("https://opendart.fss.or.kr/api/list.json",
                         params={"crtfc_key": key, "bgn_de": yyyymmdd, "end_de": yyyymmdd, "pblntf_ty": "B",
                                 "page_count": 100, "page_no": page}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "013":
            break
        if j.get("status") != "000":
            raise RuntimeError(f"DART {j.get('status')}: {str(j.get('message'))[:80]}")
        for x in j.get("list", []):
            code = (x.get("stock_code") or "").strip()
            if not code:
                continue
            nm = re.sub(r"\[[^\]]*\]", "", x["report_nm"])
            inner = nm[nm.find("(") + 1:nm.rfind(")")] if "(" in nm else nm
            k = KINDS.get(inner)
            if not k:
                continue
            out.append({"code": code, "corp": x["corp_name"], "kind": k[0], "direction": k[1],
                        "rcept_no": x["rcept_no"], "dt": x["rcept_dt"], "report": inner})
        if page >= int(j.get("total_page", 1)):
            break
        page += 1
    return out
