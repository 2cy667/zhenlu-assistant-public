# 分段实时音频接口对接（前端适配 v1）

问诊辅助提醒的可选扩展见 [guidance-api.md](guidance-api.md)：新增 `realtime.guidance` 及提醒状态 PATCH 契约。工作台现为宽幅转写 + 病历侧栏，来源按需展开。

**实现状态：本次提交实现 React 前端与接口适配，不包含下述新增后端路由、端侧采集或云端识别调度。** 当前 FastAPI 仍提供一次性音频上传接口。只有后端按本文实现后，实时音频才能实际使用；未接通时前端显示服务不可用，不生成模拟文字、不退回整段上传。既有文字问诊、历史整段音频记录继续使用原接口。

## 1. 页面与职责

原首页“音频问诊” → 新建问诊“实时音频” → 原审核工作台。没有新应用、第二套登录或独立记录库。原模板、病历记录、确认和导出复用。

| 参与方 | 职责 |
| --- | --- |
| 网页 | 创建并关联会话、展示处理状态、读取转写和草稿、来源回听、医生编辑与审核 |
| 采集端 | 连续录音、分段上传、片段编号、丢包重传、最后片段及结束标记 |
| 后端 | 绑定设备与会话、持久化片段、云端识别、跨段对齐去重、字段整理、版本控制与权限检查 |

每 10 秒一段仅为举例，不是固定协议。文字显示延迟约为分段等待 + 上传 + 识别 + 网页同步延迟。此方案属于分段近实时转写，不承诺逐字无延迟。

**本版网页使用每次请求结束后等待 2 秒的 HTTP 快照轮询。** 无重叠轮询，请求超时 10 秒。连接恢复后直接取完整快照，避免漏掉中断期间的句子。没有同时接入 SSE / WebSocket；后续可以在同一版本语义下增加推送，不能把本实现描述为已经支持 SSE。网页只在打开当前审核工作台时同步，离开页面不代表设备停止采集。

## 2. 网页使用的新增路由（后端待实现）

Base URL 沿用 `/api`，同源 Cookie 鉴权，错误格式沿用 `docs/api.md`。所有响应禁止缓存，所有读写、音频路由均校验当前用户及机构归属。生产中的采集设备关联由服务端管理，不能通过前端模式开关授权。

| 请求 | 请求体 / 请求头 | 成功结果 |
| --- | --- | --- |
| `POST /sessions/realtime` | `{patient_name, patient_ref}`；`Idempotency-Key` | 201，完整 `SessionDetail`，含 `realtime` |
| `GET /sessions/{id}/realtime` | 无；`Cache-Control: no-store` 响应 | 200，完整、原子一致的最新 `SessionDetail` |
| `POST /sessions/{id}/realtime/start` | `{expected_version}`；`Idempotency-Key` | 200 或 202，完整 `SessionDetail` |
| `POST /sessions/{id}/realtime/stop` | `{expected_version}`；`Idempotency-Key` | 200 或 202，完整 `SessionDetail` |
| `POST /sessions/{id}/realtime/retry` | `{expected_version, chunk_id}`；`Idempotency-Key` | 200 或 202，完整 `SessionDetail` |

创建应原子完成“创建会话 + 确定实时模式 + 绑定可用设备”。无可用设备返回 409 `capture_device_unavailable`；不支持实时服务返回 404 或 503 `realtime_unavailable`，不能留下普通空会话。必须保证文字与实时会话不会串用。`GET /sessions/{id}` 及既有保存、重新生成、确认响应也需包含此会话的 `realtime`，便于刷新和重新打开。

幂等键在当前用户及操作范围内生效。相同键 + 相同请求体返回同一资源或最新结果；相同键 + 不同请求体返回 409 `idempotency_conflict`。网页创建失败且结果未知时保留患者信息与原键重试；不要因网络超时生成重复会话。start / stop / retry 自身还应按服务端状态幂等，重复停止不能新建处理任务。

