# -*- coding: utf-8 -*-
"""국제정세 신호 검증 엔진 — 매매 연결 없음(기록·측정 전용).

사용:
  python main.py auto                 # 크론용: 매시간 수집·판단, MEASURE_HOUR_KST에 측정·요약
  python main.py collect              # 수집·판단만
  python main.py measure              # 측정·요약만
  python main.py collect --dry-run    # 시트 없이 콘솔 출력(LLM 키 필요)
  python main.py collect --dry-run --mock-llm sample.json   # LLM 없이 파이프라인 점검
"""
import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from classifier import classify, prompt_version, provider_tag, screen, validate
from config import KST, load_config
from measure import HORIZONS, iso_week, measure_one, parse_kst, summarize
from news import collect

ROOT = Path(__file__).parent
def _load_universe():
    """우선순위: Secret PRIVATE_UNIVERSE → private/universe.json(.gitignore) → universe.sample.json(공개용 최소본)."""
    import os
    raw = os.environ.get("PRIVATE_UNIVERSE", "").strip()
    if raw:
        return json.loads(raw)
    for f in (ROOT / "private" / "universe.json", ROOT / "universe.sample.json"):
        if f.exists():
            if f.name.endswith("sample.json"):
                if os.environ.get("GITHUB_ACTIONS"):
                    raise RuntimeError("Secret PRIVATE_UNIVERSE 미등록 — 중단")
                print("[main] 경고: 비공개 유니버스 없음 → 샘플 유니버스로 실행(로컬 전용)")
            return json.loads(f.read_text(encoding="utf-8"))
    raise FileNotFoundError("유니버스 파일 없음")


UNIVERSE = _load_universe()
CODE_SECTOR = {r["code"]: sec for sec, rows in UNIVERSE["sectors"].items() for r in rows}
ABSOLUTE_SECTORS = {sec for sec in UNIVERSE["sectors"] if sec.startswith("ETF_")}
SEEN_KEEP_HOURS = 12
SEEN_CHUNK = 40000  # 구글시트 셀 한도 50,000자 → 청크 분할 저장(12시간×150건 ≈ 1,800해시)


def _load_seen(sheet):
    """seen_news: 'hash:epoch분' 쉼표 문자열을 seen_news#0..N 청크로 저장. 구형(JSON dict) 키도 읽음."""
    seen = {}
    old = sheet.get_state("seen_news", {})
    if isinstance(old, dict):
        seen.update({h: float(v) for h, v in old.items()})
    for r in sheet.records("_state"):
        if str(r["key"]).startswith("seen_news#"):
            try:
                body = json.loads(r["value"]) or ""
            except Exception:
                body = ""
            for tok in str(body).split(","):
                h, _, m = tok.partition(":")
                if h and m.isdigit():
                    seen[h] = int(m) * 60.0
    return seen


def _save_seen(sheet, seen):
    body = ",".join(f"{h}:{int(v // 60)}" for h, v in seen.items())
    # 청크 경계에서 토큰이 잘리지 않게 쉼표 기준으로 분할
    chunks, cur = [], []
    size = 0
    for tok in body.split(",") if body else []:
        if size + len(tok) + 1 > SEEN_CHUNK and cur:
            chunks.append(",".join(cur)); cur, size = [], 0
        cur.append(tok); size += len(tok) + 1
    chunks.append(",".join(cur))
    prev = sum(1 for r in sheet.records("_state") if str(r["key"]).startswith("seen_news#"))
    for i in range(max(len(chunks), prev)):
        sheet.set_state(f"seen_news#{i}", chunks[i] if i < len(chunks) else "")
    sheet.set_state("seen_news", {})  # 구형 키 비움(셀 한도 초과 방지)
RECENT_EVENT_HOURS = 72


def now_kst():
    return datetime.now(KST)


