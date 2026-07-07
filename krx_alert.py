# -*- coding: utf-8 -*-
"""
KRX 시장경보(투자경고/투자위험/단기과열) 수집기
================================================
open.krx.co.kr 의 2단계 흐름(GenerateOTP → OPN99000001)을 그대로 재현해
헤드리스로 데이터를 받아 dashboard 가 읽는 alert.json 으로 저장한다.

- 대시보드 '투경' 탭이 이 파일을 읽는다.
- 매일 장 마감 후(예: 20:00) cron 으로 1회 실행:
    0 20 * * 1-5  cd ~/auto-sto && /usr/bin/python3 krx_alert.py >> alert.log 2>&1

의존성: requests (봇과 동일). numpy 불필요.
"""
import os
import json
import time
import datetime as dt
from urllib.parse import quote

import requests
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE, "alert.json")
HALT_FILE = os.path.join(BASE, "halt_days.json")   # 종목별 매매거래정지일 누적

OTP_URL = "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx"
DATA_URL = "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx"

# KIND(기업공시채널) — 매매거래정지 현황/예고
KIND_HALT_URL = "https://kind.krx.co.kr/investwarn/tradinghaltissue.do"
KIND_DISC_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"

# 2026년 한국 공휴일(경과거래일수 계산용) — 스킬과 동일
HOLIDAYS = {
    "2026-01-01", "2026-01-29", "2026-01-30", "2026-01-31", "2026-02-01",
    "2026-02-02", "2026-03-01", "2026-05-01", "2026-05-15", "2026-08-15",
    "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27", "2026-10-03",
    "2026-10-09", "2026-12-25",
}


