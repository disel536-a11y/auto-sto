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

# 일별 종가 수집기(키움→KRX→네이버 순서로 자동 선택).
# 없으면 해제가격만 비워두고 나머지는 정상 동작한다.
try:
    import price_source
except Exception as _e:
    price_source = None
    print("[alert] 종가 수집기 사용 불가(해제가격 생략):", repr(_e))

# 투자경고 해제가격을 계산해 표시하기 시작하는 거래일수
RELEASE_FROM_DAY = 8
# 해제 판단이 시작되는 거래일수(지정일=1일차 기준). 이 날부터 매일 요건을 본다.
JUDGE_DAY = 10

OTP_URL = "https://open.krx.co.kr/contents/COM/GenerateOTP.jspx"
DATA_URL = "https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx"

# KIND(기업공시채널) - 매매거래정지 현황/예고
KIND_HALT_URL = "https://kind.krx.co.kr/investwarn/tradinghaltissue.do"
KIND_DISC_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"

# 거래일 달력은 market_calendar 로 통일(공휴일 표 중복 제거).
# 주의: 이 파일의 기존 prev_bday 는 '오늘 포함 그 이하 마지막 거래일'(floor)
#       의미였으므로 market_calendar.last_bday 로 매핑한다.
from market_calendar import (HOLIDAYS, is_bday, next_bday, busday_count,
                             last_bday as prev_bday)


# ── 실제 거래일 달력 ──────────────────────────────────────
# 손으로 만든 HOLIDAYS 표는 틀리기 쉽다(2026년 제헌절 부활 같은 변경을 놓침).
# 실제 거래일을 시세 소스에서 받아 쓰고, 실패할 때만 HOLIDAYS 로 폴백한다.
TRADING_DAYS = []          # ['YYYY-MM-DD', ...] 오름차순


def load_trading_days(count=90):
    global TRADING_DAYS
    if price_source is None:
        return []
    try:
        TRADING_DAYS = price_source.fetch_trading_days(count)
        print("[alert] 거래일 달력 %d일 (마지막 %s)" % (len(TRADING_DAYS), TRADING_DAYS[-1]))
    except Exception as e:
        TRADING_DAYS = []
        print("[alert] 거래일 달력 실패 → 공휴일표로 대체:", str(e)[:150])
    return TRADING_DAYS


def last_trading_day() -> dt.date:
    """오늘 이하의 가장 최근 실제 거래일."""
    today = dt.date.today().isoformat()
    for d in reversed(TRADING_DAYS or []):
        if d <= today:
            return dt.date.fromisoformat(d)
    return prev_bday(dt.date.today())


def td_count(a_iso: str, b_iso: str):
    """[a, b] 양끝 포함 구간의 실제 거래일 수. 달력 밖이면 None."""
    if not TRADING_DAYS or a_iso < TRADING_DAYS[0]:
        return None
    return sum(1 for d in TRADING_DAYS if a_iso <= d <= b_iso)


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
    end = last_trading_day()                  # 기준 거래일(실제 마지막 거래일)
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


def is_halt_title(title: str) -> bool:
    """공시 제목이 '매매거래정지'를 뜻하는지 판정.

    KRX 제목 표기가 일정하지 않아 두 가지를 처리한다.
      - 띄어쓰기: '매매거래 정지 및 재개(투자경고종목 지정중)'  ← 모나미 사례.
        붙여쓰기만 찾으면 놓친다.
      - 해제 공시 제외: '주권매매거래정지해제(감자 주권 변경상장)' 는 정지가 아니라
        재개다. '정지해제/거래재개' 토큰을 먼저 지운 뒤에도 '매매거래정지'가
        남아 있어야 진짜 정지 공시로 본다.
        예) '매매거래정지및정지해제' -> '매매거래정지및' (정지 O)
            '주권매매거래정지해제'   -> '주권매매거래'   (정지 X)
    """
    n = (title or "").replace(" ", "")
    if "예고" in n:                      # 예고 공시는 별도 분류(is_halt_notice_title)
        return False
    core = n.replace("정지해제", "").replace("거래재개", "")
    return "매매거래정지" in core


