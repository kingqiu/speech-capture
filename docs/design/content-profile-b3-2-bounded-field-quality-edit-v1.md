# B3.2 有界字段级质量编辑设计 V1

日期：2026-08-29
状态：设计、公开/私有 shadow 验收和默认 Profile 切换已完成；`.2` 已于 2026-08-30 部署

## 1. 结论先行

B3.2 不再让质量编辑模型读取并重写整篇长会议文档。经过严格校验的基线文档保持不可变；Worker 先用
确定性规则完成可安全修复的工作，只有仍然失败的特定字段才生成最小证据包并进行一次短模型调用。所有
局部结果必须通过字段级校验、基线哈希前置条件和最终整篇一致性门，才可以形成 shadow 候选。

这一设计解决的是执行粒度，不只是提示词措辞。Profile 仍负责内容策略，但不能自行提高调用次数、证据
上限、超时或绕过安全校验。B3.1、builtin 默认路径、当前 Note、候选采用和 Vault 发布在本设计评审及
后续验证通过前全部保持不变。

## 2. 问题与根因

B3.1 对完整长会议执行整篇质量编辑时，单次调用持续很久；语义失败又曾触发第二次整篇调用，累计
超过 20 分钟后仍未产生可验收候选。虽然现已禁止语义失败重试，但继续提高超时不能解决以下根因：

- 一个局部缺失会让模型重新阅读并改写全部字段；
- 已经通过验证的行动、风险、未决项和说话人摘要也暴露在无必要的改写风险中；
- 失败时只能丢弃整篇结果，无法精确复用已验证字段；
- 用户看到的是一个长时间全局阶段，不能判断正在修复什么；
- Profile 提示词与执行粒度耦合，内容规则小改仍可能触发昂贵的全篇调用。

因此 B3.2 的目标不是让模型“更努力地重写”，而是把质量编辑变为可规划、可限制、可观察、可回退的
字段级修复。

## 3. 设计原则

1. **已验证基线不可变**：局部编辑从经过现有结构、证据和场景 validator 的文档开始；未触及字段保持
   字节级不变。
2. **确定性修复优先**：已有证据结果恢复、摘要与最终结果一致、无证据角色清空等规则继续在本地执行，
   不调用模型。
3. **只修失败字段**：语义门产生稳定失败码，修复规划器把失败码映射到已注册 repair；禁止自由决定重写
   整篇文档。
4. **最小证据输入**：模型只接收目标字段、直接关联证据和必要的相邻确认片段，不接收完整逐字稿。
5. **局部输出契约**：每次调用只允许返回目标字段的严格 JSON，不允许返回完整 Note 或修改其他字段。
6. **无语义重试**：语义失败直接安全停止。只有截断或 JSON 无法解析时允许一次短重试，且仍受同一总预算
   限制。
7. **最终全局守门**：局部通过不代表整篇通过；合并后仍执行当前全部通用、meeting 和发布输入 validator。
8. **失败保留基线**：超时、证据包过大、哈希变化或最终校验失败均不产生部分候选，不改变当前 Note。

## 4. 建议执行流程

```text
validated baseline
      |
      v
quality_preflight ----失败----> 安全停止，保留基线
      |
      v
deterministic_repair
      |
      v
repair_planning ----无模型修复----> final_validation
      |
      v
evidence_packet -> field repair 1/N -> field validator
      |                                  |
      +-------------失败-----------------+--> 安全停止
      |
      v
deterministic_merge（校验字段基线哈希）
      |
      v
final_validation -> shadow candidate（初期仅临时目录）
```

### 4.1 `quality_preflight`

输入是不可变 `EvidenceBundle`、已验证基线文档和固定 `ProfileReference`。Worker：

- 重新执行现有 invariant validator，拒绝把本身不合法的文档当基线；
- 为每个可编辑字段计算 canonical SHA-256；
- 保存整个文档、证据包、校订逐字稿、recording context 和 Profile 的输入指纹；
- 运行 B3.1 最终语义门，输出稳定失败码，不输出自然语言修复指令；
- 确认当前操作为 shadow，不连接 candidate、revision、publication 或 Vault 写入路径。

### 4.2 `deterministic_repair`

下列问题由 Worker 本地规则处理，不注册模型 repair：