`expected_version` 不匹配返回 409 `realtime_version_conflict`；网页保留内容，下一轮快照恢复真实状态。命令接口不可阻塞到整段识别完成，15 秒内接受或拒绝；命令发送成功不等于设备已执行。

### 设备控制模式

- `control_mode: "web"`：显示开始/结束按钮。`start` 先变成 `starting`，设备确认后变成 `recording`；`stop` 先变成 `stopping`，收到设备结束事件才进入 `ended`。
- `control_mode: "device"`：网页提示使用设备上的录音按钮，不发送 start/stop。收到端侧事件后自动显示对应状态。
- 设备断连与网页网络断连是两件事。前者由 `capture: "disconnected"` 表示，后者显示网页同步错误并保留已展示内容。关闭网页不能停止设备录音。

## 3. 扩展 SessionDetail

保留原 `status`、`audio`、`transcript`、`draft`、`latest_record` 等字段。旧会话可省略 `realtime` 或为 null；实时会话必须携带：

```json
{
  "realtime": {
    "version": 12,
    "capture": "recording",
    "control_mode": "web",
    "end_received": false,
    "last_chunk_seq": null,
    "transcript_revision": 3,
    "draft_transcript_revision": 3,
    "final_draft_ready": false,
    "chunks": [
      {
        "id": "chunk-001",
        "seq": 1,
        "offset_ms": 0,
        "duration_ms": 10000,
        "status": "completed",
        "audio_url": "/api/sessions/session-id/chunks/chunk-001/audio",
        "error_message": null,
        "retryable": false
      }
    ]
  }
}
```

上例仅是结构示意，`session-id` 不是可直接调用的真实会话。

| 字段 | 约定 |
| --- | --- |
| `version` | 会话完整快照版本，非负整数；片段、原文、草稿、保存、确认、采集状态有任何变化都递增；不能在进程重启后归零 |
| `capture` | `idle / starting / recording / stopping / ended / disconnected` |
| `control_mode` | `web / device`，不能依据浏览器是否在线推断设备已结束 |
| `last_chunk_seq` | 未结束时 null，设备结束时给出正整数；片段序号从 1 连续递增 |
| `end_received` | 后端是否收到设备结束标记；网页发了 stop 不能设为 true |
| `transcript_revision` | 当前累计原文修订；新增、修正、删除均递增 |
| `draft_transcript_revision` | 当前草稿基于哪一版累计原文；尚无草稿时 null |
| `final_draft_ready` | 所有音频已处理且最终草稿已持久化；新原文到达时立即置 false |
| `chunks[].status` | `receiving / transcribing / completed / failed` |
| `audio_url` | 同源、带鉴权、支持 Range 的可播放文件地址；未就绪时 null |
| `retryable` | 失败时是否允许网页触发重试；未提供重试能力时 false |

前端拒绝比当前 `version` 更旧的响应（包括慢轮询晚于保存响应返回），防止病历倒退。后端必须以同一事务/一致快照提供原文、来源和草稿，禁止同版本出现不同内容。

## 4. 转写、字段来源与跨段回听

`TranscriptSegment` 在既有 `id, seq, speaker, text, start_ms, end_ms` 上增加：

```json
{
  "id": "sentence-002",
  "seq": 2,
  "speaker": "患者",
  "text": "一条跨越两个片段的问诊原文",
  "revision": 2,
  "is_final": true,
  "start_ms": 9500,
  "end_ms": 10500,
  "source_ranges": [
    {"chunk_id": "chunk-001", "start_ms": 9500, "end_ms": 10000},
    {"chunk_id": "chunk-002", "start_ms": 0, "end_ms": 500}
  ]
}
```

