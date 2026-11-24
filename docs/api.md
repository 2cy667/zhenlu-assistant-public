# 诊录助手接口说明（v0.4）

医生页面的实时提醒与状态更新使用第 7、8 节接口。

独立分段采集网关的可选协议见 [realtime-api.md](realtime-api.md)，默认前端使用本仓库的单文件上传或外部实时转录接口。

- Base URL：`http://127.0.0.1:8000/api`
- 数据格式：普通接口使用 UTF-8 JSON；音频上传使用 `multipart/form-data`，实时订阅使用 SSE。
- 交互式文档：启动后端后访问 `http://127.0.0.1:8000/docs`。
- 语音识别使用 Fun-ASR，草稿和会话问答使用千问，响应中的 `provider` 和 `notice` 标识结果来源。`is_mock` 保留为历史数据来源标记，新手动录入及云端结果为 false。

## 1. 错误格式

除 422 校验错误外，业务错误统一返回：

```json
{
  "code": "unsupported_audio_format",
  "message": "不支持该音频格式（.txt）。支持：flac、m4a、mp3、ogg、wav、webm。"
}
```

422 校验错误返回：

```json
{
  "code": "validation_error",
  "message": "请求参数不合法，请检查后重试。",
  "details": []
}
```

常见错误码：

| HTTP | code | 场景 |
| ---: | --- | --- |
| 400 | `empty_audio` | 上传文件为空 |
| 400 | `unknown_field` | 草稿字段 key 不在五个标准字段中 |
| 404 | `session_not_found` | 会话不存在 |
| 404 | `audio_not_found` / `audio_file_missing` | 尚无音频或文件已丢失 |
| 404 | `transcript_not_found` / `draft_not_found` | 尚未转写或尚未生成草稿 |
| 404 | `record_not_confirmed` | 尚未确认，不能读取确认记录或文本 |
| 409 | `audio_required` | 未上传音频就请求转写 |
| 409 | `transcript_required` | 未完成转写就生成草稿 |
| 409 | `draft_required` | 尚无草稿就保存/确认 |
| 409 | `audio_exists` | 同一会话重复上传音频 |
| 409 | `session_has_processing` | 会话已有处理结果，不能替换音频 |
| 409 | `transcript_exists` / `draft_exists` | 重复执行一次性生成操作 |
| 409 | `session_confirmed` | 已确认后继续修改、生成或上传 |
| 409 | `revision_conflict` | 草稿 revision 过期，需刷新后重试 |
| 413 | `audio_too_large` | 音频超过 `ZHENLU_MAX_AUDIO_MB` |
| 415 | `unsupported_audio_format` | 扩展名不在允许列表 |
| 502 | `asr_failed` / `empty_transcript` | 转写提供方失败或返回空文本 |
| 502 | `fun_asr_auth_failed` | Fun-ASR 鉴权失败 |
| 502 | `fun_asr_connection_failed` | Fun-ASR 无法连接或连接中断 |
| 502 | `fun_asr_timeout` | Fun-ASR 启动、发送音频或等待结果超时 |
| 502 | `fun_asr_failed` | Fun-ASR 返回 `task-failed` |
| 502 | `fun_asr_invalid_response` | Fun-ASR 返回消息格式无效 |
| 502 | `fun_asr_empty_result` | Fun-ASR 未返回最终识别文本 |
| 502 | `fun_asr_unsupported_format` | 音频格式不在 Fun-ASR 支持列表 |
| 502 | `fun_asr_invalid_audio` | WAV 不是单声道 16-bit PCM 或采样率无效 |
| 502 | `fun_asr_input_invalid` | 音频文件为空或无法读取 |
| 502 | `draft_generation_failed` | 草稿提供方失败 |
| 500 | `internal_error` | 未预期的服务端错误 |

## 2. 数据约定

### 2.1 时间与来源

- 所有 API 时间字段为 UTC ISO 8601 字符串，例如 `2026-10-27T08:51:00Z`。
- 转写句子 `start_ms` / `end_ms` 可为 `null`。当识别服务没有可靠时间信息时必须为 `null`，服务端和界面都不会填充推测值。
- 时间戳使用语音服务实际返回的有效时间区间，缺失时为空；手动录入无音频时间戳。
- 草稿字段的 `sources` 是从转写句子中按 ID 解析出的原文。模型返回不属于当前转写的来源 ID 时，服务端拒绝结果。