| 失败类型 | 本地处理 |
| --- | --- |
| 质量编辑删除已有证据支持的结果 | 从同一基线恢复完整原对象 |
| 摘要声称不存在的决议、负责人或期限 | 删除与最终结构矛盾的句子 |
| 新增 role/affiliation 没有直接依据 | 清空相应字段 |
| 新说话人摘要不能由本人引用逐句支持 | 回退到基线中的该说话人摘要 |
| 重复类别或失效 evidence ID | 使用现有 validator 拒绝，不让模型猜测 |

本阶段若已消除全部失败，直接进入最终校验；不为了“润色”额外调用模型。

### 4.3 `repair_planning`

规划器是固定注册表，不是模型。V1 只允许以下有限 repair：

| repair key | 目标 | 允许修改字段 | 何时启用 |
| --- | --- | --- | --- |
| `meeting_quantitative_promotion` | 把已存在于证据/时间线的关键数字、规则、交付物提升到正文结果 | `highlights`、指定 `topic`、`actions` 或 `open_questions` 中一个明确目标 | 确定性门能定位事实但不能安全决定表达位置 |
| `meeting_speaker_grounding` | 为一个说话人生成受本人证据约束的简短摘要 | 一个 `speaker_summary` | 基线缺失且有足够本人实质发言；不能用于扩写角色 |
| `meeting_topic_detail` | 补齐一个已存在主题的证据化 detail | 一个 `topic.detail` | 现有 bounded topic synthesis 仍无法满足覆盖门 |

摘要与结果一致、结果保留仍属于引擎硬规则，不交给 prompt。一个失败码只能映射到注册 repair 和明确字段；
未知失败码、目标字段不唯一或需要跨字段自由改写时直接停止。

### 4.4 `evidence_packet`

证据包由确定性选择器构建，建议 V1 使用以下硬上限：

- 最多 24 个连续或有明确关联的 segment；
- 规范化文本最多 12,000 个字符；
- 估算输入最多约 4,000 token，以实际 tokenizer 的更严格结果为准；
- 优先包含失败项已有 evidence、数字/规则命中的时间线范围，以及每处最多前后 1 个确认片段；
- speaker repair 只允许该 speaker 自己的实质发言，必要时可加入一条直接点名确认，但不能据此推断职位；
- 不包含人工补充、Vault 路径、发布状态、私有 job 标识或无关全文。

任何上限超出都不做截断式调用。规划器必须缩小目标或安全停止，并记录不含正文的失败原因。

### 4.5 字段级模型调用

每个 repair 使用独立严格 JSON schema，只返回目标对象和 evidence ID。执行硬限制建议为：

- 每个候选最多 3 次字段级模型调用；
- 首版串行执行，避免同机模型并发造成资源和顺序不确定；
- 单次 wall-clock 硬超时 120 秒；
- 单次输出预算 1,024–1,536 token，具体值由 repair schema 决定；
- temperature、模型角色和 context 上限由 Worker 固定；Profile 只能申请更低预算；
- 只有 JSON 截断/解析失败允许一次短重试；重试计入 3 次总调用和总耗时；
- evidence、owner、deadline、role、affiliation 和数字不得由模型补造。

以上是实现门槛候选，不是已经完成的性能承诺。编码前需要用公开合成夹具验证预算足够。

### 4.6 `deterministic_merge`

每个 repair 计划都记录目标字段的基线哈希。合并前必须再次比较：

- 输入文档和目标字段哈希仍与 preflight 一致；
- 返回 schema 只包含被授权字段；
- 所有 evidence ID 存在且满足该 repair 的说话人/时间范围约束；
- 未触及字段 canonical JSON 与基线完全相同；
- 多个 repair 的字段范围不得重叠。若确需同一字段连续修复，应在首版拒绝而不是隐式覆盖。

合并只发生在内存中的临时候选；任何一步失败都丢弃全部局部结果，不做部分持久化。

### 4.7 `final_validation`

合并后必须重新执行：

- `StructuredNoteDocument` schema 与所有 evidence 引用校验；
- meeting context、decision、action 和 category validator；
- B3.1 的结果保留、摘要一致、量化提升和说话人归因门；
- timeline 连续覆盖、人工补充保护、candidate/revision provenance 和发布输入边界检查；
- 未修改字段哈希、原始 ASR、校订逐字稿、当前 Note 和发布回执的不变性检查。

