"""
카카오 액세스 토큰 발급 도우미
사용법: python kakao_token_setup.py
"""

import webbrowser
import requests
import json
import os

# ✏️ 아래 두 값을 카카오 개발자 콘솔에서 복사해서 입력하세요
REST_API_KEY  = "1be9fee4399068b3dc8216aa5676ec76"
REDIRECT_URI  = "https://example.com/oauth"   # 앱에 등록한 Redirect URI

TOKEN_FILE = "kakao_token.json"


def get_auth_code():
    url = (
        f"https://kauth.kakao.com/oauth/authorize"
        f"?client_id={REST_API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=talk_message"
    )
    print("\n[1단계] 브라우저가 열립니다. 카카오 로그인 후 리다이렉트된 URL을 복사하세요.")
    webbrowser.open(url)
    redirected = input("\n리다이렉트된 전체 URL을 붙여넣으세요:\n> ").strip()
    # URL에서 code 파라미터 추출
    if "code=" in redirected:
        code = redirected.split("code=")[1].split("&")[0]
        print(f"인가 코드: {code}")
        return code
    else:
        raise ValueError("URL에서 code를 찾을 수 없습니다.")


def get_token(auth_code: str) -> dict:
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type":   "authorization_code",
            "client_id":    REST_API_KEY,
            "redirect_uri": REDIRECT_URI,
            "code":         auth_code,
        }
    )
    return res.json()


def refresh_token(refresh_tk: str) -> dict:
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     REST_API_KEY,
            "refresh_token": refresh_tk,
        }
    )
    return res.json()


def save_token(token_data: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 토큰 저장 완료: {TOKEN_FILE}")
    print("   (refresh_token 포함 — stock_monitor.py 가 자동으로 읽고 만료 시 자동 갱신합니다)")
    print("   이제 바로 'python stock_monitor.py --once' 를 실행하면 됩니다.\n")


def main():
    print("=" * 50)
    print("  카카오 액세스 토큰 발급기")
    print("=" * 50)

    # 기존 토큰 파일 있으면 갱신 시도
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            saved = json.load(f)
        print("\n기존 토큰 파일 발견 — 갱신을 시도합니다...")
        new_data = refresh_token(saved.get("refresh_token", ""))
        if "access_token" in new_data:
            saved.update(new_data)
            save_token(saved)
            return

    # 신규 발급
    code = get_auth_code()
    token_data = get_token(code)

    if "access_token" not in token_data:
        print(f"\n❌ 오류: {token_data}")
        return

    save_token(token_data)


if __name__ == "__main__":
    main()
