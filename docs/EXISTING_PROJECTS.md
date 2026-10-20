# 已有科研通相关开源项目对比（2026-10-19，gh 搜索 + README/文档只读）

| 项目 | 形态 | 许可 | 功能 | 与 KYTDL 的关系 |
|---|---|---|---|---|
| daitcl/ablesciSign（58★）及多个 fork、youkq95/ablesci、Walvez/ablesci-auto-checkin、594233/ablesci_AutoSign | 签到脚本 / Actions / Workers / ScriptCat | 多为无许可或 MIT | 只做每日签到 | 不重叠；见下方签到风险 |
| drqux/ablesci_auto_download（8★，2025-03） | Python + Selenium 脚本 | **无许可** | 输入 DOI/PMID 批量求助并下载；手工粘 Cookie；**多线程同时下载多个**；单篇最长等 8 小时 | 无 OA 筛选、无核对、无确认；多线程会加重网站负担 |
| hiean-slh/ablesci-download（2026-08） | WorkBuddy AI Skill（Node + CDP 浏览器） | **无许可** | DOI 发布求助、每 60 秒监控最多 2 小时、高速下载、PDF 校验题目/作者/DOI、**自动采纳或自动驳回**、**下载后重命名移动** | 最接近；但自动驳回无官方 4 类理由约束、重命名违反官方“不得修改文件（含文件名）”；无 OA、无多账户 GUI |
| ZMoe2024/ablesci-toolkit（2026-09） | Python HTTP CLI，Windows DPAPI | **无许可** | 签到核对、本人求助普通线路下载（已真实验证）、单篇应助上传、线路负载与 Range 诊断、多源分片实验；发布接口已实测但无 CLI | 技术参考价值最高（见下）；无 GUI、无 OA、无流程 |
| TYLeavome/ablesci-skills | Claude 类 Skill | 无许可 | 签到；下载 PDF：先本地脚本，失败再科研通求助 | 与我们“先免费源再科研通”思路相同，但很简略；把邮箱密码写进 shell 配置 |
| LucasLin21/ablesci-assist-guard | Chrome 插件 | MIT | 面向**应助者**（刷新求助列表、抓 DOI、打开出版商页） | 方向相反，不重叠 |
| hoanganhduc/getscipapers | Python 包 | GPL-3.0 | 多来源（含影子图书馆）获取与求助，含 ablesci 模块 | 含影子图书馆来源，不参考 |

因上述项目大多**没有许可证**（默认保留全部权利），KYTDL 不复制其代码；仅把其 README/文档中陈述的**事实**作为待核实线索。

## 需要核实/采纳的线索

1. **签到自动化风险**：ZMoe2024 README 称“网站当前签到说明明确不允许程序自动签到并提示积分清零风险”。我们已保存的官网页面中未找到该原文（今天已签到，签到弹窗不再显示）。→ 下次签到前只读查看签到弹窗原文核实。处理：App 内“每日签到”按钮仅在用户手动点击时执行（保持手动，不做定时）。
2. **Range（断点续传）**：ZMoe2024 文档称“普通节点实际支持 HTTP Range，非零偏移和同令牌重复请求均已验证”，与用户记忆（不支持）相反。→ 下次真实下载时只读观察核实（IMPROVEMENT_PLAN D1）；用户裁定：属实就做断点续传。
3. **自动确认时间**：该文档称页面“自动确认时间存在 24/48 小时文案不一致”。→ 与“站点会自动处理应助”一致；以条目实际状态为准，提醒用户及时处理。
4. **接口线索**（仅作适配器失效时排查参考，App 仍走页面 UI 流程）：采纳 `button[data-type="accept"]` → 两个确认弹窗 → `POST /assist/file-handle`（type=accept/reject）；高速下载 `POST /file/request-download-token`（channel=highspeed）；详情页文件 ID 在 `.assist-file-id`。

## KYTDL 的差异化

本地 GUI + 多账户；先合法免费源筛选再由用户确认表决定求助；DOI/标题/作者/摘要本地核对 + 撤稿标记；按官方规则约束驳回（自动只限文件不符）、保留原文件名、24 小时删除时间；<500 闸门；常驻监控 + 通知；串行与限速；MCP/CLI 给 harness 用；AGPL-3.0 明确许可。
