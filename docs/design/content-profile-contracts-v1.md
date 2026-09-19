# ProfileBundle 与 StructuredNoteDocument 最小契约 V1

日期：2026-08-28
状态：实现前契约候选；本文件不改变当前运行行为

## 1. 契约目标

本契约只解决两个问题：

1. 内容策略怎样作为受控、版本化 bundle 被 Worker 加载；
2. 模型输出怎样先形成稳定的证据文档，再由不同 renderer 生成 Note。

第一版优先保证可校验、可固定、可回退和可比较，不追求用户在线编辑任意提示词。

## 2. `ProfileBundle` 最小契约

建议 bundle 根清单使用 JSON。提示词正文继续放独立 UTF-8 Markdown 文件，renderer 使用 JSON。
JSON 便于严格拒绝未知字段、稳定 canonical hash 和避免 YAML 隐式类型。

### 2.1 清单草案

```json
{
  "bundle_schema_version": "1.0.0",
  "profile_id": "speech-capture/meeting",
  "profile_version": "2026-08-27.1",
  "content_type": "meeting",
  "document_schema": {
    "id": "speech-capture/structured-note",
    "version": "1.0.0"
  },
  "engine_compatibility": {
    "minimum": "0.1.0a0",
    "maximum_exclusive": "0.2.0"
  },
  "prompts": {
    "extraction": "extract.prompt.md",
    "synthesis": "synthesize.prompt.md",
    "coverage_repair": null,
    "quality_edit": "quality.prompt.md",
    "named_repairs": {
      "meeting_outcomes": "meeting-outcomes.prompt.md"
    }
  },
  "document_policy": "document-policy.json",
  "execution_policy": "execution-policy.json",
  "validation_policy": "validation-policy.json",
  "renderer": "renderer.json",
  "fixtures_manifest": "fixtures/manifest.json",
  "fallback_profile": {
    "profile_id": "speech-capture/meeting",
    "profile_version": "builtin-2026-08-27.1"
  },
  "files": {
    "extract.prompt.md": "sha256:...",
    "synthesize.prompt.md": "sha256:...",
    "quality.prompt.md": "sha256:...",
    "meeting-outcomes.prompt.md": "sha256:...",
    "document-policy.json": "sha256:...",
    "execution-policy.json": "sha256:...",
    "validation-policy.json": "sha256:...",
    "renderer.json": "sha256:...",
    "fixtures/manifest.json": "sha256:..."
  },
  "bundle_sha256": "sha256:..."
}
```

### 2.2 强制规则

- `profile_id + profile_version` 在一个 Worker 包中唯一；同版本不同哈希直接拒绝；
- 所有相对路径必须解析在 bundle 根目录内，禁止 `..`、绝对路径和符号链接逃逸；
- 只允许清单声明的文件；缺文件、额外关键文件、哈希错误或未知字段均不激活；
- `bundle_sha256` 是去掉自身字段后，对规范化清单和全部文件哈希计算的总哈希；
- 提示词仅是数据，不允许模板执行任意表达式。第一版只支持由引擎注入的固定占位符；
- profile 只能引用引擎注册的 prompt slot、validator、model role 和 renderer primitive；
- Worker 启动时静态校验，任务开始时把完整 profile reference 固定进检查点；
- 已开始任务恢复时必须找到同哈希 bundle；找不到则安全暂停，不能静默换最新版；
- 新 profile 只能生成候选 Note，不自动采用或发布。

### 2.3 `document-policy.json`

它声明如何使用**已有语义字段**，不定义新 schema：

```json
{
  "required_nonempty": ["title", "objective", "summary", "timeline_sections"],
  "allowed_empty": ["decisions", "actions", "risks", "open_questions"],
  "body_source": "topics",
  "field_limits": {
    "highlights": 8,
    "topics": 10,
    "speaker_summaries": 16
  }
}
```

引擎硬上限优先；profile 只能设置不超过硬上限的更小值。

### 2.4 `execution-policy.json`

```json
{
  "roles": {
    "classification": "editor",
    "extraction": "editor",
    "synthesis": "primary",
    "quality_edit": "editor"
  },
  "batch_target_tokens": 4800,
  "maximum_quality_passes": 1,
  "enabled_registered_repairs": ["meeting_outcomes"]
}
```

模型的实际名称、网络地址、超时、重试、资源预检和最大 context 仍由 Worker 配置及硬限制控制。