def telegram(cfg, text):
    if not (cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_CHAT_ID")):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      json={"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": text[:4000],
                            "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        print(f"[telegram] 실패: {type(e).__name__}")  # URL에 봇토큰 포함 → 메시지 출력 금지


def _redact(text, cfg):
    """공개 저장소 로그 대비: 설정값(키·토큰)이 출력에 섞이면 가림."""
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "NAVER_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "SHEET_ID"):
        v = cfg.get(k)
        if v and len(str(v)) > 6:
            text = text.replace(str(v), "***")
    return text


def is_session(t):
    return t.weekday() < 5 and (9, 0) <= (t.hour, t.minute) <= (15, 30)


# ───────────────────────── 수집·판단 ─────────────────────────
def run_collect(cfg, sheet, mock_llm=None):
    t = now_kst()
    ts = t.strftime("%Y-%m-%d %H:%M")

    seen = _load_seen(sheet) if sheet else {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SEEN_KEEP_HOURS)).timestamp()
    seen = {h: v for h, v in seen.items() if v >= cutoff}

    recent_by_ver = {}
    if sheet:
        lim = (t - timedelta(hours=RECENT_EVENT_HOURS)).strftime("%Y-%m-%d %H:%M")
        for r in sheet.records("신호로그"):
            if str(r["판단시각(KST)"]) >= lim:
                recent_by_ver.setdefault(str(r["프롬프트버전"]), []).append(
                    {"event_key": r["사건키"], "summary": r["사건요약"]})

    headlines = collect(cfg, set(seen))
    raw_headlines = list(headlines)
    returns = sheet.get_state("recent_returns", {}) if sheet else {}
    returns = {k: tuple(v) for k, v in returns.get("data", {}).items()}
    print(f"[collect] 신규 헤드라인 {len(headlines)}건, 수익률스냅샷 {len(returns)}종목")

    # 1차 선별(저가 모델): 무관 뉴스 제거 → 본 판단 토큰 절감. 0건이면 본 판단 호출 자체를 건너뜀(조용한 시간대 비용 0).
    n_raw, screen_note = len(headlines), ""
    if headlines and not mock_llm:
        try:
            headlines, smodel = screen(cfg, headlines)
            screen_note = f"→{len(headlines)}"
            print(f"[screen] {n_raw} → {len(headlines)}건 ({smodel})")
        except Exception as e:
            screen_note = "→스크리너실패(전체통과)"
            print(f"[screen] 실패 → fail-open: {type(e).__name__}")

    providers = ["mock"] if mock_llm else cfg["PROVIDERS"]
    if not headlines:
        providers = []
    sig_rows, meas_rows, alerts, dropped_all, primary_events = [], [], [], [], []
    for pi, prov in enumerate(providers):
        pver = "mock" if mock_llm else prompt_version(cfg, UNIVERSE, prov)
        # 같은 모델 계열(태그)의 최근 사건 전체 — 버전이 바뀌어도(쿼리·유니버스 수정) 진행 중 사건을 '신규'로 재기록하지 않게.
        tag = pver.split("-")[0]
        recent = [e for v, evs in recent_by_ver.items() if v == pver or v.startswith(tag + "-") for e in evs]
        try:
            if mock_llm:
                raw = json.loads(Path(mock_llm).read_text(encoding="utf-8"))
                events, dropped = validate(raw, headlines, UNIVERSE, {e["event_key"] for e in recent},
                                           cfg["MIN_MATERIALITY"])
                model = "mock"
            else:
                events, dropped, model = classify(cfg, headlines, recent, UNIVERSE, returns, prov)
        except Exception as e:
            # 그림자 모델 실패가 주 모델 기록을 막으면 안 됨 — 기록 후 계속
            dropped_all.append(f"{prov}:오류:{type(e).__name__}")
            if pi == 0:
                raise
            continue
        dropped_all += [f"{prov}:{d}" for d in dropped]
        if pi == 0:
            primary_events = events
        for k, ev in enumerate(events):
            sid = f"S{t.strftime('%y%m%d%H%M')}-{'moc' if mock_llm else provider_tag(cfg, prov)}{k}"
            titles = " / ".join(n["title"] for n in ev["news"][:3])
            links = " ".join(n["link"] for n in ev["news"][:3])
            sig_rows.append([sid, ts, ev["event_key"], ev["category"], ev["summary"], ev["materiality"],
                             ev["horizon_days"], "장중" if is_session(t) else "장외", titles, links, model, pver])
            for j, im in enumerate(ev["impacts"]):
                meas_rows.append([f"{sid}-{j}", sid, ts, ev["category"], ev["summary"], im["direction"],
                                  im["confidence"], im["code"], im["name"], im["rationale"], pver]
                                 + [""] * 16 + ["대기", ts])
                if pi == 0 and im["confidence"] >= cfg["ALERT_MIN_CONFIDENCE"]:
                    alerts.append(f"{im['direction']} {im['name']}({im['code']}) 확신{im['confidence']} — {im['rationale']}")

    if sheet:
        # 원문 보관(별도 시트·월별 탭): 프롬프트를 바꿔도 같은 뉴스로 재채점(오프라인)할 수 있게
        sheet.append_archive(t.strftime("%Y-%m"),
                             [[ts, h["id"], h["ts_kst"], h["source"], h["lang"], h["title"], h["link"], h["hash"]]
                              for h in raw_headlines])
        sheet.append("신호로그", sig_rows)
        sheet.append("측정", meas_rows)
        now_ts = datetime.now(timezone.utc).timestamp()
        for h in raw_headlines:
            seen[h["hash"]] = now_ts
        _save_seen(sheet, seen)
        sheet.append("실행로그", [[ts, "collect", f"{n_raw}{screen_note}", len(sig_rows), len(meas_rows), "",
                                 "; ".join(dropped_all)[:500], ""]])
    else:
        for h in raw_headlines[:15]:
            print(f"  {h['id']} {h['ts_kst']} [{h['source']}] {h['title']}")
        print("── 신호로그 ──")
        for r in sig_rows:
            print("  ", r[:8], r[-1])
        print("── 측정(신규) ──")
        for r in meas_rows:
            print("  ", r[:11])
        print("── 제외 ──", dropped_all)

    if alerts:
        telegram(cfg, f"[신호검증] {ts} 신규 사건 {len(primary_events)}건\n" +
                 "\n".join(f"• {e['summary']}" for e in primary_events) + "\n\n고확신 종목:\n" + "\n".join(alerts) +
                 "\n(기록 전용 — 매매 아님)")
    return len(sig_rows), len(meas_rows)