def is_halt_notice_title(title: str) -> bool:
    """'매매거래정지 예고' 공시인지. (사전 안내이지 정지 자체는 아님)

    '투자경고종목 지정예고' 같은 다른 예고 공시는 제외한다.
    """
    n = (title or "").replace(" ", "")
    return "매매거래정지" in n and "예고" in n


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
        # marketType 을 빈 문자열로 보내면 KIND 가 '빈 응답(0바이트)'을 준다.
        # 반드시 "0"(전체) 같은 실제 값을 넣어야 표가 온다. 이걸 놓쳐서
        # '거래정지 종목'이 항상 0건이었고 halt_days.json 도 계속 비어 있었다.
        body = {"method": "searchTradingHaltIssueSub", "forward": "tradinghaltissue_sub",
                "currentPageSize": "300", "pageIndex": "1", "searchMode": "",
                "searchCodeType": "", "searchCorpName": "", "marketType": "0"}
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


def fetch_halt_disclosures(ref_day: dt.date):
    """기준일 공시를 두 갈래로 나눠 반환 → (정지공시, 정지예고공시).

    - 정지공시  : 실제 매매거래정지가 발생/예정인 공시(다음 거래일부터 정지).
                  예) '매매거래 정지 및 재개(투자경고종목 지정중)'  ← 모나미
    - 예고공시  : '매매거래정지 예고' 처럼 사전 안내에 그치는 공시.
    실패 시 ([], []).
    """
    try:
        s = _kind_session()
        s.get(KIND_DISC_URL + "?method=searchTodayDisclosureMain", timeout=20)
        body = {"method": "searchTodayDisclosureSub", "forward": "todaydisclosure_sub",
                "currentPageSize": "300", "pageIndex": "1", "orderMode": "0", "orderStat": "D",
                "marketType": "", "searchMode": "", "searchCodeType": "", "chose": "",
                "todayFlag": "N", "selDate": ref_day.isoformat(), "searchCorpName": ""}
        html = s.post(KIND_DISC_URL, data=body,
                      headers={"Referer": KIND_DISC_URL}, timeout=20).text
        p = _RowParser(); p.feed(html)
        halts, notices = [], []
        for r in p.rows:                       # r = [시각, 종목명, 제목, 시장본부, …]
            if len(r) < 3:
                continue
            item = {"time": r[0].strip(), "name": r[1].strip(), "title": r[2].strip()}
            if is_halt_notice_title(item["title"]):
                notices.append(item)
            elif is_halt_title(item["title"]):
                halts.append(item)
        return halts, notices
    except Exception as e:
        print("[alert] 정지 공시 수집 실패:", repr(e))
        return [], []


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


def update_halt_store(status, halt_disc, designated, ref_day):
    """정지 관측을 종목별 날짜 집합으로 누적.
    - 현황(오늘 정지 중): ref_day 기록
    - 정지 공시(장 마감 후 게시): 다음 영업일 기록(그날 정지 예정)
    지정 종목만 대상, 하루 1회 멱등, 지정 해제 종목은 정리.
    ※ '예고' 공시는 정지 확정이 아니므로 여기서 쓰지 않는다."""
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
    for nd in (halt_disc or []):
        mark(nd["name"], nxt)

    for nm in list(seen.keys()):
        if nm not in designated:
            del seen[nm]
    _save_halt_store(store)
    return store


