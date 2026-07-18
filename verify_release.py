# -*- coding: utf-8 -*-
"""
투자경고 해제가격 검증
=======================
해제 판단은 지정 10일차부터 시작된다. 아직 10일차 전이면 '그날 적용될' 값을,
이미 10일차 이상이면 최신 종가일 기준 값을 계산한다.

  k = max(0, 10 - 현재일차),  판단일 D = 최신종가일(T) + k
  [1] D-5  = T-(5-k)
  [2] D-15 = T-(15-k)
  [3] 최근 15일 {D-14..D} 중 확정분 = (15-k)일

- 종가는 price_source 로 실제 수집
- 세 기준값을 이 파일에서 '독립적으로' 다시 계산
- krx_alert.release_price() 결과 및 손계산 예상치와 3중 대조

실행:
    python3 verify_release.py
"""
import price_source
import krx_alert

# (종목코드, 이름, 현재일차, 손계산 예상 해제가)
STOCKS = [
    ("013360", "일성건설",    8,  2085),
    ("092590", "럭스피아",    9,   829),
    ("002990", "금호건설",   12, 10200),
    ("270520", "앱튼",       12,  1976),
    ("002995", "금호건설우", 13, 30800),
]
JUDGE_DAY = 10


def won(n):
    return format(int(n), ",") + "원"


def check(code, name, day, expect):
    k = max(0, JUDGE_DAY - day)
    print("=" * 64)
    print("%s (%s)  %d일차  ->  k=%d  (%s)"
          % (name, code, day, k,
             "이미 판단 구간" if k == 0 else "%d거래일 뒤 10일차 도달" % k))
    print("=" * 64)
    try:
        closes, src = price_source.fetch_daily_closes(code, count=25, want_source=True)
    except Exception as e:
        print("  [수집 실패]", str(e)[:200])
        return None
    need = 16 - k
    if len(closes) < need:
        print("  [데이터 부족] %d개 (필요 %d개)" % (len(closes), need))
        return None

    c = [x[1] for x in closes]
    i1, i2 = len(c) - (6 - k), len(c) - (16 - k)      # 기준일 인덱스
    win_from = len(c) - (15 - k)

    print("  소스: %s / 최근 %d거래일\n" % (src, len(closes)))
    print("  최근 16일 종가:")
    for i in range(max(0, len(c) - 16), len(c)):
        d = closes[i][0]
        tag = []
        if i == i1:
            tag.append("[1]기준")
        if i == i2:
            tag.append("[2]기준")
        if i >= win_from:
            tag.append("[3]윈도우")
        print("    %s-%s-%s  %10s   %s"
              % (d[:4], d[4:6], d[6:8], won(c[i]), " ".join(tag)))

    # --- 독립 계산 ---
    t1 = c[i1] * 1.60
    t2 = c[i2] * 2.00
    t3 = float(max(c[win_from:]))
    mine = int(min(t1, t2, t3))

    print()
    print("  [1] T-%d  %s x 1.60 = %s" % (5 - k, won(c[i1]), won(t1)))
    print("  [2] T-%d %s x 2.00 = %s" % (15 - k, won(c[i2]), won(t2)))
    print("  [3] 최근 %d일 최고종가   = %s" % (15 - k, won(t3)))
    which = {t1: "[1]", t2: "[2]", t3: "[3]"}[min(t1, t2, t3)]
    print("  -> 최소 = %s (%s 이 결정)" % (won(mine), which))

    theirs = krx_alert.release_price(closes, k)
    cur = c[-1]
    print()
    print("  독립 계산      : %s" % won(mine))
    print("  release_price(): %s   %s" % (won(theirs), "일치" if theirs == mine else "*** 불일치 ***"))
    print("  손계산 예상치  : %s   %s" % (won(expect), "일치" if expect == mine else "*** 불일치 ***"))
    print()
    print("  최신 종가(%s): %s  ->  %s"
          % (closes[-1][0], won(cur),
             "해제가 아래 (요건 충족)" if cur < mine else "해제가 위 (아직 미충족)"))
    if k > 0:
        print("  * k>0 이라 [3] 윈도우가 아직 덜 찼음. 남은 날 종가가 채워지며")
        print("    최고가는 오르기만 하므로 이 값은 실제 해제가의 하한임.")

    ok = (theirs == mine == expect)
    print("\n  판정:", "OK" if ok else "*** 확인 필요 ***")
    return ok


def main():
    print("투자경고 해제가격 검증 (10일차 판단 기준 반영)\n")
    res = []
    for code, name, day, expect in STOCKS:
        res.append((name, check(code, name, day, expect)))
        print()
    print("=" * 64)
    print("요약")
    print("=" * 64)
    for name, ok in res:
        print("  %-12s %s" % (name, {True: "OK", False: "확인 필요", None: "수집 실패"}[ok]))
    bad = [n for n, o in res if o is not True]
    print()
    print("전부 일치합니다." if not bad else "확인 필요: " + ", ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
