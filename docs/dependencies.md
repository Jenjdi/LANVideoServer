# 依赖、版本与许可证

以下为当前环境实际使用或已准备的版本。项目自身代码使用 MIT；独立工具和动态库仍遵守各自许可证。未将系统浏览器、FFmpeg 或第三方动态库二进制放入 Git 仓库。发行前应随分发方式保留对应许可证和告示，不能仅引用本文件代替许可证原文。

|名称|当前版本|用途|许可证|
|---|---|---|---|
|GCC/G++|14.2.0|C++20 编译|GPL-3.0-or-later，运行库另有 GCC Runtime Library Exception|
|CMake|3.31.10|构建与 CTest|BSD-3-Clause|
|cpp-httplib|0.18.7，Debian 0.18.7-1+deb13u1|HTTP、受限线程队列、流式传输|MIT|
|SQLite|3.46.1|用户、会话、授权、记录、任务/分享状态与真实监控快照|Public Domain|
|libsodium|1.0.18|Argon2id、随机令牌、SHA256、proxy secretbox|ISC|
|nlohmann/json|3.11.3|JSON API 和结构化日志|MIT|
|OpenSSL|3.5.7|HTTP/BT TLS、分片SHA1再校验、真实远程入口证书/域名验证|Apache-2.0|
|libtorrent-rasterbar|2.0.11|真实 BT、Tracker/Peer、分片校验与代理|BSD-3-Clause|
|Boost|1.83.0|libtorrent 头文件依赖|BSL-1.0|
|FFmpeg/FFprobe|7.1.5，Debian 系统 GPL 构建|可选元数据探测、真实测试视频生成与解码|GPL-2.0-or-later（当前系统配置包含 GPL 功能）|
|Python|3.12.14|CLI 客户端、集成测试、工具 venv|PSF-2.0|
|Playwright Python|1.62.0|真实浏览器测试|Apache-2.0|
|pyee|13.0.1|Playwright 事件依赖|MIT|
|greenlet|3.5.6|Playwright 同步接口协程依赖|MIT AND PSF-2.0|
|typing-extensions|4.16.0|测试依赖的类型支持|PSF-2.0|
|Chromium|154.0.8037.57|本机原生 video 解码、播放和页面测试|BSD-3-Clause 及随附第三方组件许可证（仅使用系统安装）|

Python 测试依赖在 `tests/requirements.txt` 固定版本。Debian 开发包通过已验证签名的索引下载并校验后解包；安全补丁版本随 Debian 索引变化，未来刷新需重新运行构建与测试。安装脚本不使用 trusted-host、跳过校验或修改包校验和。
FFprobe 是外部可选进程，未静态链接进项目；缺失时仍提供文件名、大小、格式和 Range，时长显示未知。FFmpeg 与 Chromium 是测试所需工具，缺少时脚本会报告前置条件，不以跳过测试宣称全部通过。

|浏览器依赖|版本|用途|许可证|
|---|---|---|---|
|js-sha256|0.11.1|1 MiB 分块 SHA256 与增量完整文件校验；HTTP LAN 浏览器也可使用，不依赖 secure-context WebCrypto|MIT|

浏览器库源码随项目位于 web/sha256.min.js，许可证保留于 [js-sha256-LICENSE.txt](js-sha256-LICENSE.txt)。从 npm 的固定版本包通过 TLS 下载，并按 registry 的 SHA512 integrity 核对，记录在 [js-sha256-integrity.json](js-sha256-integrity.json)。运行时不请求 CDN，不依赖 Node。

|入口依赖|实际版本|用途|许可证|
|---|---|---|---|
|Nginx|1.26.3，Debian包1.26.3-3+deb13u7|独立可部署HTTPS反向代理、真实TLS集成测试|BSD-2-Clause（核心）；发行包另有组件告示|

Nginx/nginx-common同样通过Debian签名索引与包哈希验证后解包到sysroot，未启动系统服务、未修改/etc/nginx。运行示例生成独立私有配置；不将Nginx二进制放入Git。完整包许可证可查sysroot/usr/share/doc/nginx-common/copyright。TLS测试证书只在临时目录生成和信任；正式证书与续期由部署者提供。

阶段7监控使用C++标准库/POSIX statvfs，安装/归档与SHA256清单使用现有CMake及Python标准库，没有新增服务依赖。安装包不捆绑系统动态库、FFmpeg、Chromium或Nginx；目标部署需要对应ABI运行库。

交付附带当前验证包的完整版权告示与必要公共许可证文本，见[licenses目录](licenses/README.md)，包括编译进入应用的nlohmann/json与Boost头文件告示；这些告示不是第一方源码。

文档与发布工作流审阅另用 PyYAML 6.0.3（MIT）、python-docx 1.2.0（MIT）和 lxml 6.0.2（BSD-3-Clause）。这些工具只安装于临时审阅目录，不是服务运行依赖，也未放入发行包。