### 2.5 `validation-policy.json`

```json
{
  "registered_validators": [
    "meeting.context.sufficient",
    "meeting.decision.confirmed",
    "meeting.action.evidence_complete",
    "meeting.categories.nonduplicated"
  ],
  "thresholds": {
    "minimum_context_facets": 2,
    "single_context_minimum_characters": 80
  }
}
```

通用证据、字段、权限和恢复 validator 永远执行，不出现在这个可选列表中。未知 validator 直接拒绝
bundle，不能忽略。

### 2.6 `renderer.json`

```json
{
  "renderer_version": "1.0.0",
  "document_schema_version": "1.0.0",
  "sections": [
    {"field": "objective", "heading": "会议目标", "when": "nonempty"},
    {"field": "summary", "heading": "内容总结", "when": "always"},
    {"field": "context", "heading": "背景与参与方", "when": "nonempty"},
    {"field": "highlights", "heading": "核心结论", "when": "nonempty"},
    {"field": "topics", "heading": "主要讨论与结论", "when": "nonempty"},
    {"field": "speaker_summaries", "heading": "参与者与各方观点", "when": "nonempty"},
    {"field": "decisions", "heading": "会议决议", "when": "nonempty"},
    {"field": "actions", "heading": "待办事项", "when": "nonempty"},
    {"field": "risks", "heading": "风险与待确认项", "when": "nonempty"},
    {"field": "open_questions", "heading": "仍待确认的问题", "when": "nonempty"}
  ],
  "timeline_output": "separate_markdown",
  "evidence_output": "separate_markdown"
}
```

第一版 `when` 只允许 `always` 或 `nonempty`；不支持任意表达式。证据链接、frontmatter、人工补充保护和
输出路径不由 renderer 配置控制。

## 3. `StructuredNoteDocument` 最小契约

当前运行 schema 为 1.6.0 检查点格式。新文档契约先作为稳定 envelope 引入，不立即删除当前字段。

### 3.1 Envelope 草案

```json
{
  "document_schema_version": "1.0.0",
  "document_id": "note-document:<job-id>:<revision-key>",
  "content_type": "meeting",
  "profile": {
    "profile_id": "speech-capture/meeting",
    "profile_version": "2026-08-27.1",
    "bundle_sha256": "sha256:..."
  },
  "source": {
    "evidence_bundle_sha256": "sha256:...",
    "corrected_transcript_sha256": "sha256:...",
    "recording_context_sha256": "sha256:..."
  },
  "content": {
    "title": "...",
    "objective": {"text": "...", "evidence": ["seg_..."]},
    "summary": {"text": "...", "evidence": ["seg_..."]},
    "context": [],
    "highlights": [],
    "topics": [],
    "scene_sections": [],
    "discussion_threads": [],
    "timeline_sections": [],
    "speaker_summaries": [],
    "decisions": [],
    "actions": [],
    "risks": [],
    "open_questions": []
  },
  "quality": {
    "validator_set_version": "1.0.0",
    "validated_at": "...",
    "warnings": []
  }
}
```

`document_id` 的实际值是内部稳定 ID；上例仅说明形状，不规定把私有 job ID 写入公开 Note。

### 3.2 公共字段语义

| 字段 | 语义 | 证据要求 |
| --- | --- | --- |
| `title` | 具体记录标题 | 可由整篇证据支持，不单独强制数组 |
| `objective` | 为什么发生、要解决什么；会议必需，其他类型可空 | 至少 1 个直接片段 |
| `summary` | 连贯总览，不是结论清单 | 至少 1 个直接片段 |
| `context` | 人物、组织、关系、背景、约束 | 每项 1–3 个片段 |
| `highlights` | 真正影响记录目标的信息 | 每项 1–3 个片段 |
| `topics` | 通用主题索引；会议正文来源 | 每项与 detail 均有证据 |
| `scene_sections` | 非会议场景专用正文 | 每项证据必需 |
| `discussion_threads` | 初始主张、修正、当前方向与状态 | 每一阶段均有证据 |
| `timeline_sections` | 连续时间顺序摘要 | 使用首尾 segment ID，必须覆盖完整范围 |
| `speaker_summaries` | 实质发言者主张、承诺或顾虑 | 优先引用该 speaker 自己的发言 |
| `decisions` | 已明确确认的结论 | 必须有确认性证据 |
| `actions` | 可交付、可验收的未来动作 | task 必须有证据；owner/deadline 无证据则空 |
| `risks` | 会影响结果的实际风险或限制 | 直接证据 |
| `open_questions` | 通读后仍未解决的问题 | 问题与未解决状态均需证据 |

