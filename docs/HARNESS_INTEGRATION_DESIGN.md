# 外部 harness 兼容（Claude Code / Codex 等）（2026-10-19）

## 决定

- **主接口：MCP（stdio）**——已有 `mcp_server.py`（11 工具），Claude Code 与 Codex 均原生支持本机 stdio MCP，工具可被发现、参数结构化、可按工具授权。
- **辅接口：薄 CLI `kytdl.py`**——与 MCP 共用同一层本机 HTTP 调用（127.0.0.1 + 一次性 token），每个命令支持 `--json`；供不支持 MCP 的 harness、脚本与调试使用。不另写业务逻辑。
- 两者都要求 App 服务在运行（launcher 已可后台常驻）；服务未运行时给出明确提示（MCP 返回错误，CLI 退出码 2），不自动启动带窗口的 App。

## MCP 工具补齐（在现有 11 个基础上）

| 工具 | 说明 | 权限 |
|---|---|---|
| `oa_lookup` / `oa_fetch` | 查找/下载开放获取 PDF 并本地核对 | 本地操作，无需科研通授权 |
| `batch_status` | 批次进度（筛选/待确认/求助中/监控/完成）与消息面板事件 | 只读 |
| `review_table` | 确认表内容（哪些需要科研通求助、预计积分） | 只读 |
| `confirm_batch` | 确认部分条目进入科研通求助 | 需 App 内开启 MCP 科研通授权 **且** 参数 `confirmed=true` **且** 返回预计积分后由 harness 再次调用（两步：preview → confirm token） |
| `monitor_status` | 后台监控状态、下次同步时间、需要处理的条目 | 只读 |
| `events` | 最近事件（与右下角面板同源） | 只读 |
| `download_settings_get` | 读取下载判定与高速档位 | 只读；**不提供修改高速档位/自动发布/自动采纳/AI 开关的工具** |

- 保持原则：外部工具不能开启授权、高速、自动拒绝/采纳/发布、AI；不暴露密码、Cookie、API Key、任意路径、命令执行或任意 URL。
- 返回给 harness 的文本可能被其模型供应商处理，工具描述中注明。
- 修复 FastMCP/Pydantic 字段定义警告；协议测试覆盖新工具与权限拒绝路径。

## harness 配合下载（用户重点，2026-10-19）

外部 harness 需能驱动完整下载流程，而不仅是查询：

| 工具 | 说明 | 权限 |
|---|---|---|
| `import_references` | 导入 DOI/引用，自动建批次并开始免费源筛选 | 本地 |
| `batch_status` / `review_table` | 筛选进度与确认表 | 只读 |
| `confirm_batch`（两步） | preview 返回需求助条目与预计积分 + confirm_token；harness 必须把预览展示给用户后再带 token 调用 confirm | 需 App 内开启 MCP 科研通授权 |
| `download_paper` | 对已收到应助的条目启动科研通下载（按用户在 App 中设置的高速档位，harness 不能改档位） | 需 MCP 科研通授权 |
| `download_progress` | 进度、当前线路、速度、预计剩余时间 | 只读 |
| `wait_for` | 长轮询：等待指定条目/批次到达某状态（应助到达、下载完成、需要人工），带超时（≤10 分钟），返回期间事件 | 只读 |
| `get_result` | 返回 PDF 本地路径、原文件名、核对结论、撤稿标记；不返回 PDF 内容 | 只读 |

- CLI 对应：`kytdl wait <id> --until downloaded --timeout 600 --json`、`kytdl result <id> --json`；花积分/改网站状态的操作 CLI 仍不提供。
- 示例工作流写进 MCP_CONNECTION.md：harness 导入 → 等筛选 → 把确认表给用户看 → 用户同意后 confirm → wait_for 应助 → download_paper → wait_for 下载完成 → get_result。

## 被 harness 调用时：由 harness 自己的模型代替内置 AI（2026-10-20 评估）

Claude Code / Codex 自带模型，能读图、读文本、推理。内置 AI 模块（DeepSeek/自定义接口）只对**纯 GUI 用户**必要；经 MCP/CLI 调用时，凡是“识别/整理/解释”类工作都可交给 harness，本 App 只提供原料和校验，不做二次 AI 调用。

| 需要 AI 的环节 | GUI（无 harness） | harness 调用时 | App 提供的工具 |
|---|---|---|---|
| 智能粘贴解析失败的引用 | 内置 AI 整理（需开启） | harness 自己解析 → 提交结构化记录 | `unparsed_segments`（返回未识别段落）；`import_references(records=[{title,doi,authors}])` 走同一预览与校验 |
| 扫描件/版面异常的 PDF 识别 | 内置 AI 页图识别 | harness 直接读页图 | `render_pages(id, pages)` 返回本地 PNG 路径（不回传 base64，省上下文）；`submit_assist_result(id, fields, evidence)` 保存为“外部工具辅助结果” |
| 核对未通过的解释 | 无（人工看） | harness 读核对详情并向用户解释 | `get_result` 含逐项核对证据 |
| 错误报告分析 | 内置 AI 分析（需开启） | harness 读脱敏报告并分析 | `error_report(redacted=True)` |
| 确认表的说明/建议 | 规则生成 | harness 可补充解释给用户 | `review_table` |

保持不变的边界（无论谁做识别）：
- 外部模型结果与内置 AI 结果同等地位：**只作辅助，单独存放，不进入自动驳回/采纳/发布/扣费判定**；本地固定规则核对结论不被覆盖。
- harness 提交的记录仍经本地校验（DOI 格式、长度、去重、DOI 由 OpenAlex/Crossref 复核标题），不能借此绕过确认表。
- `render_pages` 只能渲染 App 已保存的 PDF 的前若干页，路径限定在数据目录内。
- 隐私：经 MCP 返回的内容会进入 harness 的模型供应商；工具描述与 App 设置页注明，PDF 页图只在 harness 主动请求时生成。
- 因此 harness 模式下**不需要用户在 App 内填写 AI Key**；App 内 AI 开关保持关闭也不影响 harness 工作流。

## CLI 命令

```
kytdl status [--json]
kytdl import <file|-> [--json]          # 只导入并触发免费源筛选，不发起求助
kytdl oa <DOI> [--download] [--json]
kytdl batch [<id>] [--json]             # 批次进度/确认表
kytdl events [-n 50] [--json]
kytdl monitor [on|off|status]
kytdl open [<id>]                       # 打开工作台窗口（可定位到某条目），查看下载进度
```

- MCP 工具 `open_panel(paper_id?)` 与 `kytdl open` 只打开本机界面（可定位到条目），不需要科研通授权；`workbench_status`、`download_progress`、`batch_status` 同时返回 `panel_url`，便于 harness 提示用户打开面板查看进度。
- 不提供 confirm/publish/adopt/reject 等花积分或改变网站状态的 CLI 命令（这些只在 App 内或经授权的 MCP 两步确认）。
- 退出码：0 成功，1 业务错误，2 服务未运行，3 权限不足。

## 验收

- 在 `<项目目录>`（应用所在文件夹）打开真实 Claude Code 会话加载 `.mcp.json`，调用只读工具（status、batch_status、oa_lookup 公开 DOI）验收；Codex 同理用 `.codex/config.toml`。两份配置均通过相对路径调用项目根目录的 `kytdl-mcp.cmd`，不含机器相关的绝对路径；全局配置需把 `<项目目录>` 替换为实际存放位置。
- CLI 在 PowerShell 与 Git Bash 下各跑一遍 `status --json`。
