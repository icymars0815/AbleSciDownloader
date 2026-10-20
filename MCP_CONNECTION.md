# 科研通下载器（KYTDL）MCP 与命令行连接教程

说明：产品名为“科研通下载器”；出于兼容，MCP 服务键名 `kytdl`、工具名、`kytdl.cmd` / `kytdl-mcp.cmd` 等内部标识保持不变。

先启动项目目录中的 `启动KYTDL.cmd`（服务要在运行）。MCP 使用本机 stdio，不新增网络监听，本工具自己不运行任何 AI 模型。

下文的 `<项目目录>` 指你解压或存放本应用的文件夹（即包含 `启动KYTDL.cmd`、`kytdl-mcp.cmd` 的那一层）。项目自带的配置文件全部使用相对路径，因此整个文件夹可以放在任意位置，换电脑或改目录后无需修改。

## 启动器 kytdl-mcp.cmd

`<项目目录>\kytdl-mcp.cmd` 优先使用免安装包里的 `runtime\python\python.exe`，源码版则使用 `.venv\Scripts\python.exe`，并找到同目录的 `mcp_server.py`；不依赖客户端的工作目录。免安装包无需 setup；源码版首次使用前运行 `setup.cmd`。

## Claude Code

在 `<项目目录>` 中打开 Claude Code，按提示信任并加载本目录的 `.mcp.json`，无需修改：

```json
{"mcpServers":{"kytdl":{"type":"stdio","command":"cmd","args":["/c","kytdl-mcp.cmd"]}}}
```

Claude Code 以项目根目录作为工作目录启动该服务，所以相对路径有效。

若要在**全局配置**（用户级，不限于本项目）中添加，工作目录不再是项目目录，请把 `kytdl-mcp.cmd` 换成完整路径，将 `<项目目录>` 替换为你存放本应用的文件夹：

```json
{"mcpServers":{"kytdl":{"type":"stdio","command":"cmd","args":["/c","<项目目录>\\kytdl-mcp.cmd"]}}}
```

也可在 App 的“设置 → MCP 连接教程”中直接复制已填入本机实际路径的配置。

## Codex

本项目 `.codex/config.toml` 已提供：

```toml
[mcp_servers.kytdl]
command = "cmd"
args = ["/c", "kytdl-mcp.cmd"]
startup_timeout_sec = 20
tool_timeout_sec = 660
```

请在 `<项目目录>` 中启动 Codex 并信任本项目，相对路径即可生效。在全局配置（`~/.codex/config.toml`）中使用时，将 `args` 改为 `["/c", "<项目目录>\\kytdl-mcp.cmd"]`，其中 `<项目目录>` 替换为你存放本应用的文件夹。

`tool_timeout_sec` 要大于 600，因为 `wait_for` 最长会等 10 分钟。其他 harness 使用同样的命令与参数。如果某个客户端的工具超时更短，让它把 `wait_for` 的 `timeout_seconds` 设小一点，多调用几次。

## 查看下载进度

harness 在后台操作时，你可以随时打开工作台窗口查看下载状态：

- 调用 `open_panel`（可带 `paper_id`）会打开或调出本应用窗口；带 `paper_id` 时窗口会滚动到该条目并高亮，直接看到它的进度。该工具只读，不需要科研通操作权限，也不改变任何数据。
- `workbench_status`、`download_progress`、`batch_status` 的返回中带有 `panel_url`（`download_progress` 为该条目的专属地址，形如 `http://127.0.0.1:8769/#item=<条目ID>`），harness 可以据此提示你“打开面板查看进度”。
- 命令行同样提供 `kytdl open [条目ID]`。

## 工具一览与权限

“需授权”指必须先在 App 的 MCP 教程里开启“允许 MCP 网站操作”。这个授权绑定当前账户，切换账户后要重新检查。