### 3.3 人工内容不进入模型文档

以下内容保持独立 control-plane 数据，不放进 `content`：

- “我的补充”；
- 人工编辑的候选 Markdown 草稿；
- 当前采用版本、拒绝记录和发布时间；
- Vault 路径、发布租约和冲突位置。

这样重新生成模型文档不会覆盖人工内容，切换 profile 也不会把发布状态误当语义字段。

### 3.4 版本规则

- 增加可选字段：文档 schema minor 版本；
- 删除/重命名字段或改变证据语义：major 版本；
- 只修正文档描述、不改变校验：patch 版本；
- profile 可声明一个精确文档 schema 版本；第一版不接受宽泛 `>=`；
- renderer 必须声明支持的精确文档 schema；不匹配时不渲染、不发布；
- 当前 1.6.0 payload 通过显式 adapter 转为 envelope，禁止在各处临时猜字段。

## 4. 任务固定与 provenance

任务第一次进入内容提炼前写入：

```json
{
  "profile_id": "speech-capture/meeting",
  "profile_version": "2026-08-27.1",
  "profile_sha256": "sha256:...",
  "document_schema_version": "1.0.0",
  "validator_set_version": "1.0.0",
  "renderer_version": "1.0.0"
}
```

这些字段必须同时进入：

- structuring 原始私有证据；
- 可恢复 structuring checkpoint；
- Note candidate revision；
- artifact provenance；
- 缓存指纹。

同一候选从生成、人工编辑、采用到发布不得更换 profile reference。

## 5. 缓存键规则

当前缓存已包含 cache schema、模型、prompt version、内容类型、输入哈希和部分 repair version。迁移后每个
内容阶段的指纹至少包含：

```text
cache_schema_version
stage_kind
profile_id
profile_version
profile_sha256
document_schema_version
validator_set_version
renderer_version（仅渲染缓存）
model_role + resolved_model_id
execution_policy_sha256
input evidence/transcript/context/corrections hashes
batching algorithm version + effective budgets
```

只改 renderer 不应使提取或综合缓存失效；只改 meeting profile 不应使 interview 等其他类型缓存失效。

## 6. 双读与等价迁移计划

### 6.1 阶段 B0：基线冻结

- 固定当前内置 meeting profile 的 canonical 文本与版本；
- 固定当前公开合成夹具的结构化 JSON、纯净 Note、证据 Note 和时间线；
- 记录模型调用阶段、缓存命中、恢复检查点和候选 provenance；
- 不运行或提交私有音频/逐字稿/Note。

### 6.2 阶段 B1：只实现 loader 和 adapter

- 加入严格 bundle loader、canonical hash 和 last-known-good registry；
- 加入 current-payload → `StructuredNoteDocument` adapter；
- 默认仍由内置 Python 路径生成，外部 profile 只做加载和静态校验；
- API、数据库和插件可见行为不变。

### 6.3 阶段 B2：影子双读

同一份合成 `EvidenceBundle` 同时执行：

1. A 路径：当前内置 meeting 策略；
2. B 路径：外部 meeting ProfileBundle；
3. 两条结果都通过同一 invariant validator；
4. B 结果只写测试临时目录，不写正式 checkpoint，不生成待采用版本，不发布。

模型输出具有非确定性，因此不能要求 Markdown 字节相等。等价门分三层：

| 层级 | 验收方式 |
| --- | --- |
| 契约等价 | 字段集合、类型、证据 ID、空字段行为、timeline 覆盖完全一致 |
| 语义等价 | 决议、待办、数字、人名/组织、未决项的规范化事实集合一致；允许措辞不同 |
| 产物等价 | 栏目顺序、证据链接目标、人工补充保护和发布输入一致 |

### 6.4 阶段 B3：meeting 受控切换

- 仅当自动等价门通过，再对一个经授权私有样例做必要的下游影子提炼；
- 不重跑 ASR、对齐或说话人识别；
- 私有输入输出不进 Git，不写公开 fixtures；
- 用户只审阅候选，不自动采用、不自动发布；
- 通过后将 meeting 的默认 resolver 原子指向外部 profile；其他类型保持内置。

