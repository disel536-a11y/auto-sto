# -*- coding: utf-8 -*-
"""
키움증권 REST API 클라이언트 (시세 조회 전용)
==============================================
투자경고 해제가격 계산에 필요한 '종목별 최근 일별 종가'를 가져온다.

- 조회(Read) 전용이다. 주문/매매 관련 기능은 의도적으로 넣지 않았다.
- OCX/영웅문 불필요. 순수 REST 라 리눅스 서버에서도 동작한다.
- 토큰은 kiwoom_token.json 에 캐시하고 만료 전까지 재사용한다.

설정: config.py 에 아래 값을 넣는다.
    KIWOOM_APP_KEY    = "..."
    KIWOOM_SECRET_KEY = "..."
    KIWOOM_MOCK       = False   # True 면 모의투자 서버 사용

자체 진단:
    python3 kiwoom.py            # 토큰 발급 + 삼성전자 일봉 조회 확인
    python3 kiwoom.py 005930     # 특정 종목으로 확인
    python3 kiwoom.py 005930 raw # 응답 원문(구조 파악용)까지 출력
"""
import os
import json
import time
import datetime as dt

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "kiwoom_token.json")

REAL_HOST = "https://api.kiwoom.com"
MOCK_HOST = "https://mockapi.kiwoom.com"

# 거래소 구분 접미사 (키움 표기)
#   ""    → KRX(한국거래소) 정규시장.  시장경보 판단 기준이라 기본값.
#   "_AL" → 통합(KRX+NXT)
#   "_NX" → NXT(넥스트레이드) 단독
SUFFIX_KRX, SUFFIX_UNIFIED, SUFFIX_NXT = "", "_AL", "_NX"


def _cfg():
    try:
        import config
    except ImportError:
        raise RuntimeError("config.py 가 없습니다. config.example.py 를 복사해 만드세요.")
    app = getattr(config, "KIWOOM_APP_KEY", "")
    sec = getattr(config, "KIWOOM_SECRET_KEY", "")
    if not app or not sec or app.startswith("여기에"):
        raise RuntimeError("config.py 에 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 를 설정하세요.")
    mock = bool(getattr(config, "KIWOOM_MOCK", False))
    return app, sec, (MOCK_HOST if mock else REAL_HOST)


# ── 토큰 ────────────────────────────────────────────────
def _load_token(host):
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("host") == host and d.get("token") and time.time() < d.get("exp", 0) - 300:
            return d["token"]
    except Exception:
        pass
    return None


def _save_token(host, token, ttl):
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"host": host, "token": token, "exp": time.time() + ttl}, f)
    os.replace(tmp, TOKEN_FILE)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def get_token(force=False):
    """접근토큰 발급(캐시 사용). 실패 시 RuntimeError."""
    app, sec, host = _cfg()
    if not force:
        t = _load_token(host)
        if t:
            return t, host

    res = requests.post(
        host + "/oauth2/token",
        json={"grant_type": "client_credentials", "appkey": app, "secretkey": sec},
        headers={"Content-Type": "application/json;charset=UTF-8"},
        timeout=20,
    )
    try:
        d = res.json()
    except Exception:
        raise RuntimeError("토큰 응답이 JSON 이 아닙니다 (HTTP %s): %s" % (res.status_code, res.text[:200]))

    token = d.get("token") or d.get("access_token")
    if not token:
        raise RuntimeError("토큰 발급 실패 (HTTP %s) return_code=%s msg=%s"
                           % (res.status_code, d.get("return_code"), d.get("return_msg")))

    # expires_dt 는 'YYYYMMDDHHMMSS' 형식. 파싱 실패하면 12시간으로 둔다.
    ttl = 12 * 3600
    exp = str(d.get("expires_dt") or "")
    if len(exp) == 14 and exp.isdigit():
        try:
            ttl = max(600, (dt.datetime.strptime(exp, "%Y%m%d%H%M%S") - dt.datetime.now()).total_seconds())
        except Exception:
            pass
    _save_token(host, token, ttl)
    return token, host


# ── 응답 파싱(필드명 변형에 방어적) ──────────────────────
_DATE_KEYS = ("dt", "base_dt", "stck_bsop_date", "date", "trd_dd")
_CLOSE_KEYS = ("cur_prc", "clos_prc", "close_pric", "stck_clpr", "close", "prc")


