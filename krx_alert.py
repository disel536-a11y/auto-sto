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

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE, "alert.json")

OTP_URL = "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx"
DATA_URL = "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx"

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


# ── 스킬 규칙 적용 → dashboard용 구조 ──────────────────────
def build(warn_raw, danger_raw, over_raw, ref_day: dt.date):
    today_next = next_bday(ref_day)

    def elapsed(des):
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
                "released": released,
                "state": ("해제 (%s)" % fmt(r["free"])) if released else "지정 중",
            })
        return out

    warn_out = pack([w for w in warn if include_warn(w)])

    # 투자위험: 중복 종목 최신 1건만(현재 지정 중 우선)
    seen, danger_dedup = set(), []
    for d in danger:
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        danger_dedup.append(d)
    danger_out = pack(danger_dedup)

    over_out = []
    for r in over_raw:
        tp = r.get("fluc_tp_cd", "0")
        chg = str(r.get("cmpprevdd_prc", "0"))
        over_out.append({
            "name": r.get("isu_nm"), "code": r.get("isu_srt_cd"),
            "price": r.get("tdd_clsprc", "-"),
            "chg": chg, "up": tp == "2", "down": tp == "1",
            "des": fmt(r.get("design_dd", "-")), "free": fmt(r.get("releas_dd", "-")),
            "elapsed": elapsed(r.get("design_dd", "-")),
        })

    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "trade_date": ref_day.isoformat(),
        "warning": warn_out,
        "danger": danger_out,
        "overheat": over_out,
        "counts": {"warning": len(warn_out), "danger": len(danger_out),
                   "overheat": len(over_out)},
    }


def save(data: dict):
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_FILE)


def main():
    try:
        raw = collect()
        data = build(*raw)
        save(data)
        c = data["counts"]
        print("[alert] 저장 완료 %s  투경 %d · 투위 %d · 과열 %d"
              % (data["updated"], c["warning"], c["danger"], c["overheat"]))
    except Exception as e:
        print("[alert] 수집 실패:", repr(e))
        raise


if __name__ == "__main__":
    main()
