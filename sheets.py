# -*- coding: utf-8 -*-
"""구글시트 I/O — 탭 자동 생성, RAW 입력(종목코드 앞자리 0 보존)."""
import json

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TABS = {
    "신호로그": ["신호ID", "판단시각(KST)", "사건키", "분류", "사건요약", "중요도", "예상지속(영업일)",
              "장중여부", "근거뉴스", "뉴스링크", "모델", "프롬프트버전"],
    "측정": ["측정ID", "신호ID", "판단시각(KST)", "분류", "사건요약", "방향", "확신도", "종목코드", "종목명",
           "판단근거", "버전", "기준일", "기준가", "베타", "판단→기준%", "1d%", "5d%", "20d%", "벤치1d%", "벤치5d%",
           "벤치20d%", "초과1d%", "초과5d%", "초과20d%", "방향보정1d", "방향보정5d", "방향보정20d",
           "상태", "갱신시각",
           # ↓ 신호 시점 스냅샷 + 단순 지수 대비 초과(종목%−지수%) — 참고 지표(1차 지표는 위 베타 조정 방향보정5d)
           "신호시점가", "KOSPI@신호", "SPX@신호", "단순초과K1d", "단순초과K5d", "단순초과K20d",
           "단순초과S1d", "단순초과S5d", "단순초과S20d"],
    "바구니": ["바구니ID", "신호ID", "판단시각(KST)", "섹터", "구분", "방향", "종목코드", "종목명", "버전",
            "신호시점가", "신호시점일", "KOSPI@신호", "SPX@신호", "기준일", "기준가",
            "1d%", "5d%", "20d%", "KOSPI1d%", "KOSPI5d%", "KOSPI20d%", "SPX1d%", "SPX5d%", "SPX20d%",
            "초과K1d", "초과K5d", "초과K20d", "초과S1d", "초과S5d", "초과S20d", "상태", "갱신시각"],
    "요약": ["버전", "그룹", "N(1d)", "평균1d", "적중률1d", "t1d", "N(5d)", "평균5d", "적중률5d", "t5d",
           "N(20d)", "평균20d", "적중률20d", "t20d", "주클러스터수", "갱신시각"],
    "실행로그": ["시각(KST)", "모드", "수집뉴스", "신규사건", "신규종목행", "측정갱신", "제외사유", "오류"],
    "_state": ["key", "value"],
}


ARCHIVE_HEADER = ["수집시각(KST)", "뉴스ID", "보도시각(KST)", "출처", "언어", "제목", "링크", "해시"]


class Sheet:
    def __init__(self, cfg):
        creds = Credentials.from_service_account_info(cfg["GCP_SA_JSON"], scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(cfg["SHEET_ID"])
        self.archive_id = cfg.get("ARCHIVE_SHEET_ID") or cfg["SHEET_ID"]
        existing = {ws.title: ws for ws in self.sh.worksheets()}
        self.ws = {}
        for name, header in TABS.items():
            ws = existing.get(name)
            if ws is None:
                ws = self.sh.add_worksheet(title=name, rows=1000, cols=len(header))
                ws.update([header], "A1", value_input_option="RAW")
                ws.freeze(rows=1)
            elif ws.row_values(1) != header:
                if ws.col_count < len(header):  # 열 추가 시 그리드 확장(기존 데이터 유지, 오른쪽에 새 열)
                    ws.add_cols(len(header) - ws.col_count)
                ws.update([header], "A1", value_input_option="RAW")
            self.ws[name] = ws

    def append_archive(self, month, rows):
        """뉴스원문 → 보관용 스프레드시트의 월별 탭(원문_YYYY-MM). 판정 시트가 무거워지지 않게 분리."""
        if not rows:
            return
        sh = self.sh if self.archive_id == self.sh.id else self.gc.open_by_key(self.archive_id)
        name = f"원문_{month}"
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=1000, cols=len(ARCHIVE_HEADER))
            ws.update([ARCHIVE_HEADER], "A1", value_input_option="RAW")
            ws.freeze(rows=1)
        ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")

    def append(self, tab, rows):
        if rows:
            self.ws[tab].append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")

    def records(self, tab):
        return self.ws[tab].get_all_records(expected_headers=TABS[tab], default_blank="")

    def get_state(self, key, default=None):
        for r in self.records("_state"):
            if r["key"] == key:
                try:
                    return json.loads(r["value"])
                except Exception:
                    return default
        return default

    def set_state(self, key, value):
        ws = self.ws["_state"]
        recs = self.records("_state")
        payload = json.dumps(value, ensure_ascii=False)
        for i, r in enumerate(recs, start=2):
            if r["key"] == key:
                ws.update([[key, payload]], f"A{i}", value_input_option="RAW")
                return
        ws.append_rows([[key, payload]], value_input_option="RAW")

    def update_rows(self, tab, updates):
        """updates: [(row_index(1-based), start_col_letter, [values...])]"""
        if not updates:
            return
        body = [{"range": f"{col}{ri}", "values": [vals]} for ri, col, vals in updates]
        for i in range(0, len(body), 200):
            self.ws[tab].batch_update(body[i:i + 200], value_input_option="RAW")

    def replace_body(self, tab, rows):
        ws = self.ws[tab]
        ws.batch_clear([f"A2:Z{max(ws.row_count, 2)}"])
        if rows:
            ws.update(rows, "A2", value_input_option="RAW")
