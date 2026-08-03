# Stage H · 界面消息目录 V1

本文件固定 MVP 的简体中文界面文案和可直接用于后续国际化的英文消息键。图片内文字只用于表达
层级；实现以本文件为准。

## 1. 规则

- 消息键统一使用 `speechCapture.<area>.<meaning>`，键名表达语义，不包含颜色、布局或设备实例名；
- 动态值使用 `{workerName}`、`{current}`、`{total}`、`{minutes}`、`{fileName}` 等命名占位符；
- 后端错误码不能直接展示给用户。界面先按稳定错误码映射本目录文案，仅在诊断详情显示错误码；
- 主按钮写明动作和对象，例如 `重新连接书房 Mac`，不用只有 `重试` 的模糊文案；
- 不伪造完成时间，不把断线解释为 Worker 失败，不把 `已处理` 写成 `已发布`；
- 状态不能只依赖颜色。状态文字、图标和必要说明同时存在。

## 2. 通用与导航

| 消息键 | 简体中文 | 使用位置 |
| --- | --- | --- |
| `speechCapture.workbench.title` | 语音工作台 | 首页标题 |
| `speechCapture.workbench.subtitle` | 把长录音安全地转成逐字稿和可用笔记 | 首页副标题 |
| `speechCapture.task.new` | 新建任务 | 任务列表与空状态 |
| `speechCapture.task.current` | 当前任务 | 窄窗口任务选择器 |
| `speechCapture.note.openClean` | 打开纯净 Note | 任务页 |
| `speechCapture.timeline.open` | 打开时间线 | 任务页 |
| `speechCapture.transcript.openReview` | 复核逐字稿 | 任务页 |
| `speechCapture.history.open` | 查看版本记录 | 总结差异页 |
| `speechCapture.action.cancel` | 取消 | 非破坏性退出 |
| `speechCapture.action.close` | 关闭 | 说明面板 |
| `speechCapture.action.back` | 返回 | 保持上下文的页内返回 |

## 3. 新建任务

| 消息键 | 简体中文 | 说明 |
| --- | --- | --- |
| `speechCapture.intake.title` | 新建语音任务 | 页面标题 |
| `speechCapture.intake.sourceFile` | 来源文件 | 音频选择区 |
| `speechCapture.intake.replaceFile` | 更换文件 | 已选文件动作 |
| `speechCapture.intake.recordingDate` | 录音日期 | 可修改建议值 |
| `speechCapture.intake.recordingDateHint` | 根据文件名建议，可以修改 | 日期说明 |
| `speechCapture.intake.contextLabel` | 补充背景（可选） | 自由文本标签 |
| `speechCapture.intake.contextPlaceholder` | 可以写会议主题、参与者、公司名或你认为有帮助的任何信息 | 不限制句数或格式 |
| `speechCapture.intake.contextHint` | 只作为逐字稿校订和笔记提炼的参考，不会替代录音证据 | 固定安全说明 |
| `speechCapture.intake.processingLocation` | 处理位置 | Worker 选择区 |
| `speechCapture.intake.processingProfile` | 处理模式 | 准确/速度选择区 |
| `speechCapture.intake.profileAccuracy` | 准确优先 | 默认模式 |
| `speechCapture.intake.profileAccuracyHint` | 适合会议、访谈和重要记录 | 模式说明 |
| `speechCapture.intake.profileSpeed` | 速度优先 | 可选模式 |
| `speechCapture.intake.profileSpeedHint` | 资源占用更低 | 模式说明 |
| `speechCapture.intake.contentTypeAuto` | 内容类型 · 自动判断 | 默认内容类型 |
| `speechCapture.intake.submitRemote` | 确认并开始上传 | 远程 Worker 主动作 |
| `speechCapture.intake.submitLocal` | 确认并开始处理 | 同设备 Worker 主动作 |
| `speechCapture.intake.sourceImmutable` | 原始音频不会被修改 | 提交前确认 |
| `speechCapture.intake.contextReferenceOnly` | 补充背景只作为参考 | 提交前确认 |

## 4. Worker、配对与连接