# ── 스킬 규칙 적용 → dashboard용 구조 ──────────────────────
def build(warn_raw, danger_raw, over_raw, ref_day: dt.date,
          status=None, notice=None, store=None, halt_disc=None):
    status = status or []
    notice = notice or []
    halt_disc = halt_disc or []
    seen = (store or {}).get("seen", {})
    # 현재 정지 중 + 정지 공시로 다음 거래일 정지 예정인 종목 → 종목행 '정지' 배지
    halted_now = {h["name"] for h in status} | {d["name"] for d in halt_disc}

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
        # (수집 마감일 기준 거래일수) - 과열 10일차 필터 등 서버 판정용.
        # 대시보드 표시용 'X일차'는 조회 시점(오늘)이 반영되도록 dashboard.html에서
        # des/free 로 재계산한다. 여기 값은 표시에 직접 쓰이지 않음.
        d = parse_kdate(des)
        if not d:
            return 0
        n = td_count(d.isoformat(), ref_day.isoformat())   # 실제 거래일 달력 우선
        if n is not None:
            return max(1, n)
        return busday_count(d, ref_day) + 1                # 폴백: 공휴일표

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
        return w["free"] == "-"                   # 해제 종목은 표시하지 않음(사용자 요청)

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
        if d["name"] in dedup_seen or d["free"] != "-":   # 해제 종목 제외
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
    # 거래정지 종목 = ① 현재 정지 중(KIND 현황) + ② 정지 공시로 다음 거래일 정지 예정.
    # 공시는 장 마감 후 나오므로 실제 정지는 다음 거래일이다. 사용자가 그걸 보고
    # 매매하는 시점도 다음 거래일이라 같은 목록에 넣는다(상태는 구분 표기).
    def is_warn_related(name, text):
        return name in code_by_name or any(
            k in (text or "") for k in ("투자경고", "투자위험", "단기과열"))

    halted_out, seen_halt = [], set()
    for h in status:
        if not is_warn_related(h["name"], h["reason"]):
            continue
        halted_out.append({"name": h["name"], "code": code_by_name.get(h["name"], ""),
                           "reason": h["reason"], "when": "정지 중", "pending": False})
        seen_halt.add(h["name"])

    nxt = next_bday(ref_day)
    for d in (halt_disc or []):
        if d["name"] in seen_halt or not is_warn_related(d["name"], d["title"]):
            continue
        halted_out.append({"name": d["name"], "code": code_by_name.get(d["name"], ""),
                           "reason": d["title"],
                           "when": "%d/%d 정지 예정" % (nxt.month, nxt.day), "pending": True})
        seen_halt.add(d["name"])

    # 매매거래정지 '예고' 공시만
    notice_out = [{"name": n["name"], "code": code_by_name.get(n["name"], ""),
                   "title": n["title"], "time": n["time"]} for n in notice]

    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "trade_date": ref_day.isoformat(),
        # 대시보드가 'X일차'를 셀 때 쓰는 실제 거래일 목록(공휴일표 대체).
        "trading_days": list(TRADING_DAYS),
        "warning": warn_out,
        "danger": danger_out,
        "overheat": over_out,
        "halted": halted_out,
        "halt_notice": notice_out,
        "counts": {"warning": len(warn_out), "danger": len(danger_out),
                   "overheat": len(over_out),
                   "halted": len(halted_out), "notice": len(notice_out)},
    }


# ── 투자경고 해제가격 ─────────────────────────────────────
# 해제 요건: 판단일 종가가 아래 ①②③ 중 어느 것에도 해당하지 않을 것.
#   ① 판단일 종가 ≥ 5일 전날 종가 × 1.60
#   ② 판단일 종가 ≥ 15일 전날 종가 × 2.00
#   ③ 판단일 종가 = 최근 15일 종가 중 최고가
# 셋 다 피하려면 종가가 세 기준값 '모두보다 낮아야' 하므로,
# 해제가격 = min(①,②,③ 기준값) 이고 그 값 '미만'으로 마감하면 해제 요건 충족.
#
# 기준 시점은 '다음 거래일(T+1)'이다. closes 의 마지막이 직전 영업일(T)이므로
#   ① 의 5일 전날  = T-4  → closes[-5]
#   ② 의 15일 전날 = T-14 → closes[-15]
#   ③ 의 최근 15일 = {T-13 … T, T+1} → 이미 확정된 14일의 최고가가 기준
def release_price(closes, k=0):
    """판단일(D)의 해제 기준가. closes 는 [(YYYYMMDD, 종가)] 오름차순.

    k = 최신 종가일(T)에서 판단일까지 남은 거래일 수.  D = T + k
      - 아직 10일차 전이면 k = 10 - 현재일차  (10일차에 적용될 값을 미리 계산)
      - 이미 10일차 이상이면 k = 0           (최신 종가일이 곧 판단일)

    조건의 기준일을 D 기준으로 풀면:
      ① D-5  = T-(5-k)   → c[-(6-k)]
      ② D-15 = T-(15-k)  → c[-(16-k)]
      ③ 최근 15일 {D-14..D} 중 확정분 = T-(14-k)..T = (15-k)일 → c[-(15-k):]

    k>0 이면 ③의 윈도우가 아직 다 안 찼다. 남은 날 종가가 채워지며 최고가는
    오르기만 하므로, 이 값은 실제 해제가의 하한이다(값이 낮아지지는 않는다).
    """
    if k < 0 or k > 4:                 # k가 5 이상이면 ①의 기준일이 미래라 계산 불가
        return None
    need = 16 - k
    if not closes or len(closes) < need:
        return None
    c = [x[1] for x in closes]
    t1 = c[-(6 - k)] * 1.60            # ① 5일 전날 대비 60% 상승선
    t2 = c[-(16 - k)] * 2.00           # ② 15일 전날 대비 100% 상승선
    t3 = float(max(c[-(15 - k):]))     # ③ 최근 15일 최고종가선(확정분)
    return int(min(t1, t2, t3))


