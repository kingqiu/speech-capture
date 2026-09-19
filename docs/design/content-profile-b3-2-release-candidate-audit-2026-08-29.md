# B3.2 有界字段级会议纪要修复：发布候选审计

日期：2026-08-29
结论：**经授权的最新私有会议只读 shadow 与上线前门均通过；项目所有者随后明确授权，会议默认 Profile 已受控切换到 `2026-08-29.2` 并部署到宿主 Worker。既有任务、Note、发布状态和 Vault 未被改写。**

## 1. 本轮边界

本轮先完成 B3.2 候选能力的代码、测试、打包和隔离审计，之后在项目所有者明确授权下只读加载最新私有会议的
既有 `speech-record.json`，执行一次真实基线 no-op 和一次纯内存反事实挑战。没有重跑音频，没有写任务、
checkpoint、candidate/revision、Note 或 Vault，也没有改变发布状态。受控部署后，三个正式结构化入口统一通过
`OllamaStructuringEngine.for_worker_default()` 加载 `2026-08-29.2`；`.1` 文件和固定 hash 未改变，并保留为
精确回滚版本。

## 2. 候选身份

| 对象 | 身份 |
| --- | --- |
| 当前默认 Profile | `speech-capture/meeting@2026-08-29.2` |
| 当前默认 bundle SHA-256 | `sha256:640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0` |
| 精确回滚 Profile | `speech-capture/meeting@2026-08-29.1` |
| 回滚 bundle SHA-256 | `sha256:903bff654e1c112209610f876b529abce34aa7ab279964b5927334bb32c59c6f` |
| Python wheel SHA-256 | `a5490bfbe9abc65e710544a5af011a3ff707b631a16006779688d4ab4fac5727` |
| Standalone manifest SHA-256 | `6092d84af592bac9cc90995fdcb25df42f62c1474c672fb08b9a95e6c22c9d43` |
| Standalone 内容 | 3,438 files / 1,298,528,751 bytes |

## 3. 审计后的执行边界

唯一完整编排路径为：显式 shadow capability → 固定 `.2` Profile → 单字段 planner → 受支持 transport →
trusted invariant validator → meeting semantic gate → 独立内存结果。

- 编排器只接受 recording synthetic transport 或固定 loopback 的本地 Ollama transport；调用方不能注入任意 transport；
- invariant validator 只能由 Worker 内部 factory 创建，绑定不可变 evidence snapshot 与正式 meeting 文档校验器；
- validator 遇到需要规范化的文档会拒绝，不能借验证过程静默改写候选；
- baseline、segments 和 issues 全部深拷贝，最终结果为独立内存对象；
- 编排器及 bridge 没有被生产 structuring、API、恢复、发布或默认 resolver 导入；
- shadow 模块没有 JobStore、checkpoint、candidate/revision、artifact、publication、Vault 或正式 API 依赖。

私有验收另有一个未接生产的一次性 capability：只有 `explicit_authorization=true` 才能创建，并绑定授权引用、
目标 Job、完整 baseline 和 evidence snapshot 的 SHA-256。同一 capability 只能成功 claim 一次；目标、基线或证据
漂移会在 transport 调用前拒绝且不会消耗授权。私有本地 transport 必须与 orchestrator 共用同一 cancellation
source，避免只停止上层等待而没有关闭底层 HTTP connection。

因此当前候选即使存在于安装包中，也不会因普通任务执行而被触发。

## 4. 三类 repair 端到端矩阵

对 `.2` 注册的三个 repair 均完成公开合成端到端验证：

1. quantitative promotion；
2. speaker grounding；
3. topic detail。

每类均覆盖成功且无部分写入、未授权拒绝且不重试、timeout 清理后新请求恢复、调用前取消零 transport 调用、
非法 JSON 只允许一次解析重试后恢复，共 15 项矩阵测试。额外验证 baseline/evidence 不变、heartbeat 和 transport
monitor 无残留、最终文档经过正式 invariant 与语义门。

## 5. 验证结果

| 检查 | 结果 |
| --- | --- |
| Worker 全量测试 | 707 项通过 |
| B3.2 聚焦测试 | 124 项通过 |
| 三类 repair E2E 矩阵 | 15 项通过 |
| standalone/profile 聚焦测试 | 37 项通过 |
| Obsidian 插件测试 | 17 files / 80 tests 通过 |
| 插件 TypeScript + production build | 通过 |
| Ruff | 通过 |
| protocol generated types check | 通过 |
| `git diff --check` | 通过 |
| standalone build + 独立 `--verify` | 通过，manifest hash 一致；私有入口与核心 gate 均存在于冻结 PYZ |

## 6. 打包风险与修复

隔离 shadow 模块没有生产 import，这正是运行时安全要求，但也意味着 PyInstaller 不能依赖普通 import graph 自动发现它们。
此外 Profile JSON/Markdown 也必须作为 package data 显式进入冻结包。构建脚本现已：

