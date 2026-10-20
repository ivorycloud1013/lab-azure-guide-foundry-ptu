# Microsoft Foundry gpt-image-2.5 HTTP Status Code 가이드

Microsoft Foundry 에 배포한 `gpt-image-2.5` 모델을 운영하면서 발생할 수 있는 HTTP Status Code 와, 상태 코드별 클라이언트 처리 방법을 다룬다.

---

## 목차

1. [HTTP Status Code](#1-http-status-code)
    - [200](#200)
    - [400](#400)
    - [401](#401)
    - [403](#403)
    - [404](#404)
    - [429](#429)
    - [500](#500)
2. [클라이언트 코드](#2-클라이언트-코드)
3. [실제 배포 검증](#3-실제-배포-검증)

---

## 1. HTTP Status Code

### 200

- 원인: 정상 응답
- 대응: base64 디코딩 후 저장

```json
{
  "created": 1698116662,
  "data": [
    { "b64_json": "<base64 image data>" }
  ],
  ...
}
```

### 400

콘텐츠 필터(입력 · 출력)와 파라미터 오류로 나뉜다.

#### 콘텐츠 필터

- 원인: Content Filter 에 걸림
- 대응: 프롬프트 · 이미지 수정. 필요 시 콘텐츠 필터 정책 조정 신청

```json
{
  "error": {
    "message": "Your request was rejected by the safety system. If you believe this is an error, contact us at Azure support ticket and include the request ID <request-id>.",
    "type": "image_generation_user_error",
    "param": null,
    "code": "moderation_blocked",
    "moderation_details": {
      "moderation_stage": "input",
      "categories": ["other"]
    }
  }
}
```

`content_policy_violation` 형식이면 `inner_error.content_filter_results` 에서 걸린 카테고리를 확인한다.

```json
{
  "error": {
    "code": "content_policy_violation",
    "message": "Your request was rejected as a result of our safety system. ...",
    "inner_error": {
      "code": "ResponsibleAIPolicyViolation",
      "content_filter_results": { }
    }
  }
}
```

#### 파라미터 오류

- 원인: 요청의 입력 파라미터 제약 및 형식 오류
- 대응: 요청 전 클라이언트에서 파라미터 검증

```json
{
  "error": {
    "message": "Invalid size '1000x1000'. Width and height must both be divisible by 16.",
    "type": "image_generation_user_error",
    "param": "size",
    "code": "invalid_value"
  }
}
```

### 401

- 원인: API key 오류 · 만료, Entra 토큰 scope 오류, endpoint 오류
- 대응: 인증 사항 확인

```json
{
  "error": {
    "code": "401",
    "message": "Access denied due to invalid subscription key or wrong API endpoint. Make sure to provide a valid key for an active subscription and use a correct regional API endpoint for your resource."
  }
}
```

### 403

RBAC 과 네트워크 차단으로 나뉜다.

#### RBAC

- 원인: RBAC (예를 들어, `Cognitive Services OpenAI User`) 역할 없음
- 대응: RBAC 부여 (반영까지 수 분 소요)

```json
{
  "error": {
    "code": "PermissionDenied",
    "message": "The principal <id> lacks the required data action ... to perform POST /openai/deployments/{deployment-id}/images/edits operation."
  }
}
```

#### 네트워크

- 원인: 네트워크 ACL · Private Endpoint 로 차단
- 대응: 방화벽 허용 IP · VNet · Private Endpoint 설정 확인

```json
{
  "error": {
    "code": "403",
    "message": "Public access is disabled. Please configure private endpoint."
  }
}
```

### 404

- 원인: URL 경로 오류
- 대응: URL 경로 확인, 배포 생성 직후라면 수십 초 ~ 5분 대기 후 재시도

```json
{
  "error": {
    "code": "DeploymentNotFound",
    "message": "The API deployment for this resource does not exist. If you created the deployment within the last 5 minutes, please wait a moment and try again."
  }
}
```

### 429

- 원인: 모델 배포의 사용 quota 초과
- 대응: 응답 헤더 `retry-after-ms` 만큼 대기 후 재시도 ([PTU 가이드](../README.md#33-foundry-model-ptu-deploy-429-spilloverpy) 참고)

```json
{
  "error": {
    "code": "RateLimitReached",
    "message": "Your requests to gpt-image-2.5-flare for gpt-image-2.5-flare in East US 2 have exceeded the call rate limit for your current AIServices S0 pricing tier. This request was for ImageGenerations_Create under OpenAI Language Model Instance API. Please retry after 26 seconds. To increase your default rate limit, visit: https://aka.ms/oai/quotaincrease."
  }
}
```

### 500

- 원인: 서비스 내부 오류
- 대응: 지수 백오프(1s → 2s → 4s …, 최대 5회)로 재시도. 문제 지속되면 `request id` 를 첨부해 담당자 확인 요청

```json
{
  "error": {
    "message": "The server had an error processing your request. Sorry about that! You can retry your request, or contact us through an Azure support request at: https://go.microsoft.com/fwlink/?linkid=2213926 if you keep seeing this error. (Please include the request ID <request-id> in your email.)",
    "type": "server_error",
    "param": null,
    "code": null
  }
}
```

---

## 2. 클라이언트 코드

[`gpt-image-edit.py`](gpt-image-edit.py) 는 `images.edit` 를 한 번 호출하고 상태 코드별로 분기하는 스니펫이다. openai SDK 가 상태 코드마다 예외 클래스를 주므로 예외를 따로 정의하지 않는다.

| 상태 코드 | SDK 예외 |
|---|---|
| `400` | `openai.BadRequestError` — 콘텐츠 필터와 파라미터 오류가 모두 400 이고 `code` 값이 여러 가지라 구분하지 않는다. `code` · `param` · `message` 를 남기고 세부는 `e.body` 에서 본다 |
| `401` | `openai.AuthenticationError` |
| `403` | `openai.PermissionDeniedError` |
| `404` | `openai.NotFoundError` |
| `429` | `openai.RateLimitError` |
| `5xx` | `openai.InternalServerError` |
| 타임아웃 · 연결 실패 | `openai.APITimeoutError` · `openai.APIConnectionError` |

예외는 `message` · `code` · `param` · `type` · `status_code` · `body` 를 가진다. SDK 가 해주지 않아 스니펫에 함수로 둔 것은 두 가지다.

- `request_id()` — Azure 는 `apim-request-id` 로 주는데 SDK 의 `e.request_id` 는 `x-request-id` 만 읽는다
- `retry_after_ms()` — 429 를 직접 처리할 때 쓴다. 본문 메시지가 아니라 헤더를 본다