def enrich_release_prices(warn_out):
    """지정 중이고 RELEASE_FROM_DAY 이상인 투경 종목에 해제가격을 채운다.

    대시보드는 조회 시점 기준으로 일차를 다시 계산하므로 서버 기준보다
    하루 앞설 수 있다. 그래서 한 칸 여유(-1)를 두고 미리 계산해둔다.
    """
    if price_source is None:
        return
    targets = [w for w in warn_out
               if not w["released"] and w["elapsed"] >= RELEASE_FROM_DAY - 1 and w.get("code")]
    if not targets:
        return

    ok, used = 0, set()
    for w in targets:
        try:
            closes, src = price_source.fetch_daily_closes(w["code"], count=25, want_source=True)
            # 해제 판단은 JUDGE_DAY(10일차)부터. 아직 못 미쳤으면 그날까지 남은 거래일수.
            k = max(0, JUDGE_DAY - w["elapsed"])
            p = release_price(closes, k)
            if p:
                last = closes[-1][1]
                w["release_price"] = p
                w["release_base"] = closes[-1][0]      # 계산에 쓴 최신 종가일
                w["release_pending"] = k               # 0이면 확정 판단 구간, >0이면 예상치
                w["last_close"] = last
                # 판단 구간(k=0)에서 판단일 종가가 기준가 미만이면 요건 충족
                # → 다음 거래일에 해제된다. k>0 은 아직 판단일 전이라 단정하지 않는다.
                w["release_ready"] = bool(k == 0 and last < p)
                # '내일 해제가' = 다음 거래일(T+1) 기준가.
                # 이미 판단 구간(k=0)이고 오늘 요건을 못 채웠을 때만 의미가 있다.
                #  - k>0 이면 위 해제가 자체가 이미 미래(10일차) 값이라 중복
                #  - 이미 충족했으면 내일 해제되므로 볼 필요 없음
                if k == 0 and not w["release_ready"]:
                    pn = release_price(closes, 1)
                    if pn:
                        w["release_price_next"] = pn
                ok += 1
                used.add(src)
        except Exception as e:
            print("[alert] 해제가격 실패 %s(%s): %s" % (w["name"], w["code"], str(e)[:200]))
        time.sleep(0.4)                                # 호출 간격 여유(상대 서버 배려)
    print("[alert] 해제가격 계산 %d/%d 종목%s"
          % (ok, len(targets), (" (소스: %s)" % ",".join(sorted(used))) if used else ""))


def save(data: dict):
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_FILE)


def main():
    try:
        load_trading_days()          # collect() 의 기준일 계산보다 먼저
        warn_raw, danger_raw, over_raw, end = collect()
        status = fetch_halt_status()
        halt_disc, notice = fetch_halt_disclosures(end)
        designated = set()
        for r in warn_raw + danger_raw:
            if r.get("kor_isu_nm"):
                designated.add(r["kor_isu_nm"])
        for r in over_raw:
            if r.get("isu_nm"):
                designated.add(r["isu_nm"])
        store = update_halt_store(status, halt_disc, designated, end)
        data = build(warn_raw, danger_raw, over_raw, end, status, notice, store, halt_disc)
        enrich_release_prices(data["warning"])
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