初期成功结果只能写入操作系统临时目录作为 shadow 报告，不生成待采用版本、不发布。

## 5. Profile 与引擎职责

### 5.1 Profile 可以声明

- 已注册 repair 中哪些可用于该 Profile；
- repair 的提示词正文、较小的 field limit 和较低的输出预算；
- 适用的 registered semantic validator；
- renderer 既有栏目名称、顺序和显示策略。

### 5.2 Profile 不可以声明

- 任意 repair key、任意输出字段或执行代码；
- 高于 Worker 硬上限的调用数、token、segment、超时或重试；
- 跳过证据、语义、发布、人工补充或恢复 validator；
- 自动采用候选、覆盖当前 Note 或改变 Vault 位置；
- 用整篇 `quality_edit` 作为字段 repair 失败后的降级路径。

后续实现应在 `execution-policy.json` 中增加声明式 `field_repairs`，但 Worker schema 和注册表必须先定义
硬最大值。实现 B3.2 时创建新的 meeting bundle 版本；不得修改已经固定的 `2026-08-29.1` 内容和哈希。

## 6. 进度与可观察性

Worker 沿用 P0 的无正文心跳，增加以下子阶段：

| 子阶段 | 用户可见信息 |
| --- | --- |
| `quality_preflight` | 正在检查候选和基线 |
| `deterministic_repair` | 正在执行本地一致性修复 |
| `repair_planning` | 发现需修复字段数，不显示正文 |
| `field_repair` | “修复字段 1/2”，repair 类型和已运行时长 |
| `final_validation` | 正在执行整篇证据与一致性校验 |

心跳间隔不超过 10 秒。遥测只记录阶段、repair key、字段类别、证据段数量、估算 token、模型角色、耗时、
错误码、重试和哈希，不记录逐字稿、Note 正文、人名、组织或私有路径。

## 7. 验收门

### 7.1 技术与安全门

- builtin、其他内容类型和现有任务恢复行为不变；
- Profile/bundle 固定、last-known-good 和同版本异哈希拒绝继续通过；
- 字段修复不能导入或调用正式 candidate/revision/publication/Vault 写入；
- 未触及字段字节级不变，所有 evidence 引用有效；
- 语义失败模型重试数为 0；JSON parser 重试最多 1 次且受总预算约束；
- 超时、崩溃和恢复不会留下部分合并结果。

### 7.2 语义质量门

对已经授权的完整长会议样例，至少满足当前已发布基线：

- 已有证据支持的行动、未决问题和实质说话人摘要不得无理由减少；
- `decisions` 为空时，摘要不得声称已经形成多项决定或完成任务分配；
- 证据支持的数字、阈值、范围、交付物和冲突事实应进入合适正文或结果栏目，不能只埋在时间线；
- 不新增无依据的角色、组织归属、负责人、截止时间、数字或决定；
- 最终所有引用、时间线和分类 validator 通过。

这些门只定义通用质量属性；任何私有样例的具体断言都不能写入公开 fixture 或通用提示词正文。

### 7.3 性能门

V1 建议采用以下待实测门槛：

- 若确定性修复已足够，不调用模型，局部质量处理目标小于 1 秒；
- 完整授权长会议样例的 B3.2 总阶段目标不超过 180 秒；
- 单次字段模型调用不超过 120 秒，总调用不超过 3 次；
- 无超过 10 秒的心跳空窗；
- 不能通过截断证据或降低语义门来达标。

若性能门与质量门冲突，保留 builtin 和基线文档，不激活 B3.2。

## 8. 失败、回退与恢复

下列任一情况都返回稳定错误码并保留基线：

- 未知语义失败码或没有唯一 repair 映射；
- 证据包超过硬上限且无法确定性缩小；
- 字段哈希或 Profile/输入指纹在执行期间变化；
- 模型超时、资源门不满足、schema/evidence/字段校验失败；
- 调用数、重试数或总耗时超过预算；
- 最终整篇 validator 失败。

恢复只能复用满足完整指纹且重新校验通过的 evidence packet 和字段候选。若实现持久化缓存，内容必须位于
现有私有 Worker 数据目录并遵守清理策略；shadow 阶段仍只写 `/private/tmp`，正式状态中不得出现半成品。

## 9. 实施顺序

