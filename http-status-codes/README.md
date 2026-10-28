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

같은 400 이라도 `code` 와 `type` 이 제각각이다. 아래는 배포에 실제로 요청해 받은 값이다.

| 요청 | `code` | `type` | `param` |
|---|---|---|---|
| `prompt` 누락 | `missing_required_parameter` | `invalid_request_error` | `prompt` |
| `edits` 에 `image` 누락 | `missing_required_parameter` | `invalid_request_error` | `image` |
| `prompt: ""` | `empty_string` | `invalid_request_error` | `prompt` |
| `prompt` 32,000자 초과 | `string_above_max_length` | `invalid_request_error` | `prompt` |
| `size: "1000x1000"` | `invalid_value` | `image_generation_user_error` | `size` |
| `quality: "ultra"` | `invalid_value` | `invalid_request_error` | `quality` |
| `n: 99` (최대 10) | `integer_above_max_value` | `invalid_request_error` | `n` |
| 정의되지 않은 필드 | `unknown_parameter` | `invalid_request_error` | 해당 필드명 |
| `Content-Type: text/plain` | `unsupported_content_type` | `invalid_request_error` | `null` |
| `image` 가 지원하지 않는 형식 | `unsupported_file_mimetype` | `invalid_request_error` | `image` |
| `image` 가 깨진 파일 | `invalid_image_file` | `image_generation_user_error` | `null` |

`param` 이 있는 경우와 없는 경우가 섞여 있고, 같은 `invalid_value` 가 `type` 을 달리 받기도 한다. `code` 로 분기하지 말고 `code` · `param` · `message` 를 그대로 남기는 편이 안전하다.

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

`edits` 의 `image` 가 지원하는 형식은 `image/jpeg` · `image/png` · `image/webp` 다.

```json
{
  "error": {
    "message": "Invalid file 'image': unsupported mimetype ('text/plain'). Supported file formats are 'image/jpeg', 'image/png', and 'image/webp'.",
    "type": "invalid_request_error",
    "param": "image",
    "code": "unsupported_file_mimetype"
  }
}
```

#### 지원하지 않는 operation

- 원인: 배포 모델이 지원하지 않는 operation 호출 (예를 들어, 이미지 배포에 `chat/completions`)
- 대응: 요청 경로 확인. `code` · `type` · `param` 이 모두 없어 `message` 로만 판별된다

```json
{
  "error": {
    "message": "The requested operation is unsupported."
  }
}
```

### 401

- 원인: API key 오류 · 만료, Entra 토큰 scope 오류, endpoint 오류
- 대응: 인증 사항 확인

**401 은 본문 형태가 세 가지다.** 어느 쪽이 오는지는 인증 방식과 실패 지점에 따라 갈린다.

api-key 계열 — api-key 가 틀렸거나, 인증 헤더가 아예 없거나, `Bearer` 뒤에 토큰이 없을 때.

```json
{
  "error": {
    "code": "401",
    "message": "Access denied due to invalid subscription key or wrong API endpoint. Make sure to provide a valid key for an active subscription and use a correct regional API endpoint for your resource."
  }
}
```

Entra 토큰 계열 — scope(audience) 가 다르거나, 서명이 깨졌거나, `Bearer` 접두사 없이 토큰만 보냈을 때.

```json
{
  "statusCode": 401,
  "message": "Unauthorized. Access token is missing, invalid, audience is incorrect (https://cognitiveservices.azure.com or https://ai.azure.com), or have expired."
}
```

토큰이 **다른 테넌트**에서 발급된 경우는 401 이 아니라 **400** 이고, `code` 자리에 코드가 아니라 문장이 들어온다.

```json
{
  "error": {
    "code": "Tenant provided in token does not match resource token",
    "message": "Token tenant <tenant-id> does not match resource tenant."
  }
}
```

역할이 없는 principal — 토큰 자체는 유효하지만 그 배포에 Cognitive Services 계열 역할이 없을 때. **403 이 아니라 401 이다.**

```json
{
  "error": {
    "code": "PermissionDenied",
    "message": "Principal does not have access to API/Operation."
  }
}
```

