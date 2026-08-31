# 内容 Profile 职责盘点 V1

日期：2026-08-28
状态：实现前设计；本文件不改变 Worker、插件、协议、数据库或运行服务

## 1. 盘点结论

当前内容提炼已经有一个集中入口 `note_prompt_profiles.py`，但还不是完整的内容 profile。场景策略目前
分散在四类位置：

1. `note_prompt_profiles.py` 中的内容优先级、场景说明、栏目名称和场景章节类型；
2. `structuring_execution.py` 中的模型提示词、输出 JSON schema、场景二次修复、确定性规范化、证据校验、
   缓存指纹和恢复版本；
3. `artifact_generation.py` 中的 Markdown 栏目顺序、显隐、证据链接、回退文档和旧兼容渲染；
4. `summary_revisions.py` 中的候选版本、人工草稿、采用/拒绝和重新发布状态。

因此，第一轮解耦不能把上述内容一起搬进一个可自由编辑的提示词文件。正确边界是：

- **内容目标和表达策略**进入版本化 `ProfileBundle`；
- **结构语义**进入稳定、版本化 `StructuredNoteDocument`；
- **证据、权限、恢复、状态机和不可伪造约束**继续留在执行引擎；
- **人工草稿与发布决策**继续留在修订/发布控制面；
- **Markdown 表达**由受控 renderer 配置决定，不能反向改变证据文档。

## 2. 当前版本常量与职责

| 当前常量 | 位置 | 当前作用 | 目标归属 |
| --- | --- | --- | --- |
| `NOTE_PROMPT_VERSION` | `note_prompt_profiles.py` | 同时代表提取、综合和多类场景提示词版本 | 拆为 `profile_version` 和 bundle 总哈希 |
| `STRUCTURING_SCHEMA_VERSION` | `structuring_execution.py` | 可恢复结构化检查点 schema | 继续由引擎管理 |
| `STRUCTURING_RAW_SCHEMA_VERSION` | 同上 | 私有原始结构化证据 schema | 继续由引擎管理 |
| `STRUCTURING_CACHE_SCHEMA_VERSION` | 同上 | 缓存 envelope 兼容边界 | 继续由引擎管理 |
| `STRUCTURING_BATCHING_VERSION` | 同上 | 分批算法版本 | 继续由引擎管理；profile 只能在受控范围内给预算 |
| `SCENE_COVERAGE_REPAIR_VERSION` | 同上 | 非会议场景查漏策略 | 提示词移入 profile；算法/校验版本留在引擎 |
| `MEETING_QUALITY_REPAIR_VERSION` | 同上 | 会议质量编辑策略 | 提示词和声明式规则移入 meeting profile；安全修复留在引擎 |
| `MEETING_OUTCOME_REPAIR_VERSION` | 同上 | 会议结果项修复 | 同上 |
| `INTERVIEW_QUALITY_REPAIR_VERSION` | 同上 | 访谈质量编辑策略 | 提示词移入 interview profile；不推断规则留在引擎 |
| `VOICE_MEMO_QUALITY_REPAIR_VERSION` | 同上 | 个人备忘质量编辑策略 | 提示词移入 voice-memo profile；安全约束留在引擎 |
| `SUMMARY_REVISION_SCHEMA_VERSION` | 同上/`summary_revisions.py` | 候选 Note 版本记录 | 继续由修订控制面管理 |
| `SUMMARY_REVISION_DRAFT_SCHEMA_VERSION` | `summary_revisions.py` | 人工编辑候选草稿版本 | 继续由修订控制面管理 |

## 3. 逐文件职责迁移表

### 3.1 `note_prompt_profiles.py`