| 消息键 | 简体中文 | 映射条件 |
| --- | --- | --- |
| `speechCapture.worker.connected` | {workerName} · 已连接 | 已认证且能力兼容 |
| `speechCapture.worker.ready` | {workerName} · 已就绪 | 资源和模型满足任务 |
| `speechCapture.worker.localUnavailable` | 这台 Mac · 未检测到可用 Worker | loopback 不可达；不推断原因 |
| `speechCapture.worker.detectAgain` | 重新检测 | 本机或远程重新探测 |
| `speechCapture.worker.installHelp` | 查看安装或启动说明 | 本机 Worker 不可达 |
| `speechCapture.worker.pairingRequired` | 需要连接此设备 | 可达但未配对 |
| `speechCapture.worker.startPairing` | 开始连接 | 打开单页配对 |
| `speechCapture.worker.pairingCode` | 配对码 | 短时凭据输入标签 |
| `speechCapture.worker.pairingCodeHint` | 配对码只用于本次授权，长期凭据会安全保存在系统钥匙串 | 安全说明 |
| `speechCapture.worker.pairingExpired` | 配对码已失效，请在目标 Mac 上生成新配对码 | `PAIRING_SESSION_EXPIRED` |
| `speechCapture.worker.pairingInvalid` | 配对码不正确，请检查后重试 | `PAIRING_CODE_INVALID` |
| `speechCapture.worker.pairingComplete` | 已连接到 {workerName} | 配对成功 |
| `speechCapture.worker.protocolIncompatible` | 当前版本无法与 {workerName} 一起使用 | 协议或产物版本不兼容 |
| `speechCapture.worker.protocolUpdateAction` | 查看更新要求 | 不兼容主动作 |
| `speechCapture.worker.notReady` | {workerName} 尚未准备好 | 可达但资源/模型未就绪 |
| `speechCapture.worker.openManager` | 打开 Worker Manager | 需要在 Worker 端处理 |
| `speechCapture.worker.connectionUnknown` | Worker 状态暂时未知 | 已有任务断线 |
| `speechCapture.worker.retryScheduled` | 将于 {minutes} 分钟后自动重试（{current}/{total}） | 每分钟重试，共三次 |
| `speechCapture.worker.retryExhausted` | 已自动尝试 3 次，任务和最后进度仍已保留 | 三次失败 |
| `speechCapture.worker.reconnect` | 重新连接 {workerName} | Obsidian 主区唯一主动作 |
| `speechCapture.worker.restored` | 已恢复到最新进度 | 重连成功的短暂反馈 |
| `speechCapture.worker.restoredDetail` | 已同步 {count} 个片段，没有重复内容或丢失修订 | 恢复结果 |

## 5. 上传、处理与资源

| 消息键 | 简体中文 | 使用条件 |
| --- | --- | --- |
| `speechCapture.upload.uploading` | 正在上传 | 上传阶段 |
| `speechCapture.upload.progress` | 已上传 {received} / {total} | 字节进度 |
| `speechCapture.upload.paused` | 上传已暂停 | 用户暂停或可恢复中断 |
| `speechCapture.upload.verifying` | 正在检查音频完整性 | 完整上传后校验 |
| `speechCapture.upload.integrityFailed` | 音频完整性检查未通过 | 总体或分块校验失败 |
| `speechCapture.upload.incomplete` | 上传尚未完成，将从缺少的部分继续 | `UPLOAD_INCOMPLETE` |
| `speechCapture.upload.sourceUndecodable` | 无法读取这个音频文件，请更换受支持的音频文件 | `SOURCE_UNDECODABLE` |
| `speechCapture.upload.storageUnavailable` | Worker 暂时无法安全保存上传内容 | `UPLOAD_STORAGE_ERROR` |
| `speechCapture.upload.resume` | 继续上传 | 已有分块可续传 |
| `speechCapture.job.queued` | 等待处理 | 已入队 |
| `speechCapture.job.queuePosition` | 前面还有 {count} 个任务 | 有可用队列位置时 |
| `speechCapture.job.processing` | 正在处理 | 总状态 |
| `speechCapture.job.stageTranscribing` | 正在转写 | 当前阶段 |
| `speechCapture.job.stageAligning` | 正在对齐时间 | 当前阶段 |
| `speechCapture.job.stageSpeakers` | 正在区分说话人 | 当前阶段 |
| `speechCapture.job.stageStructuring` | 正在整理笔记 | 当前阶段 |
| `speechCapture.job.stableTranscript` | 已确认文字 | 稳定逐字稿 |
| `speechCapture.job.provisionalTranscript` | 临时结果 | 仍会变化的尾部 |
| `speechCapture.resource.memoryWarning` | 当前内存压力较高，处理可能变慢 | 警告但不阻断 |
| `speechCapture.resource.safePaused` | 已在安全位置暂停 | 阻断或显式暂停 |
| `speechCapture.resource.diskBlocked` | 可用空间不足，任务已安全暂停 | 磁盘阻断 |
| `speechCapture.resource.diskNeed` | 预计还需要 {required}，当前可用 {available} | 空间事实 |
| `speechCapture.resource.diskReserve` | 系统会保留至少 {reserve} 的安全空间 | 安全策略 |
| `speechCapture.resource.savedProgress` | 已上传音频、逐字稿和处理进度均已保留 | 阻断说明 |
| `speechCapture.resource.checkAgain` | 重新检测空间 | 用户完成外部清理后 |
| `speechCapture.resource.viewWorkerUsage` | 查看 Worker 占用 | 只读 Worker 自有占用 |
| `speechCapture.job.partial` | 部分完成 | 有明确未决范围 |
| `speechCapture.job.partialDetail` | 有 {count} 处需要复核，其余结果可以正常使用 | 部分完成说明 |
| `speechCapture.job.failed` | 当前阶段未能完成 | 可恢复失败 |
| `speechCapture.job.retryStage` | 重试当前阶段 | 不重跑已成功阶段 |