1. 先用公开合成数据实现 repair failure code、planner、packet schema、字段哈希和拒绝测试；不接模型；
2. 实现三种字段级严格 schema、局部 validator、确定性 merge 和最终全局门；
3. 接入可取消的短模型调用、10 秒心跳和硬预算；只运行公开合成 shadow；
4. 证明 builtin 路径、非 meeting 类型、恢复、缓存、候选、发布和删除链路不变；
5. 经项目所有者再次明确授权后，只复用既有私有转写与证据运行一次下游 shadow；
6. 同时通过技术、安全、语义、性能四道门后，输出候选供人工审阅；
7. 只有项目所有者明确确认，才讨论固定新 bundle 并原子切换 meeting 默认 resolver。

## 10. 明确不做

- 不增加整篇质量编辑超时，不再次盲跑完整长会议全文；
- 不用字段调用失败触发整篇模型降级或自动换模型；
- 不把私有会议断言、转写、Note、路径或哈希提交到 Git；
- 不自动采用、发布或覆盖当前 Note；
- 不在 B3.2 中迁移访谈、课程、个人记录或 generic；
- 不进入 P3 并发、Stage J 或正式常用 Vault 迁移。

## 11. 第一、二批实现状态（2026-08-29）

已新增纯模块 `meeting_field_repairs.py`，但没有被 `StructuringProcessor`、API、JobStore、candidate、
revision 或 publication 导入：

- failure code 只能映射到三种注册 repair；摘要矛盾、已有结果删除、角色/组织越权等确定性问题会拒绝
  进入模型规划；
- 每项计划固定唯一字段目标、目标 canonical SHA-256 和 evidence packet SHA-256；同一目标重叠、超过
  3 项、未知 evidence、包超限、说话人没有本人 anchor 均在调用模型前拒绝；
- packet 只保留 `segment_id`、`speaker_id` 和 `text`，最多 24 段、12,000 字符和保守估算 4,000 token，
  超限不会截断；speaker repair 只保留目标说话人自己的片段；
- 已实现三类目标特定的严格结果 schema。speaker 输出不能带 role/affiliation；行动提升不能生成 owner
  或 deadline；所有引用必须来自本次 packet，结果中新增数字必须存在于所引用原文；
- 所有局部结果会先全部校验，再合并到基线深拷贝；目标 hash 漂移、packet hash 漂移、字段上限、越权
  字段和无效证据均拒绝。合并后必须调用整篇 final validator，且 final validator 也不能修改未授权字段；
- 使用公开合成数据的专项 31 项通过；Worker 全量 597 项、Ruff 和 `git diff --check` 通过。

当前仍没有短模型调用、子阶段心跳、checkpoint/cache、shadow 候选或新 Profile bundle。下一批应先建立
仅注入 fake/model callable 的可取消字段 runner 和公开合成 shadow，证明总调用数、parser-only 重试、
120 秒硬超时与 10 秒心跳，然后才创建新的固定 meeting bundle；不得修改 `2026-08-29.1`。

## 12. 第三批公开合成 shadow runner 状态（2026-08-29）

已新增隔离模块 `meeting_field_repair_shadow.py`，使用调用方注入的短调用 adapter 和 final validator：

- 每次 request 只携带单项 plan、目标特定 schema、最小 evidence packet、attempt 和 transport timeout，
  不携带完整基线文档；
- runner 总调用硬上限为 3。JSON 无法解析可对当前 repair 重试一次，但重试计入总调用预算；schema、
  evidence、数字、字段或 final validator 失败都立即停止且不会触发模型重试；
- 单次请求 timeout 不能高于 120 秒，总阶段不能高于 180 秒；未来 transport 必须使用 request 中的 timeout，
  runner 在返回后再次校验实际耗时。超时没有降级、换模型或全文重跑；
- `repair_planning`、`field_repair i/n` 和 `final_validation` 均发出无正文 progress；阻塞调用和最终校验期间
  使用独立 daemon heartbeat，间隔配置不能高于 10 秒；
- 无 repair 时模型调用数为 0，只执行最终整篇 validator；最终门失败不返回局部文档、不修改基线；
- AST 依赖检查证明 runner 不导入 JobStore、checkpoint、revision、artifact、publication 或 API；公开合成
  专项累计 40 项、Worker 全量 606 项、Ruff 和 `git diff --check` 通过。

