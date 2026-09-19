# Speech Capture 插件受控更新方案 V1

日期：2026-09-19

状态：发布清单、不可变 Worker release store、宿主本机导入命令、认证只读 API、插件端检查/下载/校验/
明确确认、退出后安装 helper、失败回滚、重开后加载校验，以及独立外部备份恢复工具均已完成；独立远程
测试 Vault 的升级—回滚—再升级闭环尚未执行

## 1. 目标

远程笔记本上的普通更新不再依赖复制长命令，也不能再出现以下情况：

- 安装到了没有被 Obsidian 打开的 Vault；
- `manifest.json` 显示新版本，但实际加载的 `main.js` 或插件目录仍是旧版；
- 备份目录留在 `.obsidian/plugins/` 后被 Obsidian 当成同 ID 插件扫描；
- 下载、复制或重载失败后没有明确结果，也没有可恢复旧版；
- 发布者和客户端校验的不是同一份不可变构建产物。

V1 只服务于 macOS 桌面个人 Alpha。它必须显式征得用户确认，不允许静默更新、自动选择 Vault、修改
`data.json`、改变 Worker 配对或把私人内容加入更新诊断。

## 2. 已完成的发布侧基础

`pnpm release:alpha` 已形成唯一候选构建入口：

1. 运行插件和发布工具测试、类型检查及生产构建；
2. 校验 `package.json`、`manifest.json`、`versions.json` 的版本一致性；
3. 生成只含三个运行文件的可复现 ZIP；
4. 生成版本绑定安装脚本和 `release.json`；
5. 连续构建两次并要求 ZIP、installer、release manifest 字节级一致；
6. 在临时合成 Vault 中执行真实安装，验证设置保留、旧版备份、重复同 ID 目录迁移和损坏包拒绝。

发布清单只含插件 ID、版本、最低 Obsidian 版本、平台边界、文件名、文件大小、ZIP 条目和 SHA-256，
不得包含 Vault、设备、用户、网络地址、令牌、音频、逐字稿或 Note 信息。后续签名字段只能扩展 schema，
不能改变已有哈希语义。

## 3. 分发边界

个人 Alpha 的推荐分发源是已配对的书房 Worker，而不是公开 GitHub 地址或带凭据的 URL：

- 发布者把经过验证的不可变四件套放入 Worker 的独立 release store；
- Worker 只向已认证、未撤销且有当前 Vault 授权的设备提供版本元数据和下载；
- 元数据接口不返回本地文件路径，下载接口不接受任意路径参数；
- release store 与任务 artifact store 分离，删除任务、音频或 Note 不得影响安装包；
- Worker 不主动推送或安装，插件只在用户打开更新入口或执行低频检查时读取元数据。

第一版接口建议为：

- `GET /v1/client-releases/speech-capture/latest`：返回兼容版本的无内容元数据；
- `GET /v1/client-releases/speech-capture/{version}/archive`：返回固定 ZIP，支持 `ETag`；
- 后续再增加签名文件；不得把管理员上传接口暴露给普通配对设备。

## 4. 客户端状态机

插件更新必须展示可恢复的确定状态：

1. `current`：当前版本已是最新；
2. `available`：显示当前版本、候选版本、兼容性与更新说明；
3. `downloading`：下载到 Vault 外的插件更新暂存区；
4. `verified`：ZIP、release manifest、条目和文件哈希全部通过；
5. `awaiting_confirmation`：用户明确确认更新及重启；
6. `applying`：原子替换期间禁止第二次操作；
7. `restart_required`：文件已就位，但不谎报为“已加载”；
8. `loaded_verified`：重启后由新插件确认运行版本与磁盘版本一致；
9. `rolled_back` 或 `failed`：显示失败阶段、旧版是否恢复和下一安全动作。

下载失败、Worker 离线和校验失败不能改动活动插件。只有 `loaded_verified` 可以显示“更新完成”。

## 5. 安装与回滚事务

更新事务只针对 Obsidian 当前实例实际加载的 `vault.configDir/plugins/speech-capture`：

- 不按文件夹名称、最近修改时间或全盘搜索猜 Vault；
- 新文件先写入同一文件系统的 staging 目录并完整校验；
- `data.json` 只复制，不解析、不改写、不进入 release store；
- 旧插件整体移动到 `.obsidian/plugin-backups/`，该目录不在活动插件扫描范围；
- 同 ID 的其他目录先移出 `.obsidian/plugins/`，全部保留可恢复副本；
- staging 通过原子 rename 成为活动目录；安装后再次校验版本与 `main.js`；
- 任一步失败都恢复原活动目录，并留下不含私人内容的阶段码。