| 当前内容 | 处理决定 | 原因 |
| --- | --- | --- |
| `_COMMON_SALIENCE` | 迁入每个 bundle 可继承的受控公共策略 | 属于内容优先级，不改变证据边界 |
| `_PROFILE_GUIDANCE` | 迁入各内容类型的 `synthesize` / `quality_edit` 提示词 | 是核心场景策略 |
| `_SCENE_SECTION_KINDS` | 迁入 profile 的字段使用声明，但允许值仍由文档 schema 注册表校验 | profile 决定本类型使用哪些已支持语义，不得创造未知字段 |
| `_SCENE_SECTION_LABELS` | 迁入 renderer 配置 | 仅影响人类显示名称 |
| `_RENDER_HEADINGS` | 迁入 renderer 配置 | 仅影响栏目标题和顺序 |
| `extraction_guidance()` | 改为 loader 读取已编译模板 | 不再在 Python 拼接场景正文 |
| `synthesis_guidance()` | 改为 loader 读取已编译模板 | 同上 |
| `output_contract_guidance()` | 拆分：内容说明进 profile，真正 JSON schema 留在文档契约 | 防止提示词声明与实际校验漂移 |

迁移完成前，该文件保留为 last-known-good 内置回退，不在阶段 B 删除。

### 3.2 `structuring_execution.py`：模型调用

| 当前能力 | 目标归属 | 说明 |
| --- | --- | --- |
| 内容类型分类提示词 | 引擎内置 V1，后续可做独立 classifier profile | 第一轮 meeting 迁移不顺带改变分类行为 |
| 批次候选提取提示词 | ProfileBundle `prompts.extraction` | `FindingKind` 枚举与证据 ID 校验仍留在引擎 |
| 全局文档综合提示词 | ProfileBundle `prompts.synthesis` | JSON schema 由引擎传入，不由提示词自行定义 |
| 场景覆盖查漏提示词 | ProfileBundle `prompts.coverage_repair` | 调用次数上限与无结果回退留在引擎 |
| 会议/访谈/个人备忘质量编辑提示词 | ProfileBundle `prompts.quality_edit` | profile 可缺省；引擎按受控 pipeline 决定是否调用 |
| 会议结果项、话题等局部修复提示词 | ProfileBundle `prompts.named_repairs` | 只能引用引擎注册的 repair slot，不能增添任意阶段 |
| transcript polish 提示词 | 暂留引擎 | 它修改校订逐字稿候选，不是 Note 表达策略，需另行设计 |
| 模型名、token 预算、context 预算 | profile 的受限 `execution_policy` + 引擎硬上限 | profile 可选已注册角色，不能指定任意服务或超越资源上限 |
| heartbeat、超时、重试 | 引擎 | 运行可靠性，不能由内容 profile 关闭 |

### 3.3 `structuring_execution.py`：schema 与确定性校验

以下能力**不得搬入提示词包**：

- segment ID、speaker ID、时间范围和证据是否存在；
- JSON 字段集合、类型、长度、数量硬上限和未知字段拒绝；
- 决议是否有确认性证据，负责人/期限是否可由原文直接证明；
- 原始 ASR、校订逐字稿和人工修订账本的不可变边界；
- 内容类型、任务状态、检查点世代、幂等键和并发冲突；
- 安全暂停、恢复、缓存原子写入及私有证据路径；
- profile 不能关闭的通用反幻觉校验。

当前 `_validate_document()` 同时混有三种职责，实施时必须拆开但保持同一结果：

| 类别 | 示例 | 目标 |
| --- | --- | --- |
| 结构不变量 | 字段集合、类型、证据 ID、长度上限 | `DocumentInvariantValidator`，永远执行 |
| 场景质量规则 | 会议背景不能过少、访谈已回答问题不进 open questions | 注册式 `ProfileQualityValidator`；profile 只能选择已审核规则 |
| 确定性正文修复 | 去掉无证据主持人推断、清除泛化或重复内容 | 保留为版本化 engine normalizer；不得让 profile 注入代码 |

第一轮不要求物理拆分函数，只要求契约和测试先区分这三类。

### 3.4 `artifact_generation.py`

