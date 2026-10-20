# 第三方许可说明

科研通下载器（内部代号 KYTDL）自身代码以 **GNU AGPL-3.0** 发布（见 LICENSE）。选择 AGPL 是因为核心依赖 PyMuPDF 采用 AGPL-3.0（或 Artifex 商业许可），这样整个项目在分发时与其完全兼容。

本项目不包含、不分发任何第三方文献 PDF；`data/`、`test_papers/` 已从版本库排除，示例 PDF 为程序生成的虚构内容。未复制 Zotero、papis 等 AGPL/GPL 项目的代码，只参考其公开描述的思路。

运行时依赖（通过 pip 安装，不随源码打包；许可来自 2026-10-19 已安装包的元数据）：

| 依赖 | 许可 |
|---|---|
| PyMuPDF | AGPL-3.0 或 Artifex 商业许可 |
| pystray | LGPL-3.0 |
| playwright | Apache-2.0 |
| mcp | MIT |
| Pillow | MIT-CMU (HPND) |
| pywin32 | PSF |
| olefile | BSD |
| certifi | MPL-2.0 |
| cryptography | Apache-2.0 或 BSD-3-Clause |
| httpx / httpcore / starlette / uvicorn / sse-starlette / click / idna / pycparser / python-dotenv | BSD-3-Clause |
| pydantic / pydantic-core / pydantic-settings / anyio / attrs / jsonschema / referencing / rpds-py / h11 / httpx-sse / pyee / PyJWT / six / annotated-types / typing-inspection | MIT |
| python-multipart | Apache-2.0 |
| greenlet | MIT 与 PSF-2.0 |
| typing_extensions | PSF-2.0 |

Playwright 使用本机已安装的 Google Chrome（channel="chrome"），不随项目分发浏览器。

第三方服务（OpenAlex、Crossref、Europe PMC、NCBI/PMC、Semantic Scholar、Unpaywall、arXiv、bioRxiv/medRxiv、CORE、科研通、DeepSeek）各有其使用条款，与本项目无隶属关系；所获取开放获取文献的再利用以各文献自身许可（如 CC BY）为准。

## 前端组件库

| 组件 | 版本 | 许可 | 位置 |
|---|---|---|---|
| Bootstrap | 5.3.3 | MIT | `static/vendor/bootstrap-5.3.3/`（`bootstrap.min.css`、`bootstrap.bundle.min.js`，取自官方发布的 dist 文件，未修改） |

Bootstrap：Copyright 2011-2024 The Bootstrap Authors。以 MIT 许可发布，许可全文见 <https://github.com/twbs/bootstrap/blob/main/LICENSE>，与本项目的 AGPL-3.0 兼容。文件随源码本地提供，运行时不访问任何 CDN。

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