# ── 날짜 유틸 ─────────────────────────────────────────────
def is_bday(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_bday(d: dt.date) -> dt.date:
    while not is_bday(d):
        d -= dt.timedelta(days=1)
    return d


def next_bday(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_bday(d):
        d += dt.timedelta(days=1)
    return d


def busday_count(a: dt.date, b: dt.date) -> int:
    """[a, b) 구간의 영업일 수 (np.busday_count 과 동일 정의)."""
    if a >= b:
        return 0
    n, cur = 0, a
    while cur < b:
        if is_bday(cur):
            n += 1
        cur += dt.timedelta(days=1)
    return n


def parse_kdate(s: str):
    if not s or s == "-":
        return None
    return dt.datetime.strptime(s.replace("/", "-"), "%Y-%m-%d").date()


def fmt(s: str) -> str:
    return s.replace("/", "-") if s and s != "-" else "-"


# ── KRX 수집 ─────────────────────────────────────────────
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def fetch(sess: requests.Session, bld: str, page_path: str, params: dict) -> list:
    """GenerateOTP 로 code 발급 후 OPN99000001 POST → 행 리스트 반환."""
    ref = "https://open.krx.co.kr" + page_path
    otp = sess.get(
        OTP_URL,
        params={"bld": bld, "name": "form", "_": str(int(time.time() * 1000))},
        headers={"Referer": ref},
        timeout=20,
    ).text.strip()

    body = dict(params)
    body["pagePath"] = page_path
    body["code"] = otp
    res = sess.post(DATA_URL, data=body, headers={"Referer": ref}, timeout=20).json()
    return res.get("block1") or res.get("result") or []


def collect():
    sess = _session()
    end = prev_bday(dt.date.today())          # 기준 거래일(마지막 영업일)
    strt = end - dt.timedelta(days=9)         # 최근 해제분 포함용 여유 구간
    s_str, e_str = strt.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    warn_raw = fetch(sess, "MKD/10/1002/10020406/mkd10020406",
                     "/contents/MKD/10/1002/10020406/MKD10020406.jsp",
                     {"ind_tp": "ALL", "period_strt_dd": s_str, "period_end_dd": e_str})
    danger_raw = fetch(sess, "MKD/10/1002/10020407/mkd10020407",
                       "/contents/MKD/10/1002/10020407/MKD10020407.jsp",
                       {"ind_tp": "ALL", "period_strt_dd": s_str, "period_end_dd": e_str})
    over_raw = fetch(sess, "MKD/04/0403/04031200/mkd04031200",
                     "/contents/MKD/04/0403/04031200/MKD04031200.jsp",
                     {"mkt_tp_cd": "ALL", "fromdate": e_str, "todate": e_str})
    return warn_raw, danger_raw, over_raw, end


# ── KIND 매매거래정지(현황/예고) 수집 ─────────────────────
class _RowParser(HTMLParser):
    """<tr><td>…</td></tr> 표에서 셀 텍스트만 추출(의존성 없이)."""
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._buf, self._in = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._in, self._buf = True, []

    def handle_endtag(self, tag):
        if tag == "td" and self._in:
            self._row.append(" ".join("".join(self._buf).split()))
            self._in = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def _kind_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def fetch_halt_status():
    """KIND 매매거래정지종목(현재 정지 중) → [{'name','reason'}]. 실패 시 []."""
    try:
        s = _kind_session()
        s.get(KIND_HALT_URL + "?method=searchTradingHaltIssueMain", timeout=20)
        body = {"method": "searchTradingHaltIssueSub", "forward": "tradinghaltissue_sub",
                "currentPageSize": "300", "pageIndex": "1", "searchMode": "",
                "searchCodeType": "", "searchCorpName": "", "marketType": ""}
        html = s.post(KIND_HALT_URL, data=body,
                      headers={"Referer": KIND_HALT_URL}, timeout=20).text
        p = _RowParser(); p.feed(html)
        out = []
        for r in p.rows:                       # r = [번호, 종목명, 사유]
            if len(r) < 3:
                continue
            name = r[1].strip()
            if not name or name == "종목명":
                continue
            out.append({"name": name, "reason": r[2].strip()})
        return out
    except Exception as e:
        print("[alert] 정지현황 수집 실패:", repr(e))
        return []


def fetch_halt_notice(ref_day: dt.date):
    """KIND 오늘 공시 중 제목에 '매매거래정지' 포함 → [{'time','name','title'}]. 실패 시 []."""
    try:
        s = _kind_session()
        s.get(KIND_DISC_URL + "?method=searchTodayDisclosureMain", timeout=20)
        body = {"method": "searchTodayDisclosureSub", "forward": "todaydisclosure_sub",
                "currentPageSize": "200", "pageIndex": "1", "orderMode": "0", "orderStat": "D",
                "marketType": "", "searchMode": "", "searchCodeType": "", "chose": "",
                "todayFlag": "N", "selDate": ref_day.isoformat(), "searchCorpName": ""}
        html = s.post(KIND_DISC_URL, data=body,
                      headers={"Referer": KIND_DISC_URL}, timeout=20).text
        p = _RowParser(); p.feed(html)
        out = []
        for r in p.rows:                       # r = [시각, 종목명, 제목, 시장본부, …]
            if len(r) < 3:
                continue
            title = r[2].strip()
            if "매매거래정지" in title:
                out.append({"time": r[0].strip(), "name": r[1].strip(), "title": title})
        return out
    except Exception as e:
        print("[alert] 정지예고 수집 실패:", repr(e))
        return []


# ── 정지일 누적 저장(종목별 날짜 집합, 멱등) ────────────────
def _load_halt_store():
    try:
        with open(HALT_FILE, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d.get("seen"), dict):
                return d
    except Exception:
        pass
    return {"seen": {}}


def _save_halt_store(store):
    tmp = HALT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, HALT_FILE)


def update_halt_store(status, notice, designated, ref_day):
    """정지 관측을 종목별 날짜 집합으로 누적.
    - 현황(오늘 정지 중): ref_day 기록
    - 예고(매매거래정지 예고 공시): 다음 영업일 기록(그날 정지 예정)
    지정 종목만 대상, 하루 1회 멱등, 지정 해제 종목은 정리."""
    store = _load_halt_store()
    seen = store["seen"]
    halted_today = {h["name"] for h in status}
    nxt = next_bday(ref_day).isoformat()
    today = ref_day.isoformat()

    def mark(name, iso):
        if name not in designated:
            return
        lst = seen.setdefault(name, [])
        if iso not in lst:
            lst.append(iso)

    for nm in designated:
        if nm in halted_today:
            mark(nm, today)
    for nd in notice:
        mark(nd["name"], nxt)

    for nm in list(seen.keys()):
        if nm not in designated:
            del seen[nm]
    _save_halt_store(store)
    return store


# ── 스킬 규칙 적용 → dashboard용 구조 ──────────────────────
def build(warn_raw, danger_raw, over_raw, ref_day: dt.date,
          status=None, notice=None, store=None):
    status = status or []
    notice = notice or []
    seen = (store or {}).get("seen", {})
    halted_now = {h["name"] for h in status}

    code_by_name = {}
    for r in list(warn_raw) + list(danger_raw):
        if r.get("kor_isu_nm"):
            code_by_name[r["kor_isu_nm"]] = r.get("isu_cd", "")
    for r in over_raw:
        if r.get("isu_nm"):
            code_by_name[r["isu_nm"]] = r.get("isu_srt_cd", "")

    def halt_dates_for(name, des):
        lst = seen.get(name, [])
        d0 = parse_kdate(des)
        if d0:
            lst = [x for x in lst if x >= d0.isoformat()]
        return sorted(lst)
    today_next = next_bday(ref_day)

    def elapsed(des):
        # (수집 마감일 기준 거래일수) — 과열 10일차 필터 등 서버 판정용.
        # 대시보드 표시용 'X일차'는 조회 시점(오늘)이 반영되도록 dashboard.html에서
        # des/free 로 재계산한다. 여기 값은 표시에 직접 쓰이지 않음.
        d = parse_kdate(des)
        return busday_count(d, ref_day) + 1 if d else 0

    def days_since_release(free):
        d = parse_kdate(free)
        if not d:
            return 999
        return busday_count(d + dt.timedelta(days=1), today_next)

    def norm(r):
        return {"name": r.get("kor_isu_nm"), "code": r.get("isu_cd"),
                "act": r.get("act_dd", "-"), "des": r.get("design_dd", "-"),
                "free": r.get("free_dt", "-")}

    warn = [norm(r) for r in warn_raw]
    danger = [norm(r) for r in danger_raw]

    # 투자위험 현재 지정 중 종목명(투경 중복 제거용)
    danger_active_names = {d["name"] for d in danger if d["free"] == "-"}

    def include_warn(w):
        if w["name"] in danger_active_names:      # 투위 지정 중이면 투경에서 제외
            return False
        if w["free"] == "-":
            return True
        return days_since_release(w["free"]) <= 2  # 해제 후 2거래일 이내만

    def pack(rows):
        out = []
        for r in rows:
            released = r["free"] != "-"
            out.append({
                "name": r["name"], "code": r["code"],
                "act": fmt(r["act"]), "des": fmt(r["des"]), "free": fmt(r["free"]),
                "elapsed": elapsed(r["des"]),
                "halt_dates": halt_dates_for(r["name"], r["des"]),
                "halted": r["name"] in halted_now,
                "released": released,
                "state": ("해제 (%s)" % fmt(r["free"])) if released else "지정 중",
            })
        return out

    warn_out = pack([w for w in warn if include_warn(w)])

    # 투자위험: 중복 종목 최신 1건만(현재 지정 중 우선)
    dedup_seen, danger_dedup = set(), []
    for d in danger:
        if d["name"] in dedup_seen:
            continue
        dedup_seen.add(d["name"])
        danger_dedup.append(d)
    danger_out = pack(danger_dedup)

    over_out = []
    for r in over_raw:
        el = elapsed(r.get("design_dd", "-"))
        if el > 10:                    # 10거래일 초과 단기과열은 표시 제외(사용자 요청)
            continue
        tp = r.get("fluc_tp_cd", "0")
        chg = str(r.get("cmpprevdd_prc", "0"))
        nm = r.get("isu_nm")
        over_out.append({
            "name": nm, "code": r.get("isu_srt_cd"),
            "price": r.get("tdd_clsprc", "-"),
            "chg": chg, "up": tp == "2", "down": tp == "1",
            "des": fmt(r.get("design_dd", "-")), "free": fmt(r.get("releas_dd", "-")),
            "elapsed": el,
            "halt_dates": halt_dates_for(nm, r.get("design_dd", "-")),
            "halted": nm in halted_now,
        })

    # 거래정지 현황(투경 관련만): 지정 종목이거나 사유에 투경 키워드
    halted_out = [{"name": h["name"], "code": code_by_name.get(h["name"], ""),
                   "reason": h["reason"]}
                  for h in status
                  if h["name"] in code_by_name
                  or any(k in h["reason"] for k in ("투자경고", "투자위험", "단기과열"))]

    # 매매거래정지 예고 공시
    notice_out = [{"name": n["name"], "code": code_by_name.get(n["name"], ""),
                   "title": n["title"], "time": n["time"]} for n in notice]

    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "trade_date": ref_day.isoformat(),
        "warning": warn_out,
        "danger": danger_out,
        "overheat": over_out,
        "halted": halted_out,
        "halt_notice": notice_out,
        "counts": {"warning": len(warn_out), "danger": len(danger_out),
                   "overheat": len(over_out),
                   "halted": len(halted_out), "notice": len(notice_out)},
    }


def save(data: dict):
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_FILE)


def main():
    try:
        warn_raw, danger_raw, over_raw, end = collect()
        status = fetch_halt_status()
        notice = fetch_halt_notice(end)
        designated = set()
        for r in warn_raw + danger_raw:
            if r.get("kor_isu_nm"):
                designated.add(r["kor_isu_nm"])
        for r in over_raw:
            if r.get("isu_nm"):
                designated.add(r["isu_nm"])
        store = update_halt_store(status, notice, designated, end)
        data = build(warn_raw, danger_raw, over_raw, end, status, notice, store)
        save(data)
        c = data["counts"]
        print("[alert] 저장 완료 %s  투경 %d · 투위 %d · 과열 %d · 정지 %d · 정지예고 %d"
              % (data["updated"], c["warning"], c["danger"], c["overheat"],
                 c["halted"], c["notice"]))
    except Exception as e:
        print("[alert] 수집 실패:", repr(e))
        raise


if __name__ == "__main__":
    main()