本批仍未调用 Ollama、未运行私有内容、未创建文件输出、正式候选或新 bundle。下一道门是扩展严格
ProfileBundle 契约并创建新的、未激活 meeting bundle，为三种 repair 提供独立短提示词及只能降低硬上限的
声明式预算；必须保持 `2026-08-29.1` 的文件和哈希不变。

## 13. 第四批严格 Bundle 契约与未激活候选（2026-08-29）

ProfileBundle loader 现支持两种均为严格校验的执行策略形态：旧 Bundle 继续只接受原四个字段；新 Bundle
可额外声明完整的 `field_repairs`。该字段不是任意扩展点：repair key 必须来自 Worker 注册表，prompt 与
policy 的 key 必须完全一致，模型角色固定为 `editor`，所有调用数、输出 token、字段字符、evidence 段数、
evidence 字符/token、单次超时、总超时、心跳和 parser retry 都只能小于等于 Worker 硬上限。未知字段、
未知 repair、缺 prompt、零预算启用 repair 或任何超限值都会拒绝整个 Bundle。

已新增固定候选 `speech-capture/meeting@2026-08-29.2`，canonical bundle SHA-256 为
`640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0`。它复用 `.1` 的主提炼策略，新增：

- `meeting_quantitative_promotion`：只把证据中的数字、阈值、范围、顺序和交付条件补回指定字段；
- `meeting_speaker_grounding`：只使用目标说话人本人片段修复其观点摘要，禁止角色/组织推断；
- `meeting_topic_detail`：只把指定主题写具体，不改变其他主题或把提议升级为决定。

三条 prompt 都只能返回请求附带 schema，不接收完整文档；声明预算为最多 3 次、单次 120 秒、总计
180 秒、10 秒心跳、每项 parser retry 最多 1、输出最多 1,024 token。它们没有获得候选、采用、发布、
Vault、逐字稿或人工补充权限。

默认 `load_bundled_meeting_profile()` 仍明确加载 `2026-08-29.1`，`.2` 的 fallback 也固定指向 `.1`，因此
本批没有改变当前运行行为。回归测试同时固定 `.1` 的 bundle hash 与 `profile.json` 文件 hash，证明旧固定
Bundle 未被修改。公开合成/Bundle 专项累计 62 项，Worker 全量 620 项、Ruff 和 `git diff --check` 通过。

下一道门只能建立一个“validated Bundle → isolated shadow request”的只读 adapter：读取 `.2` 的 prompt
和收紧预算生成 runner 配置，并用公开合成 caller 证明实际请求不超过声明值及 Worker 硬上限。该 adapter
通过前不得把 `.2` 接入默认 resolver、当前任务、真实模型或私有样例。

## 14. 第五批只读 Profile-to-shadow adapter（2026-08-29）

已新增 `meeting_field_repair_profile.py`，只接受经过严格 loader 验证的 meeting ProfileBundle，并生成不可变
`MeetingFieldRepairShadowConfig`。它读取每个已注册 repair 的短 prompt、`editor` 角色、输出预算、字段与
evidence 上限、单次/总超时、心跳和 parser retry；配置包含固定 `profile_id + version + bundle_sha256`，
不包含模型 transport、完整基线、任务状态、路径或发布权限。

profiled wrapper 将该配置交给隔离 runner。单字段 caller request 现在可携带：plan、目标严格 schema、短
prompt、模型角色、最大输出 token、实际 transport timeout 和 Profile 指纹；仍不携带 baseline/document。
Profile 较小的字段字符限制会同时收紧发给 caller 的 JSON schema，并在返回后再次执行本地长度校验；较小的
packet、调用、超时、心跳和重试预算也由 runner 再次验证，不能只依赖 Bundle loader。

runner 对手工伪造配置同样 fail closed：未知 repair、超出 Worker 上限、未启用 repair、计划数超过 Profile
预算或 packet 超过 Profile 声明都在模型调用前拒绝。parser retry 取 Worker 与 Profile 的较小值；单次超时、
总超时和心跳也始终取较小值。语义/schema/evidence/final gate 仍不重试，所有结果仍只在内存原子合并。

