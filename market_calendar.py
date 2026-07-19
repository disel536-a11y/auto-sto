"""
한국 증시 거래일 달력 (단일 소스).
================================
예전엔 krx_alert.py / stock_monitor.py / dashboard.html 이 각자 공휴일 표를
들고 있어서, 하나만 갱신하고 나머지를 빠뜨리면 N상·일차 계산이 틀어졌다
(2026 제헌절 부활 누락이 실제 사례). 파이썬 쪽은 이 모듈 하나로 통일한다.

주의 — prev/last 의미 구분:
  - prev_bday(d) : d '직전' 거래일 (d 제외).      예) 다음 판단 기준일
  - last_bday(d) : d 포함 그 이하 마지막 거래일.  예) '가장 최근 거래일'
  두 함수는 의미가 달라 섞으면 하루씩 어긋난다. 호출부 의도에 맞게 골라 쓸 것.

가능하면 이 표(고정 목록)보다 price_source.fetch_trading_days() 의 실제
거래일 데이터를 우선 쓰는 게 안전하다. 이 표는 그 데이터가 없을 때의 폴백이다.
"""
import datetime as dt

# 2026년 한국 증시 휴장일
HOLIDAYS = {
    "2026-01-01",
    "2026-02-16", "2026-02-17", "2026-02-18",          # 설날(2026-02-17)
    "2026-03-01", "2026-03-02",                        # 삼일절 + 대체
    "2026-05-01", "2026-05-05",                        # 근로자의날, 어린이날
    "2026-06-03", "2026-06-06",                        # 지방선거일, 현충일
    "2026-07-17",                                      # 제헌절(2026년 공휴일 부활)
    "2026-08-15", "2026-08-17",                        # 광복절 + 대체
    "2026-09-24", "2026-09-25", "2026-09-26",          # 추석(2026-09-25)
    "2026-10-03", "2026-10-05", "2026-10-09",          # 개천절 + 대체, 한글날
    "2026-12-25", "2026-12-31",                        # 성탄절, 연말 휴장
}


def is_bday(d: dt.date) -> bool:
    """평일이면서 휴장일이 아니면 거래일."""
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_bday(d: dt.date) -> dt.date:
    """d '직전' 거래일 (d 자신은 제외)."""
    d = d - dt.timedelta(days=1)
    while not is_bday(d):
        d -= dt.timedelta(days=1)
    return d


def last_bday(d: dt.date) -> dt.date:
    """d 포함, d 이하의 가장 최근 거래일 (d 가 거래일이면 d)."""
    while not is_bday(d):
        d -= dt.timedelta(days=1)
    return d


def next_bday(d: dt.date) -> dt.date:
    """d '직후' 거래일 (d 자신은 제외)."""
    d += dt.timedelta(days=1)
    while not is_bday(d):
        d += dt.timedelta(days=1)
    return d


def busday_count(a: dt.date, b: dt.date) -> int:
    """[a, b) 구간의 거래일 수 (np.busday_count 과 동일 정의)."""
    if a >= b:
        return 0
    n, cur = 0, a
    while cur < b:
        if is_bday(cur):
            n += 1
        cur += dt.timedelta(days=1)
    return n


if __name__ == "__main__":
    import sys
    t = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    print("기준일       :", t, "(거래일)" if is_bday(t) else "(휴장)")
    print("직전 거래일  :", prev_bday(t))
    print("포함 최근    :", last_bday(t))
    print("다음 거래일  :", next_bday(t))