# ───────────────────────── 측정·요약 ─────────────────────────
MEAS_COL_START = "L"  # 기준일 ~ 갱신시각 (L..AC)


def _snapshot_returns(sheet, t):
    """유니버스 전체의 최근 5/20영업일 수익률을 하루 1회 저장 → 매시간 collect가 LLM에 제공(시세 호출 70회/일로 제한)."""
    from prices import daily_closes
    snap, fails = {}, 0
    for code in CODE_SECTOR:
        try:
            px = daily_closes(code)
            days = sorted(px)
            if len(days) > 20:
                snap[code] = (round((px[days[-1]] / px[days[-6]] - 1) * 100, 2),
                              round((px[days[-1]] / px[days[-21]] - 1) * 100, 2))
        except Exception:
            fails += 1
    sheet.set_state("recent_returns", {"asof": t.strftime("%Y-%m-%d %H:%M"), "data": snap})
    print(f"[snapshot] 수익률 {len(snap)}종목, 실패 {fails}")


def run_measure(cfg, sheet):
    t = now_kst()
    ts = t.strftime("%Y-%m-%d %H:%M")
    recs = sheet.records("측정")
    updates, done_rows, n_upd, errs = [], [], 0, []
    for i, r in enumerate(recs, start=2):
        if r["상태"] == "완료":
            done_rows.append(r)
            continue
        try:
            code = str(r["종목코드"]).zfill(6)
            m = measure_one(parse_kst(str(r["판단시각(KST)"])), code, r["방향"], t,
                            absolute=CODE_SECTOR.get(code) in ABSOLUTE_SECTORS)
        except Exception as e:
            errs.append(f"{r['측정ID']}:{e}")
            continue
        vals = [m["base_date"] or "", m["base"] if m["base"] is not None else "",
                m["beta"] if m["beta"] is not None else "", m["pre"] if m["pre"] is not None else ""]
        for key in ("r", "b", "x", "s"):
            vals += [m[f"{key}{h}"] if m[f"{key}{h}"] is not None else "" for h in HORIZONS]
        vals += [m["status"], ts]
        updates.append((i, MEAS_COL_START, vals))
        n_upd += 1
        r.update({f"방향보정{h}d": m[f"s{h}"] for h in HORIZONS})
        r.update({f"초과{h}d%": m[f"x{h}"] for h in HORIZONS})
        r["판단→기준%"] = m["pre"]
        r["기준일"] = m["base_date"]
        done_rows.append(r)
    sheet.update_rows("측정", updates)

    def _f(v):
        return float(v) if v not in ("", None) else None

    by_ver = {}
    for r in done_rows:
        by_ver.setdefault(str(r.get("버전", "")), []).append(r)
    summ = []
    for ver, rs in sorted(by_ver.items()):
        rows, base_rows = [], []
        for r in rs:
            wk = iso_week(r.get("기준일", ""))
            row = {"signal_id": r["신호ID"], "category": r["분류"], "confidence": r["확신도"], "week": wk}
            for h in HORIZONS:
                row[f"s{h}"] = _f(r.get(f"방향보정{h}d"))
            rows.append(row)
            # 기준선: LLM 판단 대신 '판단→기준 구간에 움직인 방향을 그대로 추종'.
            # LLM이 이 단순 규칙을 못 이기면 해석의 부가가치는 없다.
            pre = _f(r.get("판단→기준%"))
            if pre not in (None, 0.0):
                b = {"signal_id": r["신호ID"], "category": "기준선", "confidence": "-", "week": wk}
                for h in HORIZONS:
                    x = _f(r.get(f"초과{h}d%"))
                    b[f"s{h}"] = (1 if pre > 0 else -1) * x if x is not None else None
                base_rows.append(b)
        lines = summarize(rows)
        bl = summarize(base_rows)[0] if base_rows else None
        if bl:
            bl["group"] = "기준선:반응방향추종(비교용)"
            lines.insert(1, bl)
        for line in lines:
            out = [ver, line["group"]]
            for h in HORIZONS:
                st = line[h]
                out += [st["n"], st["mean"] if st["mean"] is not None else "",
                        st["hit"] if st["hit"] is not None else "", st["t"] if st["t"] is not None else ""]
            summ.append(out + [line[5].get("clusters", ""), ts])
    sheet.replace_body("요약", summ)
    _snapshot_returns(sheet, t)
    sheet.append("실행로그", [[ts, "measure", "", "", "", n_upd, "", "; ".join(errs)[:500]]])
    print(f"[measure] 갱신 {n_upd}행, 오류 {len(errs)}")
    if summ:
        msg = [f"[신호검증 일일요약] {ts}", "1차지표=5d 사건단위 평균 / 적중률 / 주클러스터 t (N, 주수)"]
        for a in summ:
            if a[1] in ("전체", "기준선:반응방향추종(비교용)"):
                msg.append(f"{a[0]} {a[1][:3]}: {a[7]}% / {a[8]}% / t={a[9]} (N={a[6]}, {a[14]}주)")
        telegram(cfg, "\n".join(msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["auto", "collect", "measure"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock-llm")
    a = ap.parse_args()
    cfg = load_config()
    sheet = None
    if not a.dry_run:
        from sheets import Sheet
        sheet = Sheet(cfg)
    t = now_kst()
    try:
        if a.mode in ("auto", "collect"):
            run_collect(cfg, sheet, a.mock_llm)
        if sheet and (a.mode == "measure" or (a.mode == "auto" and t.hour == cfg["MEASURE_HOUR_KST"])):
            run_measure(cfg, sheet)
    except Exception as e:
        print(_redact(traceback.format_exc(), cfg))
        if sheet:
            try:
                sheet.append("실행로그", [[t.strftime("%Y-%m-%d %H:%M"), a.mode, "", "", "", "", "", _redact(f"{e}", cfg)[:500]]])
            except Exception:
                pass
        telegram(cfg, f"[신호검증] 오류 {t:%m-%d %H:%M}: {_redact(str(e), cfg)[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