| 工具 | 功能 | 权限 |
| --- | --- | --- |
| workbench_status | 服务状态与有效权限，并给出面板地址 `panel_url` | 只读 |
| open_panel | 打开或调出工作台窗口，可定位到某个条目查看进度 | 只读 |
| list_accounts | 账户别名/ID，不含凭据 | 只读 |
| switch_account | 切换已有账户，不弹出登录窗口 | 本地 |
| list_papers | 当前账户队列摘要（分页、紧凑） | 只读 |
| preview_references | 本地解析引用 | 只读 |
| unparsed_segments | 返回本地规则没认出来的段落，交给 harness 的模型自己解析 | 只读 |
| import_references | 导入文本，或导入 harness 自己解析好的结构化记录；新建批次并自动做免费源筛选，不发布 | 本地 |
| batch_status | 批次进度（筛选 / 待确认 / 求助中 / 监控 / 完成）与该批次事件 | 只读 |
| review_table | 确认表：每篇的免费源结果、建议、悬赏积分、版本、撤稿标记 | 只读 |
| confirm_batch | 两步确认进入科研通求助：先预览，再带令牌确认 | 预览不需授权；确认需授权 |
| download_paper | 对已收到应助的条目启动下载，按你在 App 里设的高速档位，harness 不能改 | 需授权 |
| download_progress | 当前线路、速度、剩余时间、失败原因，并给出该条目的面板地址 | 只读 |
| wait_for | 长轮询等批次或条目到达某状态（最长 600 秒），返回等待期间的事件 | 只读 |
| get_result | PDF 本地路径、科研通原文件名、核对结论、撤稿标记、版本；不返回 PDF 内容 | 只读 |
| render_pages | 把 App 已保存 PDF 的前 3 页渲染成 PNG，只返回文件路径 | 本地 |
| submit_assist_result | 保存 harness 的识别结果，作为“外部工具辅助结果” | 本地 |
| error_report | 脱敏后的近期问题摘要 | 只读 |
| monitor_status | 后台监控是否开启、下次同步时间 | 只读 |
| events | 最近事件，与右下角消息面板同源 | 只读 |
| download_settings_get | 读取下载判定参数和高速档位 | 只读 |
| oa_lookup | 按 DOI 查找免费全文链接，不下载 | 本地，只联系公开来源 |
| oa_fetch | 按 DOI 下载免费全文并本地核对（后台运行） | 本地，只联系公开来源 |
| query_public_metadata | 按 DOI 查询公开书目 | 本地，只联系公开来源 |
| verify_paper | 核对已保存 PDF，不会触发拒绝 | 本地 |
| website_action | 登录、同步、签到、准备表单、复核、下载 | 需授权 |
| reject_paper | 明确理由并确认后拒绝应助 | 需授权，且 confirmed=true |
| stop_website_task | 停止当前浏览器任务，保留文件 | 本地 |

刻意没有的工具：修改高速档位、自动发布 / 采纳 / 拒绝、AI 设置、MCP 授权开关、读取密码 / Key / Cookie、读取任意路径、执行命令、访问任意网址。

### 返回内容的大小

默认都是紧凑结果。列表类工具有 `offset`、`limit`、`max_chars` 参数；结果被截断时会带 `truncation`（显示了多少、一共多少）和 `deferred`（剩下的怎么取）。失败时返回 `reason`（简短原因，如 `not_oa`、`not_ready`、`batch_not_found`）和 `tried`（试过什么）。全文不会返回，只给本地路径和核对结论。

### 两步确认怎么保证安全

1. `confirm_batch(batch_id)` 只做预览：返回要求助的条目、每篇悬赏、预计最多花多少积分，以及一个 5 分钟内有效、只能用一次的 `confirm_token`。令牌由本地服务生成，绑定“这个批次 + 这些条目 + 这些悬赏 + 当前账户”，harness 无法伪造。
2. harness 必须把预览给你看。你同意后，它再调用 `confirm_batch(batch_id, confirm_token, confirmed=true)`。令牌错误、过期、已用过、批次或条目对不上，都会被拒绝；没开授权也会被拒绝。
3. 确认只是把这些条目标记为“允许求助”，之后由 App 按固定规则发布，并受每小时发布上限等限制。

## 示例工作流