### 2.2 草稿来源与版本

- `origin` 为 `ai` 或 `doctor`。
- `ai_value` 保留系统首次生成的原始值；医生修改后 `value` 变化，`ai_value` 不变。
- `revision` 是草稿乐观锁。所有修改、重新生成、确认请求都必须携带当前 `expected_revision`。
- 保存字段可传入 `accept_suggestion: true`，显式采纳当前建议的文字及其来源；该字段值必须与当前建议一致。未传入时视为普通医生编辑。
- 重新生成时：
  - `origin=ai` 的字段可更新；
  - `origin=doctor` 的字段不会被覆盖，系统结果写入 `suggestion`，需医生主动采纳。
- 确认后的记录是独立快照，草稿被锁定，不可继续编辑或重新生成。

### 2.3 主要对象

`SessionDetail` 同时是上传、转写、草稿、确认等操作接口的统一返回结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 会话 ID |
| `patient_name` / `patient_ref` | string / string? | 虚构患者称呼与可选编号 |
| `status` | string | 见下方状态表 |
| `status_label` | string | 中文状态标签 |
| `error_message` | string? | 最近一次处理失败的用户提示 |
| `created_at` / `updated_at` | string | UTC 时间 |
| `has_audio` / `has_transcript` / `has_draft` | bool | 资源是否存在 |
| `confirmed_version` | int | 已确认版本号 |
| `audio` | object? | 音频元数据与播放 URL |
| `transcript` | object? | 转写、处理来源与逐句内容 |
| `draft` | object? | 五字段草稿、来源、建议和 revision |
| `latest_record` | object? | 最近一次确认记录快照与纯文本 |

`audio` 字段：

```json
{
  "id": "…",
  "filename": "consultation.wav",
  "content_type": "audio/wav",
  "size_bytes": 3409356,
  "duration_ms": 106446,
  "uploaded_at": "2026-10-27T08:51:00Z",
  "url": "/api/sessions/{session_id}/audio"
}
```

`transcript` 字段：

```json
{
  "id": "…",
  "provider": "fun-asr:fun-asr-realtime-2026-02-28",
  "is_mock": false,
  "language": "zh-CN",
  "notice": "由 Fun-ASR 转写，请核对原文。",
  "revision": 1,
  "state": "final",
  "created_at": "2026-10-27T08:51:10Z",
  "has_reliable_timestamps": true,
  "segments": [
    {
      "id": "…",
      "seq": 0,
      "speaker": "医生",
      "text": "您好，请坐。今天主要是哪里不舒服？",
      "start_ms": 300,
      "end_ms": 5127
    }
  ]
}
```

`draft` 字段：

```json
{
  "id": "…",
  "provider": "qwen:qwen3.7-plus",
  "is_mock": false,
  "notice": "由千问生成，请医生核对。",
  "revision": 1,
  "status": "editing",
  "created_at": "2026-10-27T08:51:20Z",
  "updated_at": "2026-10-27T08:51:20Z",
  "has_doctor_edits": false,
  "has_suggestions": false,
  "fields": [
    {
      "key": "chief_complaint",
      "label": "主诉",
      "value": "咳嗽、咳痰5天，伴发热2天。",
      "ai_value": "咳嗽、咳痰5天，伴发热2天。",
      "origin": "ai",
      "is_doctor_edited": false,
      "edited_at": null,
      "is_unmentioned": false,
      "sources": [
        {
          "id": "…",
          "seq": 1,
          "speaker": "患者",
          "text": "医生，我咳嗽、咳痰五天了，这两天还有点发烧。",
          "start_ms": 5577,
          "end_ms": 10558
        }
      ],
      "suggestion": null
    }
  ]
}
```

`latest_record` 字段：

```json
{
  "id": "…",
  "version": 1,
  "draft_revision": 2,
  "confirmed_at": "2026-10-27T08:53:00Z",
  "text": "诊录助手 · 问诊记录\n患者：演示患者 A（DEMO-001）\n…",
  "fields": [
    {
      "key": "chief_complaint",
      "label": "主诉",
      "value": "…",
      "origin": "doctor",
      "sources": []
    }
  ]
}
```

字段 key 固定为：

| key | label |
| --- | --- |
| `chief_complaint` | 主诉 |
| `present_illness` | 现病史 |
| `past_history` | 既往史 |
| `medication_history` | 用药史 |
| `allergy_history` | 过敏史 |