- 外层 `start_ms/end_ms` 是整场问诊时间；`source_ranges` 是各文件内的时间，按播放顺序排列。上例回听第一段最后 0.5 秒，再回听第二段前 0.5 秒。
- 每个来源必须引用同一会话、已可播放的片段。边界不得超出文件实际时长；重叠采集须由后端消除重复来源，不能重复播放重叠内容。
- ID 稳定，修订递增；前端同 ID 只展示最高 revision，按 seq 排列。原文更正/删除必须同步更新所有字段及建议中的 `sources`，并增加草稿 revision，撤销旧审核状态。
- `is_final=false` 为临时识别结果，不能作为可确认病历依据或开放回听；结束前所有有效句子必须最终定稿。
- 说话人无法可靠区分时使用空字符串，前端显示“未区分说话人”。不能固定交替猜测医生/患者。
- 无可靠时间戳时外层时间为 null、`source_ranges=[]`，保留原文核对但禁用区间回听，不伪造 00:00。
- 字段 `sources` 与 `suggestion.sources` 使用同一扩展句子结构。网页从现有“查看来源”和“回听来源”按钮播放片段，无需等待整段音频拼接。`audio` 可保持 null。

## 5. 草稿更新、医生修改与确认

语音识别与五字段整理是独立任务。后端结合累计的最终原文增量生成草稿，保持原五字段 key；未提及的信息不得推断为“无”。避免每次轮询生成一次草稿，轮询只是读取。

- 保存继续使用 `PATCH /sessions/{id}/draft` 与 `expected_revision`。AI 更新和医生保存必须使用同一版本锁；模型不得覆盖 `origin=doctor`，只能提出 `suggestion`。
- 前端接收新草稿时保留正在输入的字段，刷新未修改字段；任何草稿修订会撤销逐项审核。旧建议的采纳标记失效，医生可重新比较。409 不清空输入、不自动重发旧修改。
- 现有“保留医生内容”仍是当前页面的建议忽略状态，后端没有独立驳回接口；不承诺跨刷新记住这一决定。
- 确认继续使用 `POST /sessions/{id}/confirm`，原 `expected_revision` 不变。**后端必须再次原子校验**：采集 ended、收到结束标记、1..last_chunk_seq 无缺段、所有片段 completed、无临时转写、final_draft_ready=true、draft_transcript_revision=transcript_revision。网页断线或过期快照期间禁用确认。
- 不满足时返回 409 `realtime_not_final`。更新与确认不能竞态成功；确认成功后锁定草稿及来源、记录快照，后续音频/模型结果不得修改该确认版本。

## 6. 采集端 → 后端建议（本提交未实现）

端侧上传协议由采集同学实现；建议 `POST /sessions/{id}/chunks` multipart 包含 `file, chunk_id, seq, offset_ms, duration_ms, sha256`，用稳定片段 ID + 校验和幂等重传。设备凭据由服务端校验，网页不持有设备密钥、不转发音频文件。

设备停止后上传剩余尾段，并发送结束标记 `{last_chunk_seq}`。结束标记可能先于尾段到达，此时后端等待补齐，不得直接置 final_draft_ready。同 seq 不同校验和应拒绝；同 seq 同校验和返回已有状态。失败重试不能重复句子或重复模型任务。需对录音时长、文件大小、音频格式、设备绑定、保留时间和并发问诊数量设服务端约束。

## 7. 联调清单

1. 未实现接口返回明确错误；创建结果未知时使用同键重试，不生成第二会话。
2. 设备开始应答前为 starting；设备本地开始录音也能更新网页。
3. 乱序片段、重复句子、旧快照、网络断连恢复，不丢原文、不回退版本。
4. 模型更新期间医生仍可输入；保存冲突保留文字，保存成功出现在确认导出中。
5. 跨段句子依次回听，播放到来源末尾暂停；无时间戳禁用回听。
6. stop 成功但尾段未到、缺段、失败、临时结果、草稿未追上原文时，都不能确认。
7. 登录过期不继续取患者数据或播放音频；原账号恢复后保留未保存输入。
8. 刷新 / 切换模块后通过 GET 会话详情和快照恢复，继续用原病历记录及模板导出。

本地验证使用受控接口响应；真实设备、ASR 和最终草稿调度仍需团队联调。提交中不包含演示账号、合成音频、定时模拟数据或独立 HTML 测试页面。