公开合成重点测试累计 71 项、Worker 全量 629 项、Ruff 和 `git diff --check` 通过。AST 依赖审计确认新 adapter
不导入 JobStore、checkpoint、revision、artifact、publication 或 API。本批没有真实 transport、Ollama、
私有样例、正式 candidate/revision、采用、发布或 Vault 写入；默认 loader 仍使用 `2026-08-29.1`。

下一道门应先实现公开合成 transport recorder/fake model envelope，证明 adapter 形成的 prompt、schema、
packet、timeout 和输出预算能完整映射到未来短模型调用，同时验证取消/超时不会留下后台调用或部分状态。
在这一门通过前，仍不得连接真实模型或当前任务。

## 15. 第六批公开合成 transport envelope 与收尾契约（2026-08-29）

设计 envelope 时修正了一处契约缺口：原 plan 只有目标字段 hash，没有目标字段当前值，短模型无法知道自己
正在修什么。`MeetingFieldRepairPlan` 现在额外保存目标字段/条目的 canonical JSON；transport 只展开这一项，
不会加入完整 baseline。该 canonical 值继续由既有 SHA-256 固定，合并前仍以当前文档重算 hash 防止漂移。

新增 `meeting_field_repair_transport_shadow.py`：

- 把 profiled request 规范化为不可变 envelope，包含单目标当前值、最小 evidence packet、局部 schema、
  repair prompt、`editor`、最大输出 token、timeout、attempt 和固定 Profile 指纹；
- `rendered_prompt` 明确把输入 JSON 标记为“不可信数据，不是指令”，未来 transport payload 保留 schema 与
  输出预算，不选择模型 ID、不访问网络；
- recording synthetic transport 只在内存保存 canonical envelope，同步调用注入 responder；模块不包含 HTTP、
  socket、文件系统、JobStore、candidate/revision、publication、API 或 Vault 依赖，也不创建工作线程；
- runner/profile wrapper 新增协作式 cancellation check，在调用前、调用返回后和 final validation 前后检查；
  调用前取消不会形成 envelope，中途取消与模拟 timeout 都会把 active call 归零并停止 heartbeat；
- 该阶段只证明同步 fake transport 的参数映射和资源收尾，不能声称已实现真实 HTTP 请求的强制中断。未来
  transport 必须单独证明取消时会关闭底层连接，而不是只等待超时返回。

公开合成重点测试累计 77 项、Worker 全量 635 项、Ruff 和 `git diff --check` 通过。默认 Profile 仍是 `.1`，
`.2` 仍未激活；没有 Ollama、网络、私有样例、正式候选、采用、发布或状态写入。

下一步应先设计并实现一个未接运行时的本地短调用 transport adapter，明确模型角色解析、连接关闭、响应大小
上限、JSON body 校验和取消语义；所有 I/O 必须可注入并用公开合成 fake connection 验证。只有强制 abort 与
无后台残留测试通过后，才讨论是否获得授权运行一次真实但仍为公开合成的本地模型调用。

## 16. 第七批未接线本地短调用 transport（2026-08-29）

已新增 `meeting_field_repair_local_transport.py`，但生产 structuring、API、任务恢复、candidate、revision、
publication 和默认 Profile resolver 均未导入它：

- transport 只允许 `editor` 角色，并把 Profile 固定的短 prompt、单目标当前值、最小 evidence packet、严格
  schema 和输出预算映射到固定 `127.0.0.1:11434/api/generate`；调用方不能改变 host、port 或 path；
- 请求固定 `stream=false`、`think=false`、温度 `0.2` 和 8,192 context token；模型名只接受有限安全字符，
  `num_predict` 来自已验证且不超过 Worker 硬上限的 Profile envelope；
- response 最多读取 256 KiB；HTTP 状态、`Content-Type`、可选 `Content-Length`、UTF-8/JSON、允许字段、
  `done=true`、非空 `response` 以及计数/context 类型均严格校验，未知外层字段会 fail closed；
- transport 可注入 connection factory。取消监控只在单次调用期间存在；取消时主动关闭底层 connection，所有
  成功、拒绝、timeout、I/O 失败和取消路径都会关闭 response/connection 并 join 监控线程；
- 16 项新增测试全部使用公开合成 fake connection，没有打开网络。阻塞 fake 在取消后由监控线程关闭，测试
  同时确认 transport monitor 与 runner heartbeat 都无残留；timeout 会稳定映射到 runner 的
  `field_repair_call_timeout`；