## 6. 逐字稿、音频与修订

| 消息键 | 简体中文 | 使用位置 |
| --- | --- | --- |
| `speechCapture.review.title` | 逐字稿与证据复核 | 页面标题 |
| `speechCapture.review.playLocal` | 本地音频 | 当前设备同源文件 |
| `speechCapture.review.playRemote` | {workerName} 在线 · 流式播放 | 私有 Worker 音频流 |
| `speechCapture.review.audioUnavailable` | 当前无法播放音频，逐字稿仍可阅读和修改 | Worker 离线或音频过期 |
| `speechCapture.review.previousEvidence` | 上一条证据 | 音频导航 |
| `speechCapture.review.nextEvidence` | 下一条证据 | 音频导航 |
| `speechCapture.review.speakerDisplayName` | 说话人显示名 | 批量命名区 |
| `speechCapture.review.renameSpeaker` | 批量改显示名 | 只改人物标签 |
| `speechCapture.review.segmentSpeaker` | 这段话是谁说的？ | 单段归属区 |
| `speechCapture.review.segmentSpeakerHint` | 只纠正当前这一段，不会影响其他段落。 | 固定说明 |
| `speechCapture.review.speakerSearch` | 搜索说话人 | 多人选择器 |
| `speechCapture.review.speakerUnknown` | 暂不确定 | 多人清单固定选项 |
| `speechCapture.review.textCorrection` | 文字校订 | 当前段输入区 |
| `speechCapture.review.rawAsrProtected` | 原始识别不会被改写，修改会记录为新修订。 | 修订安全说明 |
| `speechCapture.review.saveSegment` | 保存此段修订 | 单段主动作 |
| `speechCapture.review.unsavedDraft` | 当前片段有未保存的修订 | 收起右栏状态说明 |
| `speechCapture.review.saved` | 此段修订已保存 | 短暂反馈 |
| `speechCapture.review.revisionChanged` | 这段内容刚刚发生变化，请确认最新内容后再保存 | 修订版本冲突 |
| `speechCapture.review.reloadSegment` | 载入最新内容 | 修订版本冲突主动作 |
| `speechCapture.review.regenerateRecommended` | 修订会影响笔记内容，建议重新生成 | 推荐状态 |
| `speechCapture.review.regenerate` | 重新生成笔记 | 进入重新生成流程 |

## 7. 总结差异与版本记录