## 3. 接口清单

### 3.1 系统

#### `GET /health`

返回服务状态和当前提供方。

```json
{
  "status": "ok",
  "asr_provider": "fun-asr:fun-asr-realtime-2026-02-28",
  "asr_is_mock": false,
  "draft_provider": "qwen:qwen3.7-plus",
  "draft_is_mock": false,
  "assistant_provider": "qwen-assistant:qwen3.7-plus",
  "assistant_is_mock": false,
  "job_worker_enabled": true,
  "dialogue_provider": "explicit-dialogue-rules-v1",
  "clinical_advisor": "qwen-clinical-advisor:qwen3.7-plus",
  "live_assistant_enabled": true,
  "live_assistant_worker_enabled": true
}
```

### 3.2 会话

#### `POST /sessions`

创建问诊会话。

```json
{
  "patient_name": "演示患者 A",
  "patient_ref": "DEMO-2026-001"
}
```

- `patient_name`：必填，1–80 字符，自动去除首尾空格。
- `patient_ref`：可选，最多 120 字符。

返回 `201` 与 `SessionDetail`，初始 `status=awaiting_audio`。

#### `GET /sessions`

返回按更新时间倒序的会话摘要：

```json
{
  "items": [
    {
      "id": "…",
      "patient_name": "演示患者 A",
      "patient_ref": "DEMO-2026-001",
      "status": "draft_ready",
      "status_label": "草稿待审核",
      "created_at": "…",
      "updated_at": "…",
      "has_audio": true,
      "has_transcript": true,
      "has_draft": true,
      "confirmed_version": 0
    }
  ]
}
```

#### `GET /sessions/{session_id}`

返回完整 `SessionDetail`，包含音频、转写、草稿和最近确认记录。页面刷新后也通过该接口恢复全部已保存内容。

### 3.3 音频

#### `POST /sessions/{session_id}/audio`

`multipart/form-data`，字段名 `file`。

```bash
curl -X POST "http://127.0.0.1:8000/api/sessions/{session_id}/audio" \
  -F "file=@path/to/consultation.wav"
```

- 允许扩展名：`.wav`、`.mp3`、`.m4a`、`.ogg`、`.webm`、`.flac`。
- 大小上限由 `ZHENLU_MAX_AUDIO_MB` 控制，默认 50 MB。
- 第一阶段每个会话仅允许一段音频；上传成功后状态变为 `audio_ready`。
- 文件保存在 `backend/data/uploads/{session_id}/`，文件名使用服务端生成的 ID。
- 返回 `201` 与 `SessionDetail`。
- 不校验音频内容与扩展名是否真实匹配，不进行转码；这是演示阶段的已知限制。

#### `GET /sessions/{session_id}/audio`

流式返回本地音频文件，供页面播放。没有音频时返回 `404 audio_not_found`。

### 3.4 语音转写

#### `POST /sessions/{session_id}/transcript`

调用 Fun-ASR 转写当前音频，同步接口返回 `SessionDetail`。

- 前置条件：会话已有音频，且尚无转写或上次音频转写已中断。
- `ZHENLU_ASR_PROVIDER=fun-asr` 时，后端通过 WebSocket 将当前音频分片发送至 Fun-ASR 实时接口，仅保留 `sentence_end=true` 的最终句子，并使用接口返回的 `begin_time`/`end_time` 作为毫秒时间戳。
- Fun-ASR 实时结果标记 `transcript.is_mock=false`，`provider` 为 `fun-asr:<model>`；该接口未返回说话人信息，所有句子 `speaker` 为 `未知`。
- Fun-ASR 的 WAV 输入必须为单声道 16-bit PCM；非 WAV 格式使用配置的采样率，必须与实际音频一致。当前支持 `wav`、`mp3`、`aac`、`amr`、`opus`、`speex`；上传允许的 `ogg`、`webm`、`flac`、`m4a` 会返回 `502 fun_asr_unsupported_format`，需要先转码。
- 本接口保持同步兼容；也可提交 transcribe 后台任务。Fun-ASR 仍按约 100 ms 分片发送音频，后台执行不改变传输耗时。
- 若提供方抛错，会话状态变为 `failed`、`error_message` 给出提示，接口返回 `502`；再次调用可重试。
- 最终句子实时保存，`state=streaming`；全部完成后改为 `final` 并递增转录 `revision`。部分失败时保存已识别句子并标记 `interrupted`，禁止直接生成病历。重试会重新识别同一录音并保留匹配句子的 ID；若识别内容与已保存句子冲突，拒绝覆盖，需核对录音或新建会话。更换 ASR 模型也需新建会话。

