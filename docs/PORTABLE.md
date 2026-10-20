# Windows 免安装版

从 [GitHub Releases](https://github.com/icymars0815/AbleSciDownloader/releases) 下载 `AbleSciDownloader-版本-windows-x64.zip`，完整解压到你有写入权限的目录，再双击“启动科研通下载器.cmd”。不要直接在压缩包内运行。

包内自带 Python 和锁定依赖；首次启动会从 Playwright 官方自动获取约 200 MB 浏览器到本目录，完成后自动打开 App。无需安装 Python、Chrome、输入命令或获取管理员权限。首次准备需要联网，之后不重复下载；失败时保留提示，检查网络后重新双击即可。登录、查询文献、下载也需联网。验证码和微信登录由用户自己完成。

数据继续保存在目录里的 `data/`。退出后台服务后可移动整个目录。保存的密码/AI Key 绑定当前 Windows 用户，换用户或电脑后需要重新输入；Cookie 也可能失效。

更新请解压到新目录，先退出旧版并自行备份，再迁移 `data/`；保留旧目录以便回退，不覆盖正在运行的版本。同一时间只运行一个版本。

本软件尚未购买代码签名证书，Windows 可能显示来源提示。请从官方仓库下载并核对 Release 附带 SHA256；不要关闭系统防护。当前仅提供 Windows 10/11 x64 包，ARM64/32 位与全新 Windows 虚拟机未验收。

MCP/命令行也可使用包内运行时：`kytdl.cmd` / `kytdl-mcp.cmd` 自动选用随包 Python。具体 MCP 绝对路径请从 App 的连接教程复制，随解压位置生成。

## 构建与依赖来源

开发者运行 `python packaging/build_portable.py --source-ref <公开提交> --output build/<新目录>`。文件白名单来自公开提交及明确新增发布文件；不会复制开发者 data/、虚拟环境、个人配置或历史交接。构建拒绝覆盖已存在输出目录。

Python 使用官方 3.13.16 Windows x64 嵌入式包，按官方 manifest 的 SHA256 校验；依赖按 requirements-lock.txt 下载 Windows CPython 3.13 wheels。浏览器由用户首次启动时通过随包 Playwright 1.64.0 的官方安装命令获取，不随 ZIP 再分发，不注册为系统浏览器、不自动更新。构建后运行 `python packaging/finalize_portable.py --folder <包目录> --output <新的发布目录> --source-ref <公开提交>`，自动准备浏览器、隔离验收，再排除浏览器二进制生成便携 ZIP、源码 ZIP 和 SHA256。保留 Python 许可及依赖 dist-info 中的许可文件。

参考：[Python Windows 分发](https://docs.python.org/3/using/windows.html#the-embeddable-package)、[Python 3.13.16 官方清单](https://www.python.org/ftp/python/3.13.16/windows-3.13.16.json)、[Chromium 官方下载说明](https://www.chromium.org/getting-involved/download-chromium/)、[Playwright 浏览器管理](https://playwright.dev/python/docs/browsers)。