- 源码扫描固定“除该模块自身外无生产模块导入”，依赖审计也确认没有 JobStore、checkpoint、revision、
  artifact、publication、API、Vault 或文件系统依赖。

Worker 全量 651 项测试和全目录 Ruff 已通过。本批没有访问真实 Ollama、没有运行私有内容、没有启用 `.2`、
没有生成或采用正式候选，也没有发布或写 Vault。下一道门需要项目所有者对“一次公开合成、只读、无任务状态
的真实本地模型连通性验证”给出明确授权；普通“继续”不视为真实模型调用授权。即使该门通过，仍不得连接
私有会议或默认运行路径，必须先报告延迟、响应上限、取消行为和严格 schema 结果。

## 17. 第八批一次性公开合成真实本地调用（2026-08-29）

项目所有者明确授权后，使用 `.2`、`qwen3:8b` 和两个公开虚构片段执行了恰好一次真实本地
`POST 127.0.0.1:11434/api/generate`。输入只表达“先核对公开合成范围”和“公开合成匹配率达到 100%”，
没有读取任务、数据库、逐字稿、Note、Vault、私有路径或既有产物。

验证结果：

- 调用数 1，parser retry 0，总耗时 11.357 秒；response body 2,996 bytes，低于 256 KiB 硬上限；
- 外层 Ollama JSON、局部严格 schema、evidence 限定、数字引用和最终文档校验全部通过；量化信息只引用
  `seg_public_2`，合并后的 highlight 数为 2；
- 输入 baseline canonical hash 在调用前后相同；结果只存在于进程内，未写文件、任务、候选或版本；
- response 和 connection 均各关闭一次；调用前后 transport monitor 与 runner heartbeat 都为空；
- 临时 smoke harness 已删除，没有把调用结果正文或临时脚本保留在仓库。

这只证明公开合成的一次真实连通性和正常完成收尾，不证明真实取消能在所有操作系统/HTTP 阻塞点立即中断，
也不构成 `.2` 私有会议质量或性能验收。默认仍为 `.1`，`.2` 仍未激活。下一步应先设计“显式 opt-in、只产生
内存 shadow 结果”的生产桥接边界及拒绝测试；在桥接设计通过、且再次获得特定私有样例授权前，不得运行
私有会议、登记 candidate/revision、采用、发布或切换默认 resolver。

## 18. 第九批显式 opt-in 纯内存 shadow bridge（2026-08-29）

新增 `meeting_field_repair_shadow_bridge.py`，它只组合已经验证的 planner、Profile adapter、runner 与 semantic
gate，不创建本地 transport，也没有被任何生产模块导入。当前 bridge 只允许以下精确 capability：

- `enabled=true`；
- `content_type=meeting`；
- `data_classification=public_synthetic`；
- `result_mode=memory_only` 且 `allow_persistence=false`；
- Profile 必须精确为 `speech-capture/meeting@2026-08-29.2` 和固定 bundle hash。

为了降低误把正式数据带入本阶段 bridge 的风险，输入 segment 只能包含 `segment_id/speaker_id/text`，ID 必须
使用 `seg_public_*` 与 `speaker_public_*`，最多 32 段、总计 16,000 字符；baseline evidence、timeline 边界、
issue anchor 与 speaker target 必须全部指向同一公开合成输入。未知字段、私有路径 metadata、包外 evidence、
旧 `.1`、伪造 hash、空 repair 和任一 capability 不符都会在 caller 前拒绝。

bridge 对 baseline、segments 和 issues 先深拷贝并固定 canonical hash；返回的是独立内存副本和不含正文的
profile/hash/调用计数审计信息。注入的 trusted invariant validator 只能验证，不能规范化或改写任何字段；
其返回 hash 与输入不同即拒绝。之后 bridge 强制运行 `.2` 声明的四个 meeting semantic validators，因此
合法 JSON 和局部 schema 不能掩盖“没有真正补回量化事实”等语义失败。

新增 19 项公开合成 bridge 测试，相关聚焦 90 项、Worker 全量 670 项和全目录 Ruff 通过。AST/源码扫描确认
bridge 不导入 HTTP、本地 transport、文件系统、JobStore、checkpoint、candidate/revision、artifact、
publication 或 API，其他生产模块也不导入 bridge。本批没有再次调用真实模型，没有私有输入、文件输出、
正式状态、采用、发布或默认切换。

