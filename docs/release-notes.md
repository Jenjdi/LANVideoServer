# 轻量级视频共享与传输软件 V1.0

提供用户与角色管理、视频扫描与授权、HTTP Range 播放、本地上传与分块续传、v1 BT 下载与校验分片播放、HTTP/SOCKS5 出站代理、HTTPS 反向代理适配、限时视频分享，以及管理员状态和审计查询。

本次交付修复了本地模式 Origin 校验、重复 JSON 键、续传文件清理身份核验、数据库链接检查、SOCKS5 认证协商、HTTP 请求期限和 BT 首帧分片调度等问题。回归结果与审查范围见 `docs/security-review.md`。

附件包含 Linux x86_64 Release 应用包、独立软件著作权材料 ZIP 和 SHA256 校验文件。应用包需要 Debian 13 对应动态库，没有默认账号；首次使用执行 `lanvideo --init-admin`。源代码可直接通过 CMake 构建，操作步骤见 README。

默认监听回环 HTTP。公网部署使用已有网络入口和 HTTPS 反向代理；本版本没有 STUN/UDP 打洞，未完成公网跨 NAT 和公共 BT swarm 实测。本机 Tracker、Seeder、认证代理、Chromium 和 Nginx 测试覆盖实际下载及播放流程。
