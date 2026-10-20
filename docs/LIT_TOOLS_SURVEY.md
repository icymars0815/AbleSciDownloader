# GitHub 文献检索/下载项目调研结论与采纳决定（2026-10-19）

调研范围：论文类 MCP（paper-search-mcp、arxiv-mcp-server、pubmed-mcp-server、zotero-mcp、openalex-mcp）、Zotero「Find Available PDF」与 translators、Unpaywall/oadoi、Open Access Button、pyalex、habanero、metapub、papis、JabRef、GROBID、pdf2doi。影子图书馆类项目已排除。

许可提醒：Zotero 及其 translators、papis、CERMINE 为 AGPL/GPL；pdf2doi 无许可证——**只借鉴思路，不复制代码**。部分条目（GROBID、pdf2doi、papis、JabRef、habanero、metapub）基于既有知识未读源码，实现前需再核实。

## 采纳（按优先级）

| # | 改进 | 工作量 | 模块 | 决定 |
|---|---|---|---|---|
| 1 | 浏览器兜底前先普通 GET OA 落地页，解析 `citation_pdf_url`、`link rel=alternate type=application/pdf`、meta refresh 作为新候选（仍限速、查挑战、校验 %PDF、不带 Cookie） | S–M | oa_fetch.py | 采纳 |
| 3 | 读 `Retry-After`，上限 10 s 内等一次；429/500/503 至多 1 次退避；仍受总时长 120 s 约束 | S–M | oa_fetch.py | 采纳 |
| 4 | URL 规范化去重（https、去追踪参数，保留 download 等白名单参数）；每源最多 6 个候选；重定向上限 10、同 URL 2 次 | S | oa_fetch.py | 采纳 |
| 5 | MCP 返回体积治理：默认紧凑、`max_chars`、`truncation`/`deferred`、失败返回 `reason` + `tried`；全文只存盘返回路径与核对结论 | S–M | mcp_server.py | 采纳（并入 MCP/CLI 任务） |
| 6 | 撤稿/更正标记（Crossref update-to/relation、OpenAlex is_retracted）；撤稿文献不自动判通过 | S | metadata.py、core.py、确认表 | 采纳 |
| 9a | 核对先读 PDF 内嵌 Info/XMP 的 DOI；`doi_value` 括号平衡（修复以 `)` 结尾的合法 DOI 被截断） | S | core.py | 采纳 |
| 10 | OA 查找结果按 DOI 磁盘缓存（命中 7 天 / 未命中 1 天，可清除）；确认表加「疑似重复」列与各源结果折叠 | M | metadata.py/oa_fetch.py、app.js | 采纳 |
| 2 | 礼貌池：全局「联系邮箱」用于 OpenAlex/Crossref `mailto` 与 Unpaywall，可选 OpenAlex key | S | oa_fetch.py、设置 | 采纳为**可选、默认关**（邮箱会发给第三方，需用户填写并同意）；同一开关启用 Unpaywall |
| 7 | CORE 作为仓库来源（可选 key，401/403 退回无 key） | M | oa_fetch.py | 采纳为可选；候选标注 version，必须核对 |
| 8 | 无 DOI 条目：Crossref `query.bibliographic` 给候选，只在确认表里作建议，用户确认后才采用 | M | paste_parser.py、确认表 | 采纳（仅建议） |
| 9b | GROBID 可选本机服务 | L | core.py | 暂缓（依赖重；扫描件已有 AI 可选路径） |

## 额外

- OA 位置的 `version`（published/accepted/submitted）写入确认表与核对结论，预印版/接受稿明确标注。
- 待实测：PMC `/pdf/` 是否对脚本请求设验证；若是，优先 Europe PMC render 与 PMC OA 服务。
- 当前做法中被确认为合理、保持不变：仅 CC 许可的 Crossref 链接、只认首页 DOI、不自动驳回非“文件不符”情况。