插件运行中直接覆盖并立即热重载存在失败后无法自救的风险。V1 实现应采用“下载与校验在插件内完成，
明确确认后由最小外部 helper 在 Obsidian 完全退出期间执行事务，随后重新打开同一 Vault”的模型。helper
只能接收固定 release manifest、固定 staging 目录和当前 Vault 标识，不能执行任意命令或访问任务内容。

## 6. 安全要求

- HTTPS、现有设备身份和 Vault 授权只是传输/访问边界，不能替代安装包完整性；
- V1 至少固定 SHA-256；进入 friend-ready preview 前必须增加离线可验证签名和内置公钥；
- release manifest schema、插件 ID、版本单调性、最低 Obsidian 版本和 ZIP 白名单条目全部 fail closed；
- 禁止降级，除非用户进入独立的“恢复旧版本”流程并再次确认；
- 日志只记录版本、阶段、大小、哈希前缀和错误码，不记录 Vault 路径、设备名、URL、令牌或内容；
- release store 写入只属于本机管理员发布工具，不复用普通 Worker bearer token。

## 7. 实施顺序与完成门

1. 固化 release manifest schema、路径隔离和 Worker 只读目录测试；（已完成）
2. 实现认证的 latest/archive 只读接口及生成类型；（已完成）
3. 插件实现检查、下载、校验和明确确认页面，不执行安装；（已完成）
4. 实现最小 helper、事务日志、退出后安装、重开和回滚；（已完成）
5. 用两个合成 Vault 覆盖正确 Vault、错误 Vault、旧备份重复 ID、损坏包、断网、空间不足、重启前中断、
   新版加载失败和手工恢复；（已完成合成自动化矩阵）
6. 在独立远程测试 Vault 完成一次 `N → N+1 → 回滚 N → 再升级 N+1`，并确认配对设置不变；
7. 通过后才把手工脚本从默认流程降为恢复工具。

在第 6 步通过前，不把“自动更新”展示为已经可用，也不替换当前经验证的手工安装恢复路径。设置页可以展示
当前受控入口，但必须明确说明确认、退出和加载校验三个不同阶段。

步骤 2 的实现额外固定：普通配对设备只有 GET 权限；latest/archive 均先认证；archive 每次响应前重新验证
不可变 store，返回强 ETag、SHA-256 与 private cache 头并支持 304；任何磁盘篡改都脱敏失败，不降级为
未经验证的下载。生产分发前仍需由宿主本机管理员显式导入 release，网络 API 不承担该职责。

宿主本机导入现由 `speech-capture-manager client-release-import` 承担：必须给出绝对 manifest 路径，导入前
完整验证相邻 ZIP 与 installer，使用同卷 staging 和原子 rename；相同版本同字节幂等，不同字节不可覆盖。
命令与 `client-release-status` 的 JSON 审计结果只含固定公开元数据、大小和哈希，不含本机路径或私人状态。

步骤 3 的实现额外固定：更新入口目前只出现在插件设置页；它复用当前 Worker 的
配对凭据，不保存 URL、令牌或候选包到插件设置。下载后独立验证响应 ETag/内容哈希、包大小、ZIP 中央目录与
本地条目、精确三文件白名单、CRC、内部 manifest 身份/版本/最低应用版本和 `main.js` 哈希。确认状态和候选
字节只保留在当前插件进程内，切换 Worker、移除 Worker、重新连接或卸载插件都会清除。该阶段不会写活动
插件目录、Vault 内容或 `data.json`，也不会把“已确认”显示成“更新完成”。

步骤 4 的实现额外固定：确认后的 ZIP、固定 schema 请求和只含版本/哈希/阶段码的状态文件先写入
`~/Library/Application Support/Speech Capture/Client Updates/` 私有事务目录。外部 helper 只接受该固定根、
规范 transaction ID、明确 Vault/config 作用域哈希和精确三文件 ZIP；Obsidian 完全退出前不修改活动插件。
替换时先把重复同 ID 目录及当前插件移到 Vault 内非扫描备份目录，再以同卷 rename 启用候选；任一步失败会
恢复当前插件和已迁移的重复目录。有效请求失败时会重开旧版 Vault；新插件加载后还必须由运行中的插件复核
磁盘 manifest、版本和 `main.js` 哈希，只有通过才写 `loaded_verified`。不同 Vault 的状态不能互相覆盖或
遮蔽。加载确认或最终失败后删除事务中的 ZIP、helper 和请求文件，只保留小型状态记录，避免更新暂存持续
占用空间。新版完全无法加载时，`recover-speech-capture.zsh` 要求明确 Vault 与明确备份目录名，并在 Obsidian
完全退出时把失败版本移入非扫描备份区、原子恢复所选旧版；它拒绝路径穿越、不安全文件和模糊选择，恢复
中断也会把原活动版本放回。该工具是最终兜底，不属于静默自动回滚。