def _pick_rows(payload):
    """응답 dict 안에서 '일봉 리스트'로 보이는 첫 번째 값을 찾는다."""
    if not isinstance(payload, dict):
        return []
    # 이름이 명확한 키 우선
    for k, v in payload.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "chart" in k.lower():
            return v
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def _num(s):
    """'-12,345' / '+12345' → 12345. 부호는 등락 표시라 절댓값을 쓴다."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("+", "").strip()
    if t.startswith("-"):
        t = t[1:]
    if not t or not t.replace(".", "", 1).isdigit():
        return None
    return int(float(t))


def _row_date(r):
    for k in _DATE_KEYS:
        v = r.get(k)
        if v and str(v).strip().isdigit() and len(str(v).strip()) == 8:
            return str(v).strip()
    return None


def _row_close(r):
    for k in _CLOSE_KEYS:
        if k in r:
            n = _num(r[k])
            if n:
                return n
    return None


def norm_code(code):
    """종목코드를 키움이 받는 6자리 단축코드로 정규화.

    KRX 쪽 응답은 단축코드(005930)일 수도, ISIN(KR7005930003)일 수도 있다.
    """
    c = str(code or "").strip().upper()
    if c.startswith("KR") and len(c) == 12:      # ISIN → 가운데 6자리
        return c[3:9]
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) == 6:
        return digits
    if len(digits) >= 12:                         # 숫자만 남은 ISIN 형태
        return digits[1:7]
    return digits[:6] if digits else c


# ── 일봉 조회 ────────────────────────────────────────────
def fetch_daily_closes(code, count=25, base_date=None, suffix=SUFFIX_KRX, raw=False):
    """종목의 최근 일별 종가를 [(YYYYMMDD, 종가)] 로 반환(날짜 오름차순).

    code      : 6자리 종목코드
    count     : 최근 몇 거래일치가 필요한지
    base_date : 기준일(YYYYMMDD). 기본은 오늘.
    suffix    : SUFFIX_KRX / SUFFIX_UNIFIED / SUFFIX_NXT
    raw       : True 면 (종가리스트, 응답원문) 튜플 반환
    """
    token, host = get_token()
    base = base_date or dt.date.today().strftime("%Y%m%d")

    res = requests.post(
        host + "/api/dostk/chart",
        json={"stk_cd": norm_code(code) + suffix, "base_dt": base, "upd_stkpc_tp": "1"},
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": "Bearer " + token,
            "api-id": "ka10081",          # 주식일봉차트조회요청
            "cont-yn": "N",
            "next-key": "",
        },
        timeout=20,
    )
    try:
        payload = res.json()
    except Exception:
        raise RuntimeError("일봉 응답이 JSON 이 아닙니다 (HTTP %s): %s" % (res.status_code, res.text[:200]))

    rows = _pick_rows(payload)
    out = []
    for r in rows:
        d, c = _row_date(r), _row_close(r)
        if d and c:
            out.append((d, c))

    out.sort(key=lambda x: x[0])          # 날짜 오름차순
    # 중복 날짜 제거(뒤엣것 우선)
    dedup = {}
    for d, c in out:
        dedup[d] = c
    out = sorted(dedup.items())
    out = out[-count:] if count else out

    return (out, payload) if raw else out


# ── 자체 진단 ────────────────────────────────────────────
def _selftest(code="005930", show_raw=False):
    print("=" * 60)
    try:
        app, sec, host = _cfg()
        print("서버      :", host, "(모의)" if host == MOCK_HOST else "(실전)")
        print("앱키      :", app[:6] + "…" + app[-4:])
    except Exception as e:
        print("[설정 오류]", e)
        return 1

    try:
        token, host = get_token(force=True)
        print("토큰 발급 : 성공 (길이 %d)" % len(token))
    except Exception as e:
        print("[토큰 실패]", e)
        print("\n→ 앱키/시크릿이 맞는지, 실전키인데 KIWOOM_MOCK=True 로 돼있진 않은지 확인하세요.")
        return 1

    try:
        closes, payload = fetch_daily_closes(code, count=20, raw=True)
    except Exception as e:
        print("[일봉 실패]", e)
        return 1

    if not closes:
        print("[일봉 파싱 실패] 응답은 왔지만 종가를 못 찾았습니다.")
        print("응답 최상위 키:", list(payload.keys())[:20])
        rows = _pick_rows(payload)
        if rows:
            print("행 샘플 키    :", list(rows[0].keys()))
            print("행 샘플 값    :", json.dumps(rows[0], ensure_ascii=False)[:400])
        else:
            print("응답 원문:", json.dumps(payload, ensure_ascii=False)[:600])
        print("\n→ 위 출력을 그대로 알려주시면 파서를 맞추겠습니다.")
        return 1

    print("일봉 조회 : 성공 - %s 최근 %d거래일" % (code, len(closes)))
    for d, c in closes[-5:]:
        print("            %s  종가 %s원" % (d, format(c, ",")))
    if show_raw:
        print("\n행 샘플 키:", list(_pick_rows(payload)[0].keys()))
    print("=" * 60)
    print("정상입니다. 이제 krx_alert.py 가 해제가격을 계산할 수 있습니다.")
    return 0


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    c = args[0] if args and args[0].isdigit() else "005930"
    raise SystemExit(_selftest(c, show_raw=("raw" in args)))
