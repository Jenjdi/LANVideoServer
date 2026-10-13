# 测试依据与重复执行

## 交付检查

先完成构建，再运行测试，避免正在执行的二进制被重链接。正常全量命令：

```sh
cd /workspace/LANVideoServer
/workspace/toolchain/venv/bin/cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE=/workspace/toolchain/venv/bin/python
/workspace/toolchain/venv/bin/cmake --build build-release -j2
/workspace/toolchain/venv/bin/ctest --test-dir build-release --output-on-failure
```

当前配置包含15个runner；修复后的完整结果记录于 [review-test-results.json](review-test-results.json)，安全检查范围见 [security-review.md](security-review.md)。原有阶段记录保留作历史参考。CMake的BUILD_TESTING=OFF仅构建服务，不会被表述为全部测试通过。

|runner|用例数|实际验证|
|---|---:|---|
|integration|8|配置/SQLite/健康/超限/并发/启动停止|
|auth_integration|23|密码哈希/盐、初始化、认证、会话、角色/CSRF、隐藏输入客户端|
|media_integration|26|真实扫描、媒体授权、Range逐字节/文件身份、上传/取消/输入边界|
|browser_integration|5|原生解码/seek/记录、上传/续传、错误、管理员监控与审计纯文本|
|uploads_integration|7|真实分块中断、重启继续、重复偏移/校验/路径/权限|
|bt_integration|10|真实Seeder/Tracker/代理、下载字节/分片、暂停/重启、损坏重校验、元数据路径|
|browser_bt|1|真实BT任务界面、状态、控制|
|bt_stream_integration|10|部分下载/跨片与跨文件、503/416/二次哈希、权限/焦点、并发/移除/关闭|
|browser_bt_stream|1|90秒无填充H.264、128KiB/s真实Seeder、下载未完成时首帧、35秒seek/持久化|
|shares_integration|10|单资源权限、令牌哈希、Range、真实30秒到期、撤销正在传输数据、账号/路径变化|
|remote_integration|13|真实Nginx TLS、链/主机名正负、入口头、错误服务/重定向、停机恢复、配置边界|
|browser_remote|1|真实HTTPS管理签发、专用cookie、解码/8秒seek、撤销后404|
|monitor_integration|8|真实DB/时钟/空间、管理员边界、分页/并发/重启、敏感值和参数验证|
|security_regression|15|跨站与重复输入、目录/暂存文件替换、代理协商、慢速请求和停机|
|delivery_integration|3|真实安装并移动前缀、资产识别、启动/重启、缺失/符号链接拒绝、无私有状态|

Release归档后另用package_smoke.py检查清单、包内容、迁移启动、匿名拒绝和正常关闭；是独立功能检查，不偷计为上述runner的附加用例。

## 真实夹具与证据

服务、SQLite、上传连接、FFmpeg生成的视频、libtorrent Seeder/下载端、Tracker/代理TCP字节、Nginx/TLS、Chromium均实际运行。BT上传限速是测试的实际网络整形，不是界面模拟；完成率依据库校验片，逐字节与源码文件核对。分片损坏测试实际修改临时文件，再通过校验与重下载验证。

测试不写实际var账号；密码/令牌运行时生成且不输出，结束清理临时进程和目录。认证部分到期边界在隔离数据库注入过期值，分享另有真正等待30秒的用例；监控浏览器测试注入带HTML的审计测试行验证textContent，无生产注入API，不冒充真实业务事件。网络连接与BT数据未模拟。

浏览器当前版本与视频SHA256、206/503、loadedmetadata/playing/seeked/waiting/stalled、帧数及单次耗时记录在各JSON。同一算法、不同视频长度/编码、机器负载、Peer排队与限速都会影响结果，不能横向拼成吞吐基准。历史阶段5的45秒视频记录保留；阶段6发现短文件可能在首帧前下载完，保持progress<1断言/45秒超时/128KiB/s限速，使用90秒真实视频，而不是降低断言或添加填充数据。

Chromium沙箱helper在容器不可用，仅对生成的本机夹具关闭并拦截外部请求；未更改正式部署。HTTPS浏览器仅信任当次叶证书SPKI；没有blanket ignoreHTTPSErrors，C++/Python始终开启证书链/主机名校验并有失败测试。临时证书不是公网证书与跨NAT证明。

## 结果文件

- build-release/Testing/Temporary/LastTest.log：本次runner stdout与退出结果；每次CTest会更新，必须匹配最新执行日期。
- build-release/Testing/artifacts：当前视频/BT/HTTPS/管理页截图与原始JSON；截图不能当作后端仍运行的证明。
- docs/stage7-test-results.json：交付构建/测试/归档/健康/安全检查的实际汇总。
- docs/stage7-*-evidence.json：从当前Release执行复制的原始观测，历史阶段4/5/6文件保留。

失败需保留原因、修复和相应runner复测；如果分次覆盖，明确标为合并覆盖而非单次全量成功。网络缺失/未运行/跳过不得计为通过。未开展的公网、跨NAT、生产压力与独立安全验证见feature-status.md。
