# 科研通下载器（KYTDL）免责声明与使用须知

**声明版本：2026-10-21**
使用本软件即表示你已阅读并理解以下内容。如不同意，请停止使用。

**1. 软件性质：第三方、免费、开源**
科研通下载器（AbleSciDownloader，内部代号 KYTDL）为第三方个人开源工具，并非科研通（ablesci.com）官方程序，与科研通及其运营方无任何隶属、合作或授权关系。
本软件免费开源（GNU AGPL-3.0），仅供学习交流使用。如果你是付费购买的，则属受骗。未经作者许可，不得将本软件打包后收费销售或用于其他商业目的。

**2. 文献获取方式与用途限制**
获取文献时，本软件**先查询合法的免费开放获取（OA）来源**（如 OpenAlex、Europe PMC、PubMed Central、Unpaywall、arXiv 等），能直接下载的即直接下载。
仅当上述来源查询不到时，本软件才使用你本人的科研通账户，在科研通网站发布求助，并通过**科研通提供的下载渠道**下载应助文件。所有科研通操作均以你本人账户进行，须遵守科研通规则，积分消耗由你自行承担。
本软件不使用 Sci-Hub 等影子图书馆，尊重版权。本软件仅供个人学习与科研使用，请勿用于批量牟利、代他人求助、转售文献或任何违法目的。

**3. 你的合规责任**
你应自行遵守：
- 所在地著作权法及相关法律法规；
- 出版商及数据库的许可条款；
- 所在机构（学校、单位、图书馆）的规定；
- 科研通（ablesci.com）的全部规则，包括但不限于：
  - 仅用本人注册的账号，不代他人求助，不转移积分，不使用多个账号；
  - 站点文件仅供个人学习，不得修改、再分发、牟利；本软件保存文件时保留站点原始文件名，不修改原件；
  - 站点声明求助完成后文件约 24 小时自动删除，请及时处理；已下载的本地副本由你自行负责；
  - 驳回只能依据站点允许的理由；滥用驳回、虚假理由、扰乱求助秩序可能被扣分、封号；
  - 规则以科研通页面实时内容为准，随时可能变化。本软件文档中的规则摘要仅为某一时间点的阅读结果，不构成官方说明。

**4. 与第三方无关联、无背书**
本软件是独立的第三方个人工具，与科研通、OpenAlex、Europe PMC、PubMed Central、Semantic Scholar、Unpaywall、arXiv、bioRxiv、Crossref、DeepSeek 及任何出版商均无隶属、合作或授权关系，也未获其认可。相关名称与商标归各自权利人所有，仅用于说明兼容或数据来源。

**5. 开放获取并不等于可随意使用**
OA 文献的许可各不相同（如 CC BY 允许署名后再利用，其他许可可能限制商用、改编或再分发）。下载前后请自行查看该文献的许可。本软件显示的"开放获取"状态来自第三方数据，可能不完整或有误。

**6. 自动化风险**
本软件提供同步、轮询、签到、自动发布、自动下载、自动采纳/驳回等自动化功能。站点规则、页面结构、限制和风控可能随时变化，自动化可能因此失效、误判或触发站点限制。是否启用自动功能由你自己决定。建议保持各类确认步骤开启，尤其是自动驳回和自动采纳；这些操作可能导致扣分、封号或无法撤销的结果。本软件不保证任何自动流程在无人值守下正确运行。

**7. 无担保与责任限制**
本软件按"现状"提供，不作任何明示或默示担保，包括可用性、准确性、适销性或特定用途适用性。在法律允许的最大范围内，作者及贡献者不对因使用或无法使用本软件造成的任何损失负责，包括但不限于：账号被限制或封禁、积分被扣除或损失、文件被误判或被错误驳回/采纳、下载失败或重复扣分、数据丢失、与第三方的纠纷。本软件的 PDF 核对结果仅为本地启发式判断，不能替代你的人工确认。

**8. AI 功能**
AI 功能默认关闭，需要你自行提供接口与密钥（DeepSeek 或兼容 OpenAI 接口的自定义服务）。AI 输出可能出错，只作人工辅助，不参与自动驳回、采纳或发布判断。使用 AI 产生的费用、额度、内容安全与合规问题由你与服务商之间处理。开启后向所配置的服务发送的内容，受该服务商条款与隐私政策约束；开启云端 AI 后不能称为完全离线。

**9. 隐私与数据**
- 本地处理 · 不增加积分 · 不上传隐私。
- 默认的解析与 PDF 核对在本机完成。
- 会联网的情况：登录/同步/签到/下载访问科研通；公开书目查询向 OpenAlex、Crossref、Europe PMC、Semantic Scholar、Unpaywall、arXiv、bioRxiv 等发送 DOI 或检索词；开启 AI 并经你逐次预览同意后，向你配置的 AI 服务发送选定页图或文本片段。
- 不发送整本 PDF、本地路径、Cookie、凭据或任务队列给 AI。
- 账户会话、文件和记录保存在本机 `data/` 目录，请勿公开分享。密码可选用 Windows DPAPI 加密保存，密文绑定当前 Windows 用户；AI 密钥可选仅保存在内存或用 DPAPI 保存。
- 通过 MCP/CLI 连接的外部智能体如何处理数据，取决于该客户端，本软件无法控制。

**10. 开源许可证**
本软件以 GNU AGPL-3.0 发布（见 LICENSE）。选择 AGPL 是因为核心依赖 PyMuPDF 采用 AGPL-3.0；第三方依赖许可见 THIRD_PARTY_NOTICES.md。

**11. 反馈与联系**
问题与建议请到仓库提交 Issue：见项目仓库（地址待填写）。版权方或站点方如认为本软件的某项功能不当，请通过上述方式联系，作者会及时评估并处理。提交问题时请去除 Cookie、密码、密钥和个人信息。

**12. 声明的变更**
本声明可能随版本更新而修改，以最新版本为准。作者保留在发现违规用途时停止维护或分发的权利。


---

## English summary

**AbleSciDownloader (科研通下载器, internal codename KYTDL) Disclaimer.** This is a third-party personal open-source tool. It is NOT an official ablesci.com (科研通) program and has no affiliation, partnership or authorisation from ablesci.com or its operators. It is free and open source (GNU AGPL-3.0), for personal study and research only; if you paid for it, you were scammed. To obtain a paper it first queries legal free open-access sources (OpenAlex, Europe PMC, PubMed Central, Unpaywall, arXiv, etc.) and downloads directly when possible. Only if nothing is found does it use your own ablesci.com account to post a request on the site and download the answered file through ablesci.com's own download channel; all ablesci.com actions are performed under your account, must follow ablesci.com's rules, and any points spent are yours. It does not use Sci-Hub or other shadow libraries and respects copyright. You are solely responsible for complying with copyright law, publisher licences, institutional policies and ablesci.com's rules. Open-access licences vary; check each licence before reuse. Site rules can change and automation may misbehave; keep confirmations on. The software is provided "as is" without warranty; the authors are not liable for account restrictions, point deductions, wrong rejections or adoptions, failed downloads or data loss. AI features are off by default, use your own key and costs, and may be wrong. Processing is local by default; DOIs/queries go to public bibliographic services, your site actions go to ablesci.com, and, only with your per-use consent, selected page images or text go to the AI service you configure. Data stays under `data/` on your machine. Repository: to be filled in.
