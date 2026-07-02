"""
친구 추가 도우미 — 본인(관리자) PC에서 실행.
친구에게 '발급 링크'를 보내고, 친구가 보내준 URL을 붙여넣으면 친구 토큰을 등록합니다.
등록된 친구는 봇이 알림 보낼 때 자동 수신 + 토큰 자동 갱신.

실행: python kakao_add_friend.py

※ 사전 준비(최초 1회): developers.kakao.com 에서 친구를 '팀원'으로 초대해야
  친구가 로그인/동의할 수 있습니다. (자세한 건 친구_초대_메뉴얼 참고)
"""
import requests
from urllib.parse import urlparse, parse_qs
from stock_monitor import KAKAO_REST_API_KEY, _load_friends, _save_friends

REDIRECT_URI = "https://example.com/oauth"   # 카카오 앱에 등록된 Redirect URI 와 동일해야 함


def auth_url() -> str:
    return (f"https://kauth.kakao.com/oauth/authorize?client_id={KAKAO_REST_API_KEY}"
            f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=talk_message")


def main():
    print("=" * 56)
    print("  카카오 친구 추가 도우미")
    print("=" * 56)
    print("\n[1] 아래 '발급 링크'를 친구에게 카톡으로 그대로 보내세요:\n")
    print("    " + auth_url())
    print("\n[2] 친구가 할 일:")
    print("    - 링크 열기 → 카카오 로그인 → '동의하고 계속하기' 클릭")
    print("    - 빈 화면(example.com)으로 이동되면, 그 화면의 '주소창 전체'를 복사해")
    print("      당신에게 카톡으로 보내달라고 하세요. (주소에 code= 가 들어있어야 정상)\n")

    pasted = input("[3] 친구가 보내준 URL(또는 code) 붙여넣기:\n> ").strip()
    code = pasted
    if "code=" in pasted:
        q = parse_qs(urlparse(pasted).query)
        code = (q.get("code") or [""])[0]
    if not code:
        print("❌ URL 에서 code 를 찾지 못했습니다. 친구에게 받은 주소 전체를 다시 확인하세요.")
        return

    label = input("[4] 친구 이름(메모용, 예: 철수): ").strip() or "친구"

    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": KAKAO_REST_API_KEY,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=10,
    )
    data = res.json()
    if "access_token" not in data:
        print(f"\n❌ 토큰 발급 실패: {data}")
        print("   • code 는 1회용입니다 — 친구에게 링크를 다시 눌러 '새 URL'을 받아오세요.")
        print("   • 'KOE006' 등 오류면 Redirect URI 설정을, 'KOE320' 이면 친구 팀원 등록을 확인하세요.")
        return

    friends = [f for f in _load_friends() if f.get("label") != label]  # 동일 이름이면 갱신
    friends.append({
        "label": label,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
    })
    _save_friends(friends)
    print(f"\n✅ '{label}' 등록 완료! (현재 친구 {len(friends)}명)")
    print("   이제 봇 알림이 친구 카톡(나와의 채팅)으로도 자동 전송되고, 만료 시 자동 갱신됩니다.")
    print("   확인: python stock_monitor.py --once")


if __name__ == "__main__":
    main()
