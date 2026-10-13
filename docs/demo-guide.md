# 复试演示与技术答辩流程

## 演示准备

先运行构建及真实测试，准备自己有权使用且浏览器可解码的H.264 MP4。首次管理员由自己在终端隐藏输入创建，不用固定演示密码。使用两个浏览器账号演示管理员/普通用户隔离；连接应为本机/可信局域网，公网必须部署自己的HTTPS入口。复试现场无法连接公网时直接说明，只演示已验证的本机功能与真实BT夹具，不填造公网Tracker/跨NAT结果。

## 操作流程

1. 启动程序，执行内部健康请求，查看真实JSON status/version，再登录管理员；说明C++20/CMake、SQLite/WAL、Argon2id和无默认密码。
2. 上传真实视频、扫描列表、搜索/格式筛选；创建普通用户并授权一个视频。普通用户登录后只有被授权的资源，直接请求管理接口也被403拒绝。
3. 播放并拖动进度条，暂停后刷新继续。展示实际HTTP206与Content-Range，不把浏览器自然播放至目标时间当作成功seek。
4. 勾选分块续传，暂停或刷新后重新选相同文件，从已确认偏移继续；进度是确认字节。说明1MiB清单、SHA256、fsync/事务、冲突不覆盖。
5. BT导入v1种子、恢复、暂停、重校验；速度、连接Peer与已验证片来自库。现场无可信swarm可运行下方真实本机seeder/Tracker实验，再展示记录。完成后仍可能上传，暂停控制任务。
6. 边下边播展示真实503等待、已校验片206、下载未完成时的首帧与拖动。分片映射 `floor((file_offset+range_byte)/piece_length)`，读取整个片再次SHA1、传输不可变快照；缺少片不读取预分配字节。优先级周期2秒焦点/1秒普通保留调度机会，不能承诺网络不足时无缓冲。
7. 展示BT出站代理握手/超时及HTTPS入站入口的区别。HTTPS分享只有一个完整视频、期限/撤销；匿名接收者不能获取媒体库/用户/BT任务，撤销后404。公网/跨NAT未验证，不把配置文件或公网IP说成穿透已完成。
8. 管理页刷新真实状态与审计，退出后普通账号不能读取。展示交付包SHA256、安装迁移测试、功能状态及未验证项。

## 自动化真实演示命令

```sh
cd /workspace/LANVideoServer
# 已先完成构建；这些命令在临时目录运行真实服务并清理。
/workspace/toolchain/venv/bin/python tests/browser_integration.py build-release/lanvideo
/workspace/toolchain/venv/bin/python tests/bt_integration.py build-release/lanvideo build-release/bt_fixture
/workspace/toolchain/venv/bin/python tests/browser_bt_stream.py build-release/lanvideo build-release/bt_fixture
/workspace/toolchain/venv/bin/python tests/browser_remote.py build-release/lanvideo
```

BT实验包括独立libtorrent seeder、compact HTTP Tracker、实际转发TCP字节的HTTP/SOCKS5代理；不是假进度。浏览器夹具随机凭据、临时证书，结束停止全部测试进程，不用其随机密码替换实际管理员。截图与原始JSON在build-release/Testing/artifacts中；每次生成新记录，不能把旧截图当当前演示正在运行。

## 可讲清楚的取舍

- 自己实现服务端授权、Range选择、路径约束、会话/上传状态和代理测试；成熟libtorrent处理Tracker/Peer/分片协议，不把调用库隐去冒充自研BT协议栈。
- 普通媒体保留文件FD并按块复验；BT数据按完整片哈希形成快照。响应头发送后的失败只能中断连接，已发送/已缓存字节无法撤回。
- 视频时间不是直接线性映射文件字节；浏览器解析容器索引并发Range，服务按真实字节映射种子分片。不宣称所有视频容器适合边下边播。
- 原始令牌不入DB/日志；分享片段通过POST交换scoped cookie，读取权意味着接收者能保存字节，没有伪造禁止下载/DRM能力。
- 当前反向代理是同机HTTPS终止，外部可达依赖真实网络。跨NAT需要另外的可达入口/认证隧道或中继并实测，STUN/UDP协调流程未实现。

答辩请使用 [testing.md](testing.md) 的实际运行数据。单机夹具限速和小样本不是公网性能指标，也没有虚构开发经历、用户规模或软著登记成功。