#### `GET /sessions/{session_id}/transcript`

只返回转写对象 `transcript`，结构同上。

### 3.5 病历草稿

#### `POST /sessions/{session_id}/draft`

基于当前转写生成五个字段。前置条件：已有 `state=final` 的转写、尚无草稿。返回 `SessionDetail`。增量转录未结束时，生成、重生成及角色整理返回 `409 transcript_not_final`。

- 五个字段全部返回；未提及的信息值为“未提及”，不会改成“无”。
- 服务端校验来源句子 ID；真实模型出现非法引用、缺失字段或无来源事实时拒绝整个结果。
- 千问提供方会发送对话文本及句子 ID，并要求严格结构和有效来源；无效结果返回错误，不创建草稿。

#### `GET /sessions/{session_id}/draft`

只返回草稿对象 `draft`。

#### `PATCH /sessions/{session_id}/draft`

保存医生修改。

```json
{
  "expected_revision": 1,
  "fields": [
    {
      "field_key": "chief_complaint",
      "value": "咳嗽、咳黄白痰伴发热5天，医生已核对。"
    }
  ]
}
```

- `fields` 至少 1 项、至多 5 项，字段 key 不可重复。
- 值不能为空，自动去除首尾空格。
- 仅允许五个标准字段。
- 有实际变化时：字段 `origin` 变为 `doctor`，写入 `edited_at`，草稿 `revision` 加 1。
- 若已存在系统建议，医生保存修改后该建议被清除。
- `expected_revision` 与当前 revision 不一致时返回 `409 revision_conflict`。
- 已确认会话返回 `409 session_confirmed`。

#### `POST /sessions/{session_id}/draft/regenerate`

重新生成草稿，用于演示后续生成结果的保护行为。

```json
{ "expected_revision": 2 }
```

- 对 `origin=ai` 的字段：更新 `value`、`ai_value` 和来源。
- 对 `origin=doctor` 的字段：**绝不覆盖**。如果新结果不同，写入：
  - `suggestion.value`
  - `suggestion.sources`
  - `suggestion.created_at`
- 医生可通过 `PATCH` 主动把建议值保存为当前值（此时 `origin=doctor`，建议清除）。
- 无论是否有建议，`revision` 均加 1。

### 3.6 确认、版本与复制

#### `POST /sessions/{session_id}/confirm`

```json
{ "expected_revision": 3 }
```

- 前置条件：存在草稿、未确认、revision 匹配。
- 将当前字段值、来源、`origin` 和纯文本快照写入独立确认记录。
- 会话状态变为 `confirmed`，`confirmed_version` 为该会话的第 N 版。
- 草稿 `status=confirmed` 并被锁定；后续所有修改、重生成、确认和上传均返回 `409`。
- 返回 `SessionDetail`，其中 `latest_record` 为本次确认结果。

#### `GET /sessions/{session_id}/record`

返回最近一次确认记录快照。字段结构见 `latest_record`。

#### `GET /sessions/{session_id}/record/text`

只返回纯文本，供“复制结果”使用：

```json
{
  "version": 1,
  "confirmed_at": "2026-10-27T08:53:00Z",
  "text": "诊录助手 · 问诊记录\n患者：演示患者 A（DEMO-2026-001）\n…"
}
```

未确认时返回 `404 record_not_confirmed`。

纯文本包含患者称呼、会话编号、确认时间、五个字段和说明行。历史模拟数据生成的确认文本保留原说明。

## 4. 状态

| status | 含义 |
| --- | --- |
| `awaiting_audio` | 会话已创建，待录入文字或上传音频 |
| `audio_ready` | 音频已保存，待转写 |
| `transcribing` | 转写处理中 |
| `transcribed` | 转写完成，待生成草稿 |
| `generating_draft` | 草稿生成中 |
| `draft_ready` | 草稿待医生审核 |
| `confirmed` | 已确认并锁定 |
| `failed` | 最近一次提供方处理失败，可通过 `error_message` 查看原因 |

正常流程：

```text
awaiting_audio → audio_ready → transcribed → draft_ready → confirmed
```