**Entra 계열 401 은 `error` 래퍼가 없다.** `error.code` · `error.type` · `error.param` 이 전부 없고, openai SDK 의 `e.code` 도 `None` 이 된다. 401 을 `e.code` 로 분기하면 Entra 인증 실패를 놓친다. 상태 코드로 판단하고 본문은 그대로 남기는 편이 안전하다.

### 403

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

**RBAC 미부여는 403 이 아니라 401 로 온다.** 역할 없는 principal 로 호출해 확인했다 ([401](#401) 참고). 문서에서 흔히 보이는 403 `PermissionDenied` + "lacks the required data action" 본문은 이 배포에서 재현되지 않았다. 403 을 "권한 문제" 로만 읽고 분기하면 RBAC 오류를 놓친다.

### 404

- 원인: 배포 이름 오류, URL 경로 오류
- 대응: 배포 이름(`model` 파라미터)과 URL 경로 확인, 배포 생성 직후라면 수십 초 ~ 5분 대기 후 재시도

```json
{
  "error": {
    "code": "DeploymentNotFound",
    "message": "The API deployment for this resource does not exist. If you created the deployment within the last 5 minutes, please wait a moment and try again."
  }
}
```

경로가 틀리면 `code` 가 `"404"` 인 다른 본문이 온다. `POST` 가 아닌 메서드도 **405 가 아니라 404** 로 온다.

```json
{
  "error": {
    "code": "404",
    "message": "Resource not found"
  }
}
```

**요청 본문의 JSON 이 깨져도 400 이 아니라 404 `DeploymentNotFound` 가 온다.** 어느 배포로 보낼지는 본문의 `model` 로 결정되므로, 본문을 파싱하지 못하면 배포를 찾는 단계에서 먼저 실패한다. 400 파싱 에러를 기대하고 분기하면 안 된다.

### 429

- 원인: 모델 배포의 사용 quota 초과
- 대응: 응답 헤더의 대기 시간만큼 기다린 뒤 재시도 ([PTU 가이드](../README.md#33-foundry-model-ptu-deploy-429-spilloverpy) 참고)

대기 시간 헤더는 **429 를 돌려준 계층**에 따라 다르다. 모델 추론이 돌려준 429 에는 Standard · PTU 모두 `retry-after-ms` 가 오지만, 게이트웨이가 먼저 끊은 429(`RateLimitReached`) 에는 오지 않는다. 한쪽만 읽으면 대기 시간을 놓친다.

| 헤더 | 값 예시 | 의미 |
|---|---|---|
| `retry-after-ms` | `29853` (Standard) · `831` (PTU) | 재시도 대기 밀리초. 더 정밀하므로 우선 쓴다. 게이트웨이 429 에는 오지 않는다 |
| `Retry-After` | `29` (Standard) · `1` (PTU) | 재시도 대기 초. 세 가지 429 에 모두 온다 |
| `x-ratelimit-reset-requests` | `39` | 요청 쿼터 리셋까지 남은 초 |
| `x-ratelimit-limit-requests` | `2` | 분당 허용 요청 수 |
| `x-ratelimit-remaining-requests` | `0` | 남은 요청 수 (429 가 아닌 응답에도 온다) |

본문은 세 가지로 왔다. **`code` 로 분기하면 안 되고 상태 코드로 받아야 한다.**

Standard 추론 — 가장 흔하다. `retry-after-ms` 29853 을 함께 받았다.

```json
{
  "error": {
    "message": "Your requests to gpt-image-2.5-flare for gpt-image-2.5-flare in eastus2 have exceeded request rate limit.",
    "type": "too_many_requests",
    "param": null,
    "code": "rate_limit_exceeded"
  }
}
```

게이트웨이 — `type` · `param` 이 없고 `retry-after-ms` 도 오지 않는다.

```json
{
  "error": {
    "code": "RateLimitReached",
    "message": "Your requests to gpt-image-2.5-flare for gpt-image-2.5-flare in East US 2 have exceeded the call rate limit for your current AIServices S0 pricing tier. This request was for ImageGenerations_Create under OpenAI Language Model Instance API. Please retry after 26 seconds. To increase your default rate limit, visit: https://aka.ms/oai/quotaincrease."
  }
}
```

PTU — `code` 가 `null` 이고 `type` 이 `invalid_request_error` 다. 100 PTU 배포에서 동시 50 건으로 재현했다.

```json
{
  "error": {
    "message": "Rate limit exceeded.",
    "type": "invalid_request_error",
    "param": null,
    "code": null
  }
}
```

**`images/generations` 에서는 잘못된 요청도 rate limit 을 소비하고, 429 가 본문 검증보다 먼저 떨어진다.** 파라미터 오류로 400 이 날 요청이라도 쿼터를 다 쓴 상태면 400 대신 429 가 온다. 반대로 **`images/edits` 는 본문 검증이 먼저**라 같은 상태에서도 400 이 온다. 두 operation 의 순서가 다르므로 한쪽에서 본 순서를 다른 쪽에 적용하면 안 된다.

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
| `400` | `openai.BadRequestError` — 콘텐츠 필터와 파라미터 오류가 모두 400 이고 `code` · `type` 값이 여러 가지라 구분하지 않는다. `code` · `param` · `message` 를 남기고 세부는 `e.body` 에서 본다 |
| `401` | `openai.AuthenticationError` |
| `403` | `openai.PermissionDeniedError` |
| `404` | `openai.NotFoundError` |
| `429` | `openai.RateLimitError` |
| `5xx` | `openai.InternalServerError` |
| 타임아웃 · 연결 실패 | `openai.APITimeoutError` · `openai.APIConnectionError` |

예외는 `message` · `code` · `param` · `type` · `status_code` · `body` 를 가진다. SDK 가 해주지 않아 스니펫에 함수로 둔 것은 두 가지다.

- `request_id()` — Azure 는 `apim-request-id` 로 주는데 SDK 의 `e.request_id` 는 `x-request-id` 만 읽는다
- `retry_after_ms()` — 429 를 직접 처리할 때 쓴다. 본문 메시지가 아니라 헤더를 보고, `retry-after-ms`(밀리초) 가 없으면 `Retry-After`(초) 로 폴백한다

---

## 3. 실제 배포 검증

위 응답 본문은 `gpt-image-2.5-flare` 배포(Global Standard, East US 2)에 실제로 요청해 받은 것이다. 400 계열은 16가지 요청으로 확인했다 (2026-10-27).

확인 전에 예상했던 것과 달랐던 것은 세 가지다.

| 예상 | 실제 |
|---|---|
| `POST` 가 아닌 메서드는 405 | **404** `Resource not found` |
| 요청 JSON 이 깨지면 400 파싱 에러 | **404 `DeploymentNotFound`** — 라우팅이 본문의 `model` 을 먼저 읽는다 |
| `retry-after-ms` 는 PTU 전용 | **Standard 추론 429 에도 온다**(실측 `29853`). 오지 않는 쪽은 게이트웨이 429(`RateLimitReached`) 다 |
| 401 은 한 가지 본문 | **세 가지** — api-key 계열 `code: "401"`, Entra 계열은 `error` 래퍼 없이 `statusCode` · `message`, RBAC 미부여는 `code: "PermissionDenied"` |
| RBAC 미부여는 403 | **401** `PermissionDenied` — 403 은 네트워크 차단만 |
| 다른 테넌트의 토큰은 401 | **400**, `code` 자리에 문장이 들어온다 |

검증용 요청은 `az login` 상태의 Entra ID 토큰으로 보냈고, 400 으로 끝나는 요청은 이미지가 생성되지 않아 과금이 없었다. 다만 capacity 가 작은 배포에서는 그 요청들도 rate limit 을 소비하므로, 파라미터 오류를 확인할 때는 호출 사이에 간격을 둬야 429 에 묻히지 않는다.

401 은 인증을 일부러 틀린 6가지 요청으로 따로 확인했다. **403 과 500 은 재현하지 않았다.** 403 은 랩 계정의 public network access 를 잠시 끄고 받았고(확인 즉시 되돌렸다), RBAC 미부여는 같은 테넌트의 역할 없는 계정으로 호출해 401 이라는 것을 확인했다. 500 만 서버 내부 오류라 요청으로 유발할 수 없어 문서 기준으로 남겼다. 두 응답 본문은 문서 기준이다.