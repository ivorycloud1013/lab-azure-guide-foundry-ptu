"""Microsoft Foundry gpt-image-2.5 — images.edit 호출과 상태 코드별 예외 처리.

Requirements: Python 3.11+, `pip install -r ../requirements.txt`
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import openai
from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

log = logging.getLogger("gpt-image-edit")

ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://<resource>.openai.azure.com/openai/v1/")
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-image-2.5-flare")
ENTRA_SCOPE = "https://ai.azure.com/.default"

INPUT_IMAGE = Path("input.png")       # 장당 50MB 미만, 최대 16장, PNG · JPEG · WEBP
OUTPUT_IMAGE = Path("output.png")

MS_PER_SEC = 1000

def request_id(e: openai.APIStatusError) -> Optional[str]:
    """지원 요청에 첨부할 ID. Azure 는 apim-request-id 로 준다."""
    return e.response.headers.get("apim-request-id") or e.request_id


def retry_after_ms(e: openai.APIStatusError) -> Optional[int]:
    """429 의 대기 시간. 본문 메시지의 'retry after N seconds' 가 아니라 헤더를 본다.

    PTU 배포는 retry-after-ms(밀리초) 로 주지만 Standard 배포는 주지 않는다.
    그쪽은 Retry-After(초) 를 보내므로 둘 다 읽는다.
    """
    headers = e.response.headers
    raw_ms = headers.get("retry-after-ms")
    if raw_ms and raw_ms.isdigit():
        return int(raw_ms)
    raw_sec = headers.get("retry-after") or headers.get("x-ratelimit-reset-requests")
    if raw_sec and raw_sec.isdigit():
        return int(raw_sec) * MS_PER_SEC
    return None


client = OpenAI(
    base_url=ENDPOINT,
    api_key=get_bearer_token_provider(DefaultAzureCredential(), ENTRA_SCOPE),
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        with open(INPUT_IMAGE, "rb") as image:
            result = client.images.edit(
                model=DEPLOYMENT,
                image=image,
                prompt="add a red scarf",
                size="1024x1024",
                quality="low",
            )
        OUTPUT_IMAGE.write_bytes(base64.b64decode(result.data[0].b64_json))
        log.info("saved %s", OUTPUT_IMAGE)
        return 0

    except openai.BadRequestError as e:                       # 400
        # 콘텐츠 필터와 파라미터 오류가 모두 400 이고 code 값이 여러 가지다.
        # 구분하지 않고 그대로 남긴다. 차단 단계 같은 세부 정보는 e.body 에 들어 있다.
        log.error("요청 거부 code=%s param=%s request_id=%s — %s",
                  e.code, e.param, request_id(e), e.message)
        log.debug("400 body: %s", e.body)
        return 1                                              # 고치기 전에는 재시도해도 같다

    except openai.AuthenticationError as e:                   # 401
        # Entra 토큰 계열 401 은 본문이 {"statusCode": 401, "message": …} 라 e.code 가 None 이다.
        # 401 을 code 로 분기하지 않고 예외 클래스로만 받는다.
        log.error("인증 실패 — api-key 또는 토큰 scope(%s) 와 az login 상태 확인. request_id=%s",
                  ENTRA_SCOPE, request_id(e))
        log.debug("401 body: %s", e.body)
        return 1

    except openai.PermissionDeniedError as e:                 # 403
        log.error("권한·네트워크 차단 — RBAC(Cognitive Services OpenAI User) 과 "
                  "방화벽·Private Endpoint 설정 확인. code=%s request_id=%s", e.code, request_id(e))
        return 1

    except openai.NotFoundError as e:                         # 404
        log.error("배포 '%s' 없음 — 배포 이름과 endpoint 경로(/openai/v1/) 확인. request_id=%s",
                  DEPLOYMENT, request_id(e))
        # 배포를 막 만들었다면 전파 전일 수 있다. 수십 초 ~ 5분 기다렸다가 다시 호출한다.
        return 1

    except openai.RateLimitError as e:                        # 429
        log.error("쿼터 초과 code=%s retry_after_ms=%s request_id=%s",
                  e.code, retry_after_ms(e), request_id(e))
        # max_retries=0 으로 뒀다면 여기서 retry_after_ms 만큼 쉬었다가 재시도한다.
        # PTU 라면 기다리는 대신 Standard 배포로 spillover 하는 편이 낫다
        # (레포 루트 foundry-model-ptu-deploy-429-spillover.py 참고).
        return 1

    except openai.InternalServerError as e:                   # 5xx
        # 500 은 error.code 가 null 로 오므로 상태 코드로 판단한다.
        log.error("서비스 오류 status=%s request_id=%s", e.status_code, request_id(e))
        # SDK 가 이미 max_retries 만큼 재시도한 뒤다. 계속되면 request_id 를 첨부해 지원 요청.
        return 1

    except openai.APITimeoutError:                            # 클라이언트 타임아웃
        log.error("클라이언트 타임아웃 — timeout 을 늘리거나 quality · size 를 낮춘다")
        return 1

    except openai.APIConnectionError as e:                    # 연결 실패 (응답 없음)
        log.error("연결 실패 — %s", e)
        return 1

    except Exception:                                         # 예상하지 못한 오류
        log.exception("예외")                                  # 트레이스백까지 남긴다
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