转写或病历提供方失败时进入 `failed`；已保存的原文和已有病历保留，修复或重试后可重新执行当前步骤。实时助手有独立状态，失败不会把会话改为 `failed`。

## 5. 服务配置

语音转写通过 `TranscriptionProvider` 接入 Fun-ASR，草稿通过 `DraftGenerationProvider` 接入千问。配置见 `backend/.env.example` 和 [qwen.md](qwen.md)。提供方参数通过 Settings 校验，启动时创建服务实例。环境变量优先于 `.env`。

接入其他服务时，需同时更新提供方工厂和配置允许值，并验证音频规范、来源 ID、时间戳、超时与调用计量。

### 手动问诊内容

`POST /api/sessions/{session_id}/transcript/manual` 接收 `{"text":"第一段\n第二段"}`。音频可选，按非空行保存，最多 200 行、20000 字符；已有转写或正在处理时不可提交。手动内容不含音频时间戳。保存后通过千问生成带来源的五字段草稿，手动输入记录的 `is_mock=false`。


## 6. 后台任务、角色整理与会话助手（2026-11-04）

新增接口、幂等规则、任务状态、问答来源结构、上传 SHA-256 校验与恢复方式，见[后端开发进度与联调](后端开发进度与联调.md)。`GET /health` 额外返回 `assistant_provider`、`assistant_is_mock`、`dialogue_provider`、`job_worker_enabled`。会话详情新增 `dialogue`（未整理时为 null），音频新增 `sha256`（旧记录可能为 null）。原有同步接口返回结构保持兼容。

## 7. 增量转录与实时临床助手（v0.3）

新链路：最终识别句子 → 原文及版本落库 → 独立助手队列 → 千问分析 → 带引用的建议历史 → SSE / 轮询。问答助手仍负责查询当前会话事实，实时临床助手负责主动提出补问及诊疗思路，两者不会把推断自动写入病历。

### 7.1 输入路径

上传音频的 `transcribe` 任务会自动逐句保存 Fun-ASR 的 `sentence_end=true` 结果，不保存未确定的识别假设；无需再调用下面的外部转录接口。

其他 ASR 或采集端已有最终文本时，可使用外部输入接口。这些接口接收文本，不接收实时音频流；ESP32 的边录边传音频协议还需单独接入。两条输入路径不能在同一会话混用。

| 方法 | 路径（均以 `/api` 开头） | 行为 |
| --- | --- | --- |
| POST | `/sessions/{id}/transcript/stream` | 创建外部转录，返回 `revision=0, state=streaming`；重复调用返回已有打开的转录 |
| POST | `/sessions/{id}/transcript/stream/segments` | 原子追加一批最终句子，返回最新转录 |
| POST | `/sessions/{id}/transcript/stream/complete` | 接收 `expected_revision`，结束转录；返回 `state=final`，版本再加 1 |
| GET | `/sessions/{id}/transcript` | 读取当前已保存的原文，也可在转录未结束时读取 |

追加示例：

```json
{
  "request_id": "device-01-batch-0001",
  "expected_revision": 0,
  "segments": [
    {"seq": 0, "speaker": "患者", "text": "咳嗽三天，没有发热。", "start_ms": 300, "end_ms": 2600}
  ]
}
```

- 每批 1–50 句，`seq` 从 0 开始，连续递增；一批成功追加后转录 `revision` 加 1。音频 ASR 使用上游句号 ID，序号可能不从 0 开始或不连续，客户端不要用它作为数组下标。
- 单句最多 4000 字符；整份外部/增量 ASR 转录最多 2000 句或 100000 字符。时间戳同时提供或同时为空，结束时间大于开始时间；speaker 未知时保持 `未知`，不能把匿名声纹当作医患角色。
- 相同 `request_id` 和相同内容的重放不重复保存，即使转录已结束也可重放；返回的是最新完整转录。相同编号用于不同内容返回 `409 idempotency_conflict`。
- 新批次的版本过期返回 `409 transcript_revision_conflict`；序号不连续返回 `409 segment_sequence_conflict`；结束后新写入返回 `409 transcript_closed`。
- 空转录不能结束。成功结束的重放接受结束前或结束后的版本。转录完成前禁止整理角色或生成正式草稿。
- 外部转录及待分析队列跨重启保留，可用原批次编号重放。音频转写任务被中断时保留最终句子并标记 `interrupted`，按 3.4 节重试。