| 当前内容 | 处理决定 |
| --- | --- |
| `_build_note_markdown()` 的固定栏目顺序与标题 | 迁入 renderer plan；渲染执行器仍为代码 |
| “会议用 topics、其他类型用 scene_sections”的选择 | 迁入 profile 的 section plan，但字段必须已存在于文档 schema |
| 空栏目不显示、表格/列表样式、纯净 Note 与证据 Note | renderer 配置 |
| 证据链接、block ID、时间排序、frontmatter 安全字段 | 留在 renderer 引擎代码 |
| `_fallback_document()` | 留作失败恢复，但输出必须升级为同一文档契约 |
| `_content_sections()` | 仅旧回退兼容；迁移后由 renderer plan 代替 |
| 人工“我的补充”保护 | 留在产物/修订控制面，不归 profile |
| speaker display name 人类化 | 留在引擎；profile 只决定是否显示参与者栏目 |

### 3.5 `summary_revisions.py`

该文件不迁入 profile。候选版本、人工草稿、采用/拒绝、已发布 Note 分叉和重新发布均属于产品状态机。
只补充 provenance：每个候选必须记录 `profile_id`、`profile_version`、`profile_sha256`、
`document_schema_version` 和 `renderer_version`，使 V1/V2 差异可追溯。

### 3.6 API、插件与发布

- 第一轮不改变 API 路径、任务状态或插件提交格式；
- 插件只新增只读 provenance 展示时，才需要协议兼容扩展；
- profile 不进入 Vault，不由远程笔记本执行，也不随 Note 发布；
- 删除、配对、上传、ASR、说话人识别、版本确认和冲突发布不在本轮迁移范围。

## 4. Profile 可声明与不可声明的边界

### 4.1 可声明

- 内容目标、信息优先级、禁止把哪些过程信息当重点；
- 提取、综合、查漏和质量编辑提示词；
- 使用已有文档字段的必需/可选/允许为空规则；
- 已有字段的栏目名称、顺序、显示条件和 Markdown 表达；
- 已注册模型角色、批次预算和一次已注册质量编辑；
- 选择已注册的场景质量 validator 和声明式阈值；
- profile 自带的公开合成夹具及期望结构。

### 4.2 不可声明

- 任意 Python/JavaScript、shell、URL、网络模型地址或文件路径；
- 关闭证据校验、权限校验、幂等、恢复、资源预检或原子写入；
- 修改原始音频、原始 ASR、人工修订账本或当前已发布 Note；
- 创造未注册语义字段、未知模型阶段或新的 API 状态；
- 自动采用、覆盖或发布新候选；
- 将用户补充背景当成独立事实证据。

## 5. 第一轮迁移范围

只迁移 meeting profile 的下列部分：

1. 批次提取的会议内容规则；
2. 全局综合的会议内容规则；
3. 当前会议质量编辑和结果项修复的提示词正文；
4. meeting 已有字段的标题、顺序和显隐；
5. 当前会议 profile 版本进入缓存键和候选 provenance。

明确不迁移 transcript polish、分类器、上传/发布、删除、修订状态机和其他内容类型。其他类型仍走当前
内置路径，直到各自代表样例质量门具备。

## 6. 盘点验收

进入代码阶段前必须满足：

- 每一段被移出的提示词正文都有唯一目标 slot；
- 每个当前版本常量都有保留、拆分或替代说明；
- 所有证据和安全不变量明确留在引擎；
- renderer 配置只能读取文档字段，不能制造事实；
- 当前内置路径可以作为 last-known-good 回退；
- meeting 之外的运行行为在第一轮保持不变。

## 7. 阶段 B1 落地记录（2026-08-28）

本盘点对应的第一层基础设施已经实现，但没有接入运行时：

- profile 允许声明与禁止声明的边界已由严格 loader 执行；
- 当前结构化 payload 可无语义改写地适配为稳定文档 envelope；
- `structuring_execution.py`、`artifact_generation.py` 和 `summary_revisions.py` 的职责尚未迁移；
- meeting 及其他类型继续走原内置路径，现有提示词、渲染和修订状态机保持不变；
- 完整 Worker 代码检查和 614 项自动测试通过。

后续如进入 B2，只能新增影子双读与等价校验，不得借机迁移发布、删除、上传或其他产品状态机。