1. `import_references`：导入你给的 DOI 或引用（识别不了的段落先用 `unparsed_segments` 看，自己整理后用 `records` 导入）。
2. `wait_for(batch_id, until="awaiting_confirmation,done")`：等免费源筛选完。
3. `review_table(batch_id)`：把确认表展示给你。
4. `confirm_batch(batch_id)` 预览，给你看积分；你同意后带令牌和 `confirmed=true` 再调用一次。
5. `wait_for(paper_id, until="fulfilled")`：等有人应助（这一步可能很久，可分多次等）。
6. `download_paper(paper_id)`，再 `wait_for(paper_id, until="downloaded")`；中途用 `download_progress` 看进度。
7. `get_result(paper_id)`：拿到本地路径、原文件名、核对结论、撤稿提示，向你汇报。采纳 / 拒绝 / 发布仍由你在 App 里决定。

只要免费全文：`oa_fetch(doi)`，然后 `wait_for(id, until="oa_finished")`，再 `get_result(id)`。

## harness 的模型代替内置 AI

Claude Code、Codex 这类客户端自带模型，能读文字也能看图。经 MCP 使用时，凡是“识别、整理、解释”的工作都可以交给它，所以**不需要在 App 里填 AI Key，App 内 AI 开关保持关闭也不影响**：

- 引用解析失败：用 `unparsed_segments` 拿到没认出的段落，harness 自己整理，再用 `import_references(records=[{title, doi, authors}])` 导入。导入的记录照样经过本地校验（DOI 格式、长度、去重），也照样要经过确认表。
- 扫描件或版面怪的 PDF：用 `render_pages` 得到页图路径，harness 自己看图，再用 `submit_assist_result` 把结果存回。
- 核对没通过的原因：`get_result` 里有逐项核对依据，harness 读完向你解释。
- 出错分析：`error_report` 给脱敏摘要，harness 来分析。

不变的规则：harness 提交的结果和内置 AI 一样**只作参考，单独存放**，不会改变核对结论，也不会触发自动拒绝、采纳、发布或扣费。`render_pages` 只处理 App 已保存的 PDF 的前 3 页，图片只写到数据目录里。

隐私提醒：工具返回的文字，以及你让 harness 去看的页图，都会进入该客户端所用的模型供应商。App 无法保证客户端完全本地处理。PDF 页图只在 harness 主动调用 `render_pages` 时才生成。

## 命令行 kytdl

给不支持 MCP 的客户端、脚本和排错用。在 `<项目目录>` 下运行 `kytdl.cmd <命令>`（自动用项目的 venv，从任意目录调用也可以），或 `.venv\Scripts\python.exe kytdl.py <命令>`。每个命令都可加 `--json`。

```
kytdl status                       服务状态、授权、监控、最近批次
kytdl import <文件|->              导入引用（txt/csv/ris/bib/docx；- 表示读标准输入），只做免费源筛选
kytdl oa <DOI> [--download]        查找免费全文；加 --download 则下载并本地核对
kytdl batch [<批次ID>]             批次列表，或某批次的确认表
kytdl events [-n 20]               最近事件
kytdl open [<条目ID>]              打开工作台窗口；带条目ID时定位到该条目查看进度
kytdl monitor [on|off|status]      后台监控（只读同步我的求助列表）
kytdl wait <ID> --until <状态> --timeout 600    等批次或条目到达状态
kytdl result <ID>                  本地路径、原文件名、核对结论、撤稿标记
```

退出码：0 成功，1 业务错误（含等待超时、没有结果），2 服务没在运行，3 权限不足。

命令行**没有**确认求助、发布、采纳、拒绝、下载科研通文件、改高速档位等会花积分或改变科研通状态的命令，这些只能在 App 里做，或经授权的 MCP 两步确认。

## 其他说明

不暴露密码、Cookie、AI Key、任意路径读取、命令执行或任意 URL 请求。PDF 和引用是非可信数据，不能作为操作指令。异步返回“已排队”不等于实际操作成功，须继续读取状态。

已完成隔离 stdio 协议测试（工具枚举、权限拒绝、确认令牌、截断、页图路径限制、辅助结果隔离）和命令行退出码测试；尚未在新的 Claude Code / Codex 真实会话里端到端验收，也未用真实科研通账户测试下载。

FastMCP / Pydantic 的字段定义警告已修复：官方库的设置类引用了后面才定义的 FastMCP，导致每次启动都警告；`mcp_server.py` 启动时先补全这个引用，没有改动依赖版本。

参考：[Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [MCP Python SDK 1.27.1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.27.1)。