下一道门仍不是接生产或跑私有会议。应先设计一个由现有 Worker invariant validator 提供、但保持显式
shadow-only 的受信 validator adapter；它必须证明不会写任务状态，也不能把 validator callback 变成新的权限
注入点。只有该 adapter 的公开合成与依赖隔离测试通过后，才可以请求特定私有样例只读 shadow 授权。

## 19. trusted invariant adapter、显式编排与发布候选审计（2026-08-29）

已完成此前规定的私有样例前置门：

- Worker 正式 meeting invariant validator 已包装为内部 factory 才能创建的 sealed capability；它绑定不可变 evidence
  snapshot，拒绝任意 callback、证据漂移和任何需要规范化的候选，且不依赖任务状态、文件系统或网络；
- 新增唯一 shadow orchestrator，内部组合固定 `.2` Profile、planner、两类允许的 transport、trusted validator、
  semantic gate、进度和取消；它没有被生产执行路径导入；
- 三类 repair 的 15 项端到端矩阵全部通过，覆盖成功、未授权、timeout、调用前取消和一次 JSON retry；
- 修复 standalone 打包发现问题：Profile package data 与九个隔离 shadow 模块现在显式进入冻结包；构建、manifest
  核验和 archive inspection 均通过；
- Worker 全量 696 项、B3.2 聚焦 116 项、插件 80 项、Ruff、协议生成检查、TypeScript 和生产构建通过。

完整证据记录在 `content-profile-b3-2-release-candidate-audit-2026-08-29.md`。结论是：可以申请一次指定私有会议
只读 shadow 授权，但仍不能切默认 Profile、登记/采用候选、发布或写 Vault。

## 20. 私有验收一次性授权入口预备完成（2026-08-29）

在尚未获得私有会议授权的前提下，只用合成私有 ID 补齐了下一门的执行入口：

- capability 必须由显式授权 factory 创建，绑定授权引用、目标 Job、baseline 与 evidence snapshot hash；
- capability 只能 claim 一次，重放拒绝；目标、baseline 或 evidence 漂移在 transport 前拒绝且不会误消耗授权；
- 私有 segment 和 baseline/issue 的全部 evidence、timeline、speaker reference 必须属于授权证据集；
- 真实 local transport 必须与 orchestrator 共享 cancellation callback，保证取消会关闭底层连接；
- 返回仍是内存 document 与内容无关 hash/耗时/changed-fields/progress，`persistence_permitted=false`；
- 新模块没有 JobStore、文件系统、candidate/revision、publication、API 或 Vault 依赖，也没有生产 import。

新增 7 项安全测试后 Worker 全量为 703 项，B3.2 聚焦为 123 项。重新构建的 standalone 已包含该模块，宿主
Mac 完整 verify 通过，manifest SHA-256 为
`46367ee8a5bdb98e53e448eb677f79acbf8ecb0a2cb4224b825785b1d39a979e`。没有读取或运行私有会议。

## 21. 私有验收、默认切换与宿主部署（2026-08-29 至 2026-08-30）

- 最新私有会议真实基线正确执行 0-plan no-op；同证据内存反事实挑战的 speaker/topic 两类修复用 2 次调用、
  30.130 秒通过，且未改变任何正式状态；量化类因没有合约内原始数字锚点为 N/A；
- 最终授权后默认 loader 精确切到 `.2`，三个正式结构化入口统一通过 `for_worker_default()` 注入，解决此前
  “Profile 已打包但正式引擎未使用”的接线缺口；
- `.1` bundle/hash 保持不变，`.2` fallback 仍为 `.1`；`.1 → .2 → .1` 原子回滚与已 pin 任务恢复测试通过；
- Worker 707 项、入口/Profile 聚焦 156 项和静态检查通过；新 standalone 经宿主独立 verify，manifest SHA-256
  为 `6092d84af592bac9cc90995fdcb25df42f62c1474c672fb08b9a95e6c22c9d43`；
- 部署前无处理中任务，旧运行时以明确名称保留。部署后健康、能力协商、模型、Ollama、Tailscale 和任务状态
  均正常；没有重跑或改写旧会议，也没有自动采用或发布权限。
