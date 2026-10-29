# v0.10.0 核查与发布记录

日期：2026-10-29（Asia/Shanghai）。基线为 GitHub main 的 Terra 提交 `fc9d493af355e891bb7e72e69392eb1afc36d9e5`。

## 发现与修复

- **后台操作暂停失效**：MCP 续办调度独立于全局自动流程，原来的停止接口仅终止当前浏览器命令，之后仍可能再次调度。现停止时同时暂停全局自动流程和当前账户的 MCP 续办；重启暂停所有账户的 MCP 续办。保留确认、悬赏和文件，允许主动恢复已确认的排队或已发布文献。
- **授权只在调度时检查**：原版排入浏览器队列后没有携带 MCP 授权上下文。现绑定账户和条目，在执行、限速等待后、发布/采纳/高速确认前再次检查；独立下载线程继承相同检查。关闭权限会暂停旧任务，重新开启权限不会自行恢复旧任务。已经发出的请求无法由本地停止撤回，结果不明仍须人工核实。
- **检索输入及异常响应**：多字节检索词经 URL 编码后可能超出 OpenAlex 4094 字节请求限制；现在请求前检查并提示缩短。对第三方响应中异常的嵌套字段做类型检查，防止检索接口因格式变化崩溃。
- **发布来源一致性**：原打包脚本把指定提交当作文件名清单，却复制当前工作目录内容；现在从指定 Git 提交直接提取字节，防止混入未提交改动。发布只使用公开仓库的文件。
- **依赖安全更新**：MCP SDK 从 1.27.1 升至 1.30.0；certifi 从 2026.10.18 升至 2026.10.23。锁定清单 38 个 PyPI 包重新查询 OSV，未返回已知漏洞匹配。此结果是当日数据库匹配，不是无漏洞保证。MCP 的若干公告涉及 HTTP/WebSocket/OAuth，而本程序生产 MCP 入口为 stdio，应区分依赖版本告警与实际可利用性。certifi 公告与 OSV 对旧版修复边界存在差异，更新至两者边界之后的版本。

## 验证范围

- 新增针对暂停、重启、权限撤销、账户绑定、下载线程授权、已发布任务恢复、检索边界和提交快照的回归测试。
- 测试使用隔离数据和模拟网站，不读取个人 profile、Cookie、密码或 AI 密钥。
- 本轮不执行真实论文下载、科研通发布、采纳/驳回、高速付费或 AI 请求；不把模拟结果当作真实站点验收。
- Windows 免安装版包含 Python 和锁定依赖；首次准备浏览器需要联网。未验收干净 Windows 虚拟机、ARM64、32 位或代码签名。

## 官方及数据库依据

- [OpenAlex 搜索与请求长度](https://help.openalex.org/api/searching/)
- [OpenAlex 分页](https://help.openalex.org/api/paging/)
- [PMC Cloud 数据结构](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)：只读核对元数据，未获取论文文件。
- [MCP SDK 安全公告](https://github.com/modelcontextprotocol/python-sdk/security/advisories)：1.30.0 修复相关 1.x 公告。
- [certifi 安全公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
- [OSV 查询接口](https://google.github.io/osv.dev/post-v1-querybatch/)