### 7.2 临床建议与控制

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/sessions/{id}/assistant/advice` | 当前转录版本、助手状态、当前/较早建议 |
| PUT | `/sessions/{id}/assistant/advice/settings` | `{"enabled":true}` 或 `false`；按会话启停 |
| POST | `/sessions/{id}/assistant/advice/refresh` | `202`，为失败或尚未分析的当前版本排队；已成功或正在分析的相同版本不重复收费 |
| GET | `/sessions/{id}/assistant/advice/history?limit=20&offset=0` | 最近分析历史，包括失败、过期和取消结果；limit 最大 100 |
| GET | `/sessions/{id}/usage` | 普通任务和实时助手的合并用量及估算费用 |

主要状态字段：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 本会话启用设置，默认 true |
| `available` / `worker_enabled` | 全局助手配置是否启用 / 后台执行器是否启用；两者均为 true 才会自动运行 |
| `transcript_revision` / `transcript_state` | 当前转录版本与完整性 |
| `pending` / `active_run_id` / `run_count` | 是否排队、当前运行 ID、已启动轮数 |
| `current` | 成功且与当前转录版本一致的分析；否则为 null |
| `previous` | 已过期、关闭或会话确认后不可作为当前建议的最近完成分析；否则为 null |
| `error` | 失败代码及提示；转录和病历不受影响 |

`current` / `previous` 和历史条目包含 `id`、`transcript_id`、`transcript_revision`、`transcript_state`、`provider`、`status`、`suggestions`、`metrics`、起止时间及错误。分析状态为 `running`、`succeeded`、`superseded`、`failed`、`interrupted` 或 `cancelled`。

建议示例（示意结构，不是模型准确性保证）：

```json
{
  "id": "<run-id>:0",
  "category": "examination",
  "title": "核实体温和呼吸情况",
  "suggestion": "可结合实测体温及呼吸系统查体，判断后续检查是否有必要。",
  "rationale": "原文提到咳嗽且否认发热，当前信息不足以确定病因。",
  "source_segment_ids": ["<segment-id>"],
  "sources": [{"id": "<segment-id>", "seq": 0, "speaker": "患者", "text": "咳嗽三天，没有发热。", "start_ms": 300, "end_ms": 2600}],
  "missing_information": ["年龄、基础病和查体结果"],
  "basis_kind": "model_reasoning",
  "requires_clinician_review": true
}
```

类别为 `follow_up`（补问）、`clarification`（核对）、`diagnostic_consideration`（鉴别诊断思路）、`examination`（检查参考）、`treatment`（治疗方向）、`red_flag`（警示线索）。返回空数组表示本轮未生成建议，不能据此判断病情安全。

每条建议必须引用本次输入中存在的原文 ID，并列出影响判断的缺失信息。服务器拒绝格式非法、跨会话或不存在的引用；这种校验不验证临床结论正确性。`medical_references_retrieved=false` 表示未接医学指南检索，原文引用支持病例线索，不是指南引用。提示词要求保留否定、主体和不确定性，不将未知信息默认正常；诊疗方向供医生评估，不自动处方、执行治疗或写入病历。

新句子到达时，旧分析只能成为 `previous`，界面必须标记“基于较早转录，待更新”，不能作为当前结论；下一轮只分析合并后的最新版本。关闭助手或病历确认后，迟到结果不能恢复成当前建议。关闭不能撤销已经发出的上游请求，该请求的用量仍记录。租约过期的分析标记中断，不自动为同一快照重复调用；可手动重试或等待新增文本。

默认合并窗口 2 秒、同会话启动间隔 10 秒、累计上限 60 轮；失败、取消和过期轮次也计数，手动刷新不绕过上限。每轮沿用千问客户端总超时/单次超时与最多一次重试；实际延迟还包含排队和模型耗时。队列持久化，与音频/病历任务互不占用同一执行线程。本机原型每进程一个助手线程，多个会话按队列依次分析。超出 150000 字符的序列化上下文会停止该轮分析并保留原文，不静默截断病史。

`/usage` 原有 token/费用总量现在包含助手调用；新增 `advisor_run_count`、`advisor_runs_by_status`、`advisor_runs_with_unknown_or_partial_cost`。失败、被替代及关闭后返回的调用也统计已报告用量，估算不等于供应商账单。

### 7.3 SSE 推送与重连

`GET /sessions/{id}/live/events` 返回 `text/event-stream`。初次连接立即发送完整快照，此后约每秒检查变化，变化时发送 `snapshot` 事件；无变化约每 15 秒发心跳注释。每次读取使用短数据库会话，不在等待模型或 SSE 连接期间持有事务。

```javascript
const feed = new EventSource(`/api/sessions/${sessionId}/live/events`);
feed.addEventListener('snapshot', event => {
  const { transcript, assistant } = JSON.parse(event.data);
  // 用整份快照替换本地内容。current 与 previous 分开展示，并标明版本。
});
// 页面离开时：feed.close();
```

重连返回最新完整快照；当前无事件日志重放，不依赖 `Last-Event-ID`。此接口推送的是已经保存的句子及整轮助手结果，不是 LLM token 流。部署反向代理时关闭 SSE 缓冲并为长连接设置合理超时。也可通过转录与建议 GET 接口轮询。

### 7.4 PowerShell 联调

后端配置真实密钥并启动后，可用虚构文本检查链路（会产生实际模型调用费用）：

```powershell
$baseUri = 'http://127.0.0.1:8000/api'
function Send-Json($path, $payload) {
    $body = [System.Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 8))
    Invoke-RestMethod -Method Post -Uri "$baseUri$path" -ContentType 'application/json; charset=utf-8' -Body $body
}
$session = Send-Json '/sessions' @{patient_name='虚构联调病例'}
$sid = $session.id
Invoke-RestMethod -Method Post -Uri "$baseUri/sessions/$sid/transcript/stream"
Send-Json "/sessions/$sid/transcript/stream/segments" @{
    request_id='batch-001'; expected_revision=0
    segments=@(@{seq=0; speaker='患者'; text='咳嗽三天，没有发热。'})
}
Invoke-RestMethod "$baseUri/sessions/$sid/assistant/advice" | ConvertTo-Json -Depth 12
curl.exe -N "$baseUri/sessions/$sid/live/events"
```

观察 `pending → active_run_id → current`；模型返回前 `current` 为 null。按 Ctrl+C 停止订阅后，可继续追加下一批并使用最新 revision，或完成转录：

```powershell
$transcript = Invoke-RestMethod "$baseUri/sessions/$sid/transcript"
Send-Json "/sessions/$sid/transcript/stream/complete" @{expected_revision=$transcript.revision}
Invoke-RestMethod "$baseUri/sessions/$sid/usage" | ConvertTo-Json -Depth 8
```

`/health` 用于检查 `clinical_advisor` 和 `live_assistant_worker_enabled`，不代表已验证上游模型权限。自动化测试使用临时数据库、供应商协议响应替身与虚构资料，不调用真实服务，也不能证明真实模型的医疗准确率、延迟或账单金额。

<a id="follow-up-api"></a>

## 8. 患者档案、复诊回顾与持续事项（v0.4）

路径前缀为 `/api`。运行后从 `/openapi.json` 获取类型定义，从 `/docs` 调用接口。所有 ID 作为不透明字符串传递，日期时间为 UTC。现有按姓名建会话的调用继续支持，返回新增的 `patient_id` 可为空。

| 请求 | 请求或参数 | 成功结果 |
| --- | --- | --- |
| POST /patients | patient_name 必填，patient_ref 可选；非空外部编号唯一，重名允许 | 201，患者 id、patient_name、patient_ref |
| GET /patients | query 匹配姓名、外部编号或完整 id，limit 默认 30，上限 100，offset 默认 0 | 200，items 数组 |
| POST /sessions | 选定档案时传 patient_id；同时传姓名或编号时必须与所选档案一致 | 201，会话详情 |
| PUT /sessions/{id}/patient | patient_id。医生核对后绑定未关联的旧会话；允许转录前或病历已确认后绑定 | 200，会话详情。同一绑定可重放。原有病历和确认快照保留原始称呼 |
| GET /sessions/{id}/follow-up-summary | 无请求体 | 200，session_id、patient_id、previous_record、comparisons、pending_items |
| GET /sessions/{id}/items | 无请求体 | 200，items 数组。含本次可见的自动事项、医生建立或已处理的事项及从已确认就诊带入的随访 |
| POST /sessions/{id}/items | request_id、title、actor 必填，carry_forward 默认 false | 201，事项及 actions；同一会话相同请求重放返回同一事项最新状态 |
| PATCH /sessions/{id}/items/{item_id} | expected_revision、actor、status 必填，result 和 carry_forward 可选 | 200，事项及完整操作轨迹；遗漏 result 时保留旧值，提交空字符串可清空 |
| GET /sessions/{id}/live/snapshot | 无请求体 | 200，transcript、assistant 和 items，结构与 SSE snapshot 事件相同 |

**复诊的时间范围。** previous_record 是同一患者在本次会话创建前已确认的最近一次就诊，包含 session_id、record_id、version、visit_started_at、confirmed_at 和带原文来源的五字段。草稿及其他患者记录不参与。comparisons 逐字段对照历史和已保存的本次草稿。awaiting_draft 表示等待本次草稿，not_mentioned 表示本次未提及，same_text 和 different_text 只比较文本；病情好转或恶化须由医生判断。编辑或确认本次草稿后重新读取摘要。

**事项操作。** status 接受 open（待核实）、asked（已问待答）、resolved（已解决）、dismissed（已忽略）。carry_forward 是独立布尔值，已结或忽略时自动关闭。可以用 result 记录“患者记不清”或“已核对报告”。动作保留 actor、session_id、revision、时间与处理结果。本机原型由客户端填写 actor。确认病历后不能在该次会话中修改事项，仍待随访的事项从下一次就诊处理。

**当前与历史依据。** 本次 suggestions[].sources 引用当前转录；history_sources 指向历史 session_id、record_id、version、field_key、value 和 confirmed_at。历史五字段及其原句可从 previous_record.fields 或旧会话的 /record 读取。只有补问与核对生成可处理 item_id，旧数据和其他类别可为 null。前端按 item_id 处理和去重；单轮建议 id 仅用于定位历史。医生处理后 /assistant/advice 的 current 和新的 SSE 快照都会过滤该条主动提示，/assistant/advice/history 保留分析时的结果。

**业务错误。** 422 validation_error 表示输入无效。404 patient_not_found 或 item_not_found 表示当前上下文无此对象。409 patient_ref_conflict、patient_identity_conflict、patient_already_linked、session_in_progress 分别提示核对重复编号、身份不匹配、已绑定或绑定时机。409 patient_link_required 表示跨次随访需要先关联档案。相同 request_id 提交不同内容返回 409 idempotency_conflict。版本过期返回 409 item_revision_conflict，重新读取事项及 actions 后再决定是否修改；已确认会话写操作返回 409 session_confirmed。

### 四组前端联调样例

建档和新建会话可分别携带 `Idempotency-Key`。同一操作使用相同键和资料重试会返回原对象。相同键配不同资料返回 409。在本机单用户模式下，事项处理人显示“本机医生”。

以下使用同一位虚构患者。请求中的 P、A、B、I 均替换为前一步实际返回的 ID。

1. **初诊。** POST /patients 提交 `{"patient_name":"虚构患者甲","patient_ref":"TEST-001"}` 得到 P。POST /sessions 提交 `{"patient_id":"P"}` 得到 A。读取 A 的 follow-up-summary，预期 previous_record 为 null，comparisons、pending_items 均为空数组。在 A 的 items 提交 `{"request_id":"first-follow-1","title":"下次核对复查结果","actor":"医生甲","carry_forward":true}` 得到 I。按第 3 节完成录入、生成及确认。
2. **复诊。** 使用 P 再创建会话 B。读取 B 的 follow-up-summary，可见 A 的确认版本及 I。在 B 的 items/I 提交 PATCH `{"expected_revision":1,"actor":"医生乙","status":"resolved","result":"已核对复查结果"}`。再次查询，I 从 pending_items 移出，B 的 items 仍保留原始会话及两次操作。
3. **历史与本次说法冲突。** 测试资料中 A 的用药史记有曾用某药，B 按第 7 节追加“患者：我已停用之前的药”。核对类建议若引用该历史字段，sources 指向 B 的原句，history_sources 指向 A 的 medication_history。医生处理建议时使用 item_id 和事项实际 revision。过期 PATCH 返回 409，旧病历原文和本次草稿不会被该动作改写。模型是否识别差异需单独评测。
4. **断线重连。** 前端订阅 B 的 live/events 并监听 snapshot。断开连接期间从另一客户端标记事项，重新订阅后用首个快照整体替换本地 transcript、assistant 和 items。GET live/snapshot 可用于无 SSE 的联调，返回相同数据结构，无需恢复旧事件游标。