- 收集 `speech_capture_worker` package data；
- 把十个 B3.2 隔离模块列为 hidden imports；
- 在构建后核验 `.1`、`.2` Profile 文件都存在；
- 使用 PyInstaller archive viewer 复核十个模块全部进入 PYZ，包括一次性私有验收入口。

同一候选包先在沙箱内执行独立 verify 时，`verify-model-runtime` 因沙箱无 Metal 设备以 134 退出；两个 CLI
入口均正常。这不是包缺失。随后在宿主 Mac 对同一目录执行完整 `--verify`，模型 runtime import、临时状态
smoke test、manifest、禁止私有构建路径扫描和 macOS code signature 全部通过，且 manifest hash 未变化。

这只解决“候选安装包是否完整”，不会把候选接入默认执行路径。

## 7. 经授权私有只读 shadow 验收

授权目标只读加载既有结构化文档和 evidence。报告不保留输入规模、会议正文、目标身份、私有路径或输入哈希。

真实基线先通过四个 meeting semantic validators，未发现高置信 repair issue。一次性私有 capability 因此执行
零计划 no-op：0 次模型调用、0 个变化字段、耗时 0.006 秒，baseline/result hash 完全一致。这证明“无问题时不
为了制造新版而改写纪要”。

随后只在进程内副本制造两个有真实 evidence 的退化：把一个说话人摘要替换为待修复占位、清空一个现有主题的
details。`.2` 使用 `qwen3:8b` 完成 2 次字段调用，0 次 parser retry，耗时 30.130 秒；结果只修改
`speaker_summaries` 与 `topics`，speaker grounding 和 topic detail 两项验收均通过。

量化修复在该私有会议上标记为 **N/A**：原始证据中没有符合当前契约的“阿拉伯数字＋受支持单位”
锚点，不能用纪要自身生成的数字反向冒充原始证据。该类仍由公开合成 E2E 矩阵覆盖，本次没有伪造私有通过。

调用前后 Job 状态、版本、数据库行和产物指纹完全一致。私有 runner 返回 `persistence_permitted=false`，正式
状态保护门通过；报告不记录私有状态值、数量或联合哈希。

## 8. 已知限制和部署边界

- 私有会议已验证 no-op、speaker grounding、topic detail、180 秒总性能和正式状态保护；量化类因该样例没有
  合约内原始数字锚点为 N/A；
- 当前私有样例证明局部修复可在约 30 秒完成，但真实基线本身无高置信问题，因此没有产生可与 `.1` 比较的
  正式整篇候选，也不构成自动切换理由；
- 默认 Profile 已切换；既有候选登记、采用、发布和 Vault 写入均未执行。

`.2` 作为正式会议内容 Profile 现在会向新建或重新提炼的 meeting 结构化引擎提供 extraction、synthesis、
quality-edit、meeting-outcomes 提示和注册的确定性语义门。三个字段级 repair 仍保持有界、显式、内存原子合并
边界；本次切换不会自动重写旧 Note，也没有新增自动采用或自动发布权限。

全量回归、冻结包重建、宿主独立 verify 和纯内存回滚演练均已完成。回滚演练确认 `.1 → .2 → .1` 三次原子
generation 变更，新任务回到 `.1`，而已固定 `.2` 的任务 pin 仍能精确解析；失败激活不会破坏当前版本或
last-known-good。

## 9. Go / No-Go

- **已执行 GO**：项目所有者明确授权后完成 `.2` 默认切换和宿主部署，并保留 `.1` 精确回滚能力。
- **仍为 NO-GO**：自动采用候选、自动发布、批量重写既有 Note 或改变 Vault 位置。

## 10. 受控默认切换与宿主部署（2026-08-30）

- 修复部署前审计发现的入口缺口：此前 Profile 虽已打包，后台 STRUCTURING、CLI `run-structuring` 和 API
  `regenerate_summary` 三个正式入口没有注入它；现统一调用 `for_worker_default()`；
- 默认 loader 精确固定 `.2`；`.2` manifest 的 fallback 继续固定 `.1`，旧 `.1` 文件和 hash 未修改；
- Worker 全量 707 项和入口/Profile 聚焦 156 项通过，Ruff、构建前检查和补丁格式检查通过；
- 重建包为 3,438 files / 1,298,528,751 bytes，独立宿主 verify 与部署后的 `runtime-manifest.json` SHA-256
  均为 `6092d84af592bac9cc90995fdcb25df42f62c1474c672fb08b9a95e6c22c9d43`；
- 部署前只有 8 个 `published` 和 1 个历史 `failed` 任务，没有处理中任务。旧运行时保留为
  `SpeechCaptureWorker.backup-20260830-pre-b3-2-default`；
- 新服务启动后 health、capabilities、协议协商、模型、Ollama、Tailscale 和端口检查均通过，任务状态计数未变；
  没有重跑音频、重新提炼旧会议、写 Note、采用候选或发布。
