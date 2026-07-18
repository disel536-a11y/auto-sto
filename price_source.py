# -*- coding: utf-8 -*-
"""
일별 종가 수집기 (투자경고 해제가격 계산용)
=============================================
종목의 최근 일별 종가를 [(YYYYMMDD, 종가)] 오름차순으로 돌려준다.

여러 소스를 순서대로 시도하고, 먼저 성공하는 것을 쓴다:
  1) 키움 REST API  - config 에 키가 있고 인증이 되면. (NXT 확장 여지)
  2) KRX Data       - data.krx.co.kr 개별종목 시세추이
  3) 네이버 금융     - finance.naver.com 일별시세 (봇이 이미 쓰는 소스)

어느 것이 살아있든 기능이 죽지 않게 하려는 구조다.

점검:
    python3 price_source.py            # 삼성전자로 세 소스 모두 시험
    python3 price_source.py 005930     # 특정 종목
"""
import re
import json
import datetime as dt

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _short(code):
    """ISIN(KR7005930003) 또는 6자리 → 6자리 단축코드."""
    c = str(code or "").strip().upper()
    if c.startswith("KR") and len(c) == 12:
        return c[3:9]
    d = "".join(ch for ch in c if ch.isdigit())
    if len(d) == 6:
        return d
    if len(d) >= 12:
        return d[1:7]
    return d[:6] if d else c


def _num(s):
    t = re.sub(r"[^\d]", "", str(s or ""))
    return int(t) if t else None


# ── 1) 키움 ──────────────────────────────────────────────
def from_kiwoom(code, count=25):
    import kiwoom                     # config 미설정이면 여기서 예외
    return kiwoom.fetch_daily_closes(code, count=count)


# ── 2) KRX Data (data.krx.co.kr) ─────────────────────────
KRX_JSON = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_REF = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"


def _krx_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": KRX_REF,
                      "X-Requested-With": "XMLHttpRequest"})
    s.get("https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd", timeout=20)
    return s


def _krx_isin(sess, code):
    r = sess.post(KRX_JSON, data={"bld": "dbms/comm/finder/finder_stkisu",
                                  "mktsel": "ALL", "searchText": _short(code),
                                  "typeNo": "0"}, timeout=20)
    rows = r.json().get("block1") or []
    for x in rows:
        if x.get("short_code") == _short(code):
            return x.get("full_code")
    return rows[0].get("full_code") if rows else None


def from_krx(code, count=25):
    sess = _krx_session()
    isin = _krx_isin(sess, code)
    if not isin:
        raise RuntimeError("KRX 에서 ISIN 을 찾지 못했습니다: %s" % code)

    end = dt.date.today()
    strt = end - dt.timedelta(days=count * 2 + 20)   # 휴장일 감안 여유
    r = sess.post(KRX_JSON, data={
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01701",
        "isuCd": isin, "isuCd2": _short(code),
        "strtDd": strt.strftime("%Y%m%d"), "endDd": end.strftime("%Y%m%d"),
        "share": "1", "money": "1", "csvxls_isNo": "false"}, timeout=20)

    txt = r.text.strip()
    if not txt.startswith("{"):
        raise RuntimeError("KRX 응답이 JSON 이 아님 (HTTP %s): %s" % (r.status_code, txt[:80]))
    payload = r.json()
    rows = None
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            rows = v
            break
    if not rows:
        raise RuntimeError("KRX 응답에 데이터가 없음: %s" % list(payload.keys())[:8])

    out = []
    for x in rows:
        d = str(x.get("TRD_DD") or "").replace("/", "").replace("-", "").replace(".", "").strip()
        c = _num(x.get("TDD_CLSPRC") or x.get("CLSPRC"))
        if len(d) == 8 and c:
            out.append((d, c))
    if not out:
        raise RuntimeError("KRX 행 파싱 실패. 키: %s" % list(rows[0].keys())[:12])
    out.sort()
    return out[-count:]


