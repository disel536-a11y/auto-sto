# config.py 예시 파일.
# 이 파일을 복사해서 'config.py' 로 저장하고, 아래 값을 실제 키로 바꾸세요.
#   cp config.example.py config.py   (그 뒤 config.py 편집)

GEMINI_API_KEY = "여기에_제미나이_API_키"
KAKAO_REST_API_KEY = "여기에_카카오_REST_API_키"

# ── 키움증권 REST API (시세 조회용) ──
# openapi.kiwoom.com 로그인 → API 사용신청 → 앱키/시크릿 발급.
# 투자경고 해제가격 계산용 일별 종가 조회에만 사용합니다(주문 기능 없음).
KIWOOM_APP_KEY = "여기에_키움_APP_KEY"
KIWOOM_SECRET_KEY = "여기에_키움_SECRET_KEY"
KIWOOM_MOCK = False   # 모의투자 키면 True

# 웹 로그인 세션 서명 키(선택). 비워두면 app.py 가 secret.key 파일을 자동 생성합니다.
# 여러 서버/재배포에도 세션을 유지하려면 아래에 임의의 긴 문자열을 넣으세요.
# SECRET_KEY = "여기에_임의의_긴_랜덤_문자열"
