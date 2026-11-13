"""업비트 API 오류 이름을 사람이 바로 조치할 수 있는 안내문으로 바꿉니다.

오류 이름 목록은 업비트 공식 문서 기준이며, 실제 계정으로 모두 재현해 검증하지는 못했습니다.
알 수 없는 이름은 원문을 그대로 보여줍니다.
"""

from __future__ import annotations

HINTS: dict[str, str] = {
    "no_authorization_ip": (
        "이 서버의 공개 IP 가 업비트 '허용 IP' 에 등록되어 있지 않습니다. "
        "python preflight.py 로 이 서버의 공개 IP 를 확인해 업비트 Open API 관리에서 키의 허용 IP 에 추가하세요."
    ),
    "invalid_access_key": "UPBIT_ACCESS_KEY 가 올바르지 않습니다. 복사 중 공백이나 줄바꿈이 섞이지 않았는지 확인하세요.",
    "jwt_verification": "UPBIT_SECRET_KEY 가 액세스 키와 맞지 않습니다. 키 쌍을 다시 확인하세요.",
    "expired_access_key": "API 키가 만료되었습니다. 업비트에서 키를 다시 발급하세요.",
    "out_of_scope": "API 키 권한이 부족합니다. 자산조회, 주문조회, 주문하기 권한을 켜세요(출금하기는 켜지 마세요).",
    "invalid_query_payload": "요청 서명(query_hash) 검증에 실패했습니다. 코드 또는 시계 문제일 수 있습니다.",
    "too_many_requests": "요청 한도를 초과했습니다. 잠시 뒤 다시 시도하세요.",
}

FATAL_AUTH_NAMES = frozenset(
    {"no_authorization_ip", "invalid_access_key", "jwt_verification", "expired_access_key", "out_of_scope"}
)


def explain_upbit_error(name: str | None, message: str = "") -> str:
    if name and name in HINTS:
        return f"[{name}] {HINTS[name]}"
    if name:
        return f"[{name}] {message}".strip()
    return message or "알 수 없는 오류"