### 6.5 阶段 B4：回退证明

必须自动验证：

- bundle 缺失、哈希错误、未知 validator、schema 不兼容时拒绝激活；
- 新任务使用 last-known-good；已固定任务若精确 bundle 缺失则安全暂停；
- 回退不会修改当前 Note、原始 ASR、逐字稿、人工草稿或发布路径；
- 恢复后仍使用原 profile 哈希，不能漂移到最新版。

## 7. 自动测试矩阵

| 测试组 | 必测项 |
| --- | --- |
| Loader | 未知字段、路径逃逸、重复 ID、同版本异哈希、缺文件、哈希错误 |
| Compatibility | Worker/document/renderer 版本不兼容、未知 prompt slot/validator/model role |
| Pinning | 新任务固定、重启恢复、候选生成、人工草稿、采用与发布 provenance 一致 |
| Cache | profile 改动精准失效、renderer-only 改动不重跑模型、其他类型不受影响 |
| Invariants | 伪造证据、未知 segment、猜负责人/日期、背景当事实、timeline 跳段被拒绝 |
| Renderer | 纯净 Note、证据 Note、timeline、空栏目、标题顺序、人工补充保护 |
| Revision | profile 更新只产生候选；接受/拒绝/人工编辑/重新发布维持现有边界 |
| Fallback | bundle 损坏、加载中断、last-known-good、固定版本缺失安全暂停 |

## 8. 第一轮通过标准

只有同时满足以下条件，才可以把 meeting 默认路径切到外部 profile：

1. 当前 Worker 全量自动测试继续通过；
2. 合成 meeting fixtures 的契约、语义和产物等价门通过；
3. interview、course、speech、voice_memo、generic 仍走原路径且回归通过；
4. 缓存、断点恢复、候选版本、人工编辑、发布和删除链路无行为变化；
5. 一个经授权私有样例不低于当前会议质量基线；
6. 能现场证明损坏 bundle 自动回退且现有任务不漂移；
7. 项目所有者再次确认后才激活，不以测试通过代替产品确认。

## 9. 当前实施状态与下一道门（2026-08-28）

阶段 B1 与 B2 均已完成且保持非激活状态：

- B1 已落地严格 loader、canonical hash、last-known-good registry、当前 payload adapter 和公开合成基线；
- B2 已落地测试专用的 meeting 影子双读执行器。同一份合成 `EvidenceBundle` 同时交给 A/B 两路，两路
  共用当前 invariant validator，并以稳定 envelope 进行契约、语义和产物计划三层比较；
- B2 只允许把外部 B envelope 和等价报告写入操作系统临时目录；内置 A 结果不落盘，非临时目录会在
  runner 执行前被拒绝；
- 自动测试同时证明该模块不导入 JobStore、checkpoint、summary revision、artifact generation、publication
  或 API，不会产生正式 checkpoint、候选版本、发布回执或 current state；
- 实现仍未接入任务 resolver、模型调用、缓存、API、数据库、插件或发布路径，当前 Note 与既有运行行为
  不变。

本轮当前 Worker 测试目录共收集并通过 547 项，其中 B1/B2 专项 15 项；静态检查和补丁格式检查通过。

下一道门为 B3 meeting 受控切换准备。必须再次取得项目所有者明确确认，才可以用一个经授权私有样例只做
必要的下游影子提炼；不得重跑 ASR、对齐或说话人识别，不得自动采用或发布候选，也不得提前迁移其他内容
类型。

## 10. B3 契约验证状态（2026-08-28）

- `ProfileReference` 已在受控 resolver 中固定 `profile_id`、`profile_version` 与 canonical hash；
- meeting 外部 profile 只接受显式注入，未注入时输出路径和现有 builtin 完全相同；
- 已固定任务只能恢复精确 bundle；缺失、哈希不一致或契约不兼容均安全暂停，不能自动升级；
- 四个 prompt slot 仅是惰性文本输入，不得携带可执行代码或绕过 invariant validator；
- 合成与授权私有样例均通过 evidence 引用校验，私有影子没有写正式 candidate/revision/publication；
- 当前仍缺发布 wheel 的 bundle 携带验证，以及项目所有者的语义质量确认。

在最后两项完成前，meeting 默认 profile 必须保持 builtin，其他内容类型也不得迁移。