| 消息键 | 简体中文 | 使用位置 |
| --- | --- | --- |
| `speechCapture.summaryDiff.title` | 比较重新生成的笔记 | 差异页标题 |
| `speechCapture.summaryDiff.current` | 当前版本 | 旧版标签 |
| `speechCapture.summaryDiff.candidate` | 候选版本 | 新版标签 |
| `speechCapture.summaryDiff.accept` | 接受新版笔记 | 整版接受 |
| `speechCapture.summaryDiff.acceptHint` | 新版将成为当前 Note，旧版和本次差异仍可查看 | 后果说明 |
| `speechCapture.summaryDiff.continueEditing` | 继续修改逐字稿 | 返回复核 |
| `speechCapture.summaryDiff.decline` | 不采用新版 | 整版不采用 |
| `speechCapture.summaryDiff.declineHint` | 当前 Note 保持不变，候选版会以未采用状态保留 | 后果说明 |
| `speechCapture.summaryDiff.userSectionProtected` | “我的补充”保持不变 | 受保护区域 |
| `speechCapture.versionHistory.title` | 版本记录 | 只读历史页 |
| `speechCapture.versionHistory.current` | 当前使用 | 当前版状态 |
| `speechCapture.versionHistory.accepted` | 已采用 | 历史版状态 |
| `speechCapture.versionHistory.declined` | 未采用 | 历史版状态 |
| `speechCapture.versionHistory.openVersion` | 查看此版本 | 只读动作 |
| `speechCapture.versionHistory.compareCurrent` | 与当前版本比较 | 只读动作 |
| `speechCapture.versionHistory.mvpLimit` | 第一版只提供查看和比较，不支持回滚、合并或删除 | 边界说明 |

## 8. 处理完成、发布与冲突

| 消息键 | 简体中文 | 使用条件 |
| --- | --- | --- |
| `speechCapture.publish.processed` | 已处理，等待发布 | Worker 产物已验证 |
| `speechCapture.publish.pendingDetail` | 结果已安全保存在 {workerName}，尚未写入当前 Vault | 无授权客户端在线 |
| `speechCapture.publish.autoResume` | 已授权客户端恢复连接后会自动发布 | 待发布说明 |
| `speechCapture.publish.publishing` | 正在写入 Obsidian | 发布租约已取得 |
| `speechCapture.publish.otherClientActive` | 另一台已授权设备正在发布，完成后会自动同步状态 | 发布租约冲突 |
| `speechCapture.publish.conflict` | 发现发布冲突 | 目标发生变化 |
| `speechCapture.publish.conflictDetail` | 当前 Vault 内容和新结果都已保留，不会自动覆盖 | 冲突说明 |
| `speechCapture.publish.viewDiff` | 查看差异 | 冲突首屏唯一主动作 |
| `speechCapture.publish.saveNewLocation` | 保存到新位置 | 查看差异后动作 |
| `speechCapture.publish.chooseNewLocation` | 选择新位置 | 位置输入标签 |
| `speechCapture.publish.published` | 已发布到 Obsidian | 写入并校验成功 |
| `speechCapture.publish.openNote` | 打开 Note | 发布成功主动作 |
| `speechCapture.publish.verificationFailed` | 写入后的完整性检查未通过，现有内容没有被覆盖 | 发布验证失败 |

## 9. 实现映射底线

- `SERVICE_NOT_INSTALLED`、`SERVICE_NOT_RUNNING` 和端口不可达只有在可信 Manager 状态可读时才能
  分别映射说明；纯粹探测失败统一使用 `speechCapture.worker.localUnavailable`；
- `DISK_RESERVE_TOO_LOW`/`DISK_RESERVE_LOW` 映射磁盘阻断；`DISK_RESERVE_WARNING` 只映射警告；
- `MEMORY_PRESSURE_BLOCKED` 映射安全暂停，`MEMORY_PRESSURE_WARNING` 映射处理可能变慢；
- `protocol_version_incompatible` 或 `artifact_schema_incompatible` 映射版本不兼容，不能开始上传；
- `PAIRING_SESSION_EXPIRED` 和 `PAIRING_CODE_INVALID` 使用各自文案，不暴露认证细节；
- 分块 checksum 错误只重传受影响分块；整份音频 checksum 错误先重新核对服务端分块清单，无法
  定位时再明确要求安全重传，不能谎称已经知道损坏范围；
- `VAULT_PUBLICATION_CONFLICT` 必须进入先查看差异的冲突流程；
- 未识别错误使用 `speechCapture.job.failed` 加可复制的诊断编号，不展示堆栈、本地路径或私人内容。