# ── 3) 네이버 금융 일별시세 ───────────────────────────────
NAVER_DAY = "https://finance.naver.com/item/sise_day.naver"


def from_naver(code, count=25):
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Referer": "https://finance.naver.com/item/sise.naver?code=" + _short(code)})
    got = {}
    pages = max(1, (count // 10) + 2)          # 한 페이지 10행
    for page in range(1, pages + 1):
        r = sess.get(NAVER_DAY, params={"code": _short(code), "page": page}, timeout=20)
        html = r.content.decode("euc-kr", "replace")
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", tr)
            if not m:
                continue
            tds = re.findall(r'<td class="num"[^>]*>(.*?)</td>', tr, re.S)
            if not tds:
                continue
            close = _num(re.sub(r"<[^>]+>", "", tds[0]))
            if close:
                got[m.group(1) + m.group(2) + m.group(3)] = close
        if len(got) >= count:
            break
    if not got:
        raise RuntimeError("네이버 일별시세 파싱 실패 (마크업 변경 가능)")
    return sorted(got.items())[-count:]


# ── 통합 ─────────────────────────────────────────────────
SOURCES = [("kiwoom", from_kiwoom), ("krx", from_krx), ("naver", from_naver)]


def fetch_daily_closes(code, count=25, want_source=False, quiet=True):
    """살아있는 소스로 최근 일별 종가를 가져온다. 전부 실패하면 RuntimeError."""
    errs = []
    for name, fn in SOURCES:
        try:
            out = fn(code, count=count)
            if out and len(out) >= 15:
                return (out, name) if want_source else out
            errs.append("%s: 데이터 %d개(부족)" % (name, len(out or [])))
        except Exception as e:
            errs.append("%s: %s" % (name, e))
        if not quiet:
            print("   -", errs[-1])
    raise RuntimeError("모든 소스 실패 → " + " | ".join(errs))


# ── 실제 거래일 달력 ──────────────────────────────────────
# 공휴일표를 손으로 관리하지 않기 위해, 대형주의 실제 거래일을 달력으로 쓴다.
# 임시휴장·공휴일 변경(예: 2026년 제헌절 부활)도 자동으로 반영된다.
CALENDAR_CODE = "005930"      # 삼성전자: 거래정지 이력이 사실상 없어 달력용으로 적합


def fetch_trading_days(count=90):
    """최근 실제 거래일을 ['YYYY-MM-DD', ...] 오름차순으로 반환."""
    closes = fetch_daily_closes(CALENDAR_CODE, count=count)
    return ["%s-%s-%s" % (d[:4], d[4:6], d[6:8]) for d, _ in closes]


def _selftest(code="005930"):
    print("=" * 60)
    print("일별 종가 소스 점검 -", code)
    print("=" * 60)
    ok = []
    for name, fn in SOURCES:
        try:
            out = fn(code, count=20)
            if out:
                print("[%-6s] 성공 - %d개, 최근: %s %s원"
                      % (name, len(out), out[-1][0], format(out[-1][1], ",")))
                ok.append(name)
            else:
                print("[%-6s] 빈 결과" % name)
        except Exception as e:
            print("[%-6s] 실패 - %s" % (name, str(e)[:160]))
    print("-" * 60)
    if ok:
        print("사용 가능한 소스:", ", ".join(ok), " → 우선순위 상 '%s' 사용" % ok[0])
        try:
            td = fetch_trading_days(20)
            print("실제 거래일 달력 : 최근 %d일, 마지막 %s" % (len(td), td[-1]))
            print("                   최근 5일 %s" % ", ".join(td[-5:]))
        except Exception as e:
            print("거래일 달력 실패 :", str(e)[:160])
        return 0
    print("사용 가능한 소스가 없습니다. 위 오류를 알려주세요.")
    return 1


if __name__ == "__main__":
    import sys
    a = [x for x in sys.argv[1:] if x.isdigit()]
    raise SystemExit(_selftest(a[0] if a else "005930"))
