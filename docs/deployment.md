# 部署与安装

## 环境与开发构建

已验证Linux/Debian13 x86_64、GCC14.2、C++20、CMake3.31.10；版本/许可证见dependencies.md。不宣称Windows/macOS或其他发行版ABI兼容。SQLite、cpp-httplib、libsodium、libtorrent/Boost、OpenSSL为必要编译依赖；FFprobe是可选运行探测工具，FFmpeg/Chromium/Playwright/Nginx是实际集成测试所需。运行服务本身不要求Node、浏览器测试框架或公网BT资源。

本云工作区无需系统写权限：

```sh
cd /workspace/LANVideoServer
bash scripts/setup.sh
# 已有依赖时：
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug \
  -DPython3_EXECUTABLE="$(command -v python3)"
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

setup.sh使用Debian签名索引/包校验和与TLS，解包到/workspace/toolchain/sysroot，固定Python依赖到独立venv；不会写/etc/nginx或启动系统服务。保留有效构建，不清空用户数据或Git目录。普通Debian开发机可通过系统开发包并用 `-DLOCAL_DEPS=/usr` 替换前缀；若Python测试依赖不同，指定解释器。`BUILD_TESTING=OFF`可仅构建服务，但不是通过测试证明。

## Release和安装包

```sh
cd /workspace/LANVideoServer
cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE="$(command -v python3)"
cmake --build build-release -j2
ctest --test-dir build-release --output-on-failure
python3 scripts/package.py --build build-release --output build/artifacts
```

输出文件名包含版本、平台及内容指纹；既有输出拒绝覆盖。包内：bin/lanvideo、share/lanvideo/web、命令行工具、用户/技术/许可证说明、第一方源码清单、BUILDINFO.json、SHA256SUMS。包含完整第一方实现来源；不包含运行数据库、var、密钥、种子、下载视频、测试可执行文件或第三方系统动态库。压缩和成员时间归一化以便内容可重复，不代表研发日期。源码清单没有第三方js-sha256实现，浏览器分发仍保留其MIT许可证。

这是**需要外部动态库的Linux应用包**，不是免依赖静态包。目标Debian13需具有对应ABI的libsqlite3-0、libcpp-httplib0.18、libsodium23、libtorrent-rasterbar2.0t64及OpenSSL/libstdc++/压缩库等系统运行依赖。安装系统包由部署者执行；未把系统库/FFmpeg/Nginx二进制改称项目MIT。源构建是更换发行版的推荐方式；Debian安全补丁更新后重新运行测试。

安装包校验/迁移实测工具：

```sh
# 替换为package.py返回的实际文件。
python3 tests/package_smoke.py \
  build/artifacts/ACTUAL_RELEASE_FILENAME.tar.gz \
  --library-path /workspace/toolchain/sysroot/usr/lib/x86_64-linux-gnu
```

此云环境的库采用rootless私有sysroot，因此安装测试明确通过LD_LIBRARY_PATH提供已验证的外部库；不要求裸系统上无需依赖。目标机装好系统库后可省略--library-path。工具检查逐文件清单、无私有状态、移动目录后真实启动、网页资源、匿名401、无默认管理员和SIGTERM正常关闭，不用测试进程充当正式部署。

解包后在包根目录检查 `sha256sum -c SHA256SUMS`，按自己的路径设置：

```sh
export LANVIDEO_DATA=/absolute/private/runtime-directory
# 使用此云环境私有库时才需要：
export LD_LIBRARY_PATH=/workspace/toolchain/sysroot/usr/lib/x86_64-linux-gnu
bin/lanvideo --init-admin
bin/lanvideo
```

目录逐级创建并检查符号链接，服务账号拥有的私有目录设置0700；新文件默认0600，密码隐藏输入，没有默认账号。不要把运行数据目录放到公开静态目录。程序从可执行文件的同级前缀share/lanvideo/web读取网页资源，不依赖启动cwd；开发构建回退到源码web目录。可用绝对 `LANVIDEO_WEB_ROOT` 覆盖；启动时验证所有固定资源，拒绝缺失/符号链接资源。`LANVIDEO_DATA`未设置时仍为cwd下var，使用服务监管器时应明确设绝对路径。

## 云环境构建

云工作区的 CMake 与 Python 位于 `/workspace/toolchain/venv/bin/`，开发包位于 `/workspace/toolchain/sysroot/usr`。可使用 `CMAKE=/workspace/toolchain/venv/bin/cmake PYTHON=/workspace/toolchain/venv/bin/python bash scripts/build.sh` 自动选择该依赖前缀。普通开发机默认使用 `/usr`。

## 配置和进程监管

完整默认配置及范围见.env.example；程序不执行.env脚本。必要项：bind/port/data、日志info|error、会话30..86400秒、上传1..4096MiB、可选FFprobe绝对路径。CLI初始化与服务必须使用同一数据目录。开发程序 `bash scripts/run.sh` 前台运行；Ctrl+C或SIGTERM正常关闭，等待BT resume保存及网络/磁盘线程停止。

[deploy/lanvideo.service.example](../deploy/lanvideo.service.example)是systemd模板，要求部署者先建立运行账号、指定目录与环境文件。模板未在云工作区部署或启动，不把配置文件写好当作已经完成系统服务验证。切换到Release之前停止旧实例，保留数据，启动后验证JSON健康与受保护接口。不要向不属于本应用的进程发送信号。

默认只有回环HTTP。可信局域网需显式设置合适IPv4 bind，并确认主机防火墙与浏览器可达；本版本没有跨设备可达实测。公网用同机Nginx HTTPS，后端继续127.0.0.1，配置生成/证书/双进程监管/失败回退见remote.md。不会自动开防火墙、申请证书、做端口映射、发布域名或STUN打洞。Nginx与BT出站代理相互独立。

## 备份、恢复和升级

可迁移安装目录不等于自动迁移数据路径；SQLite保存共享根目录、下载根目录和上传路径等绝对路径。优先保留相同目录布局/挂载点，改变路径需按应用配置规则重建索引或迁移相应文件并验证，不能仅移动DB就宣称BT/续传可恢复。

建议一致的停机备份：暂停BT，SIGTERM停止服务并等待退出，使用原目录权限拷贝**整个运行数据目录**（DB及WAL/SHM若仍存在、proxy.key、ingress.key、bt-meta、默认共享/下载等），另外备份配置指向的外部媒体目录、BT下载目录及共享目录内隐藏续传文件。保留0600/0700权限。启动脚本、实际TLS证书/私钥与运维环境文件按运营者的密钥管理方案备份，不能混入公开发布包。

SQLite在线备份API只备份数据库，不同时保护动态文件、代理密钥、种子元数据或正在写入的数据分片；不把单份DB快照当作全部系统可恢复证明。丢失proxy.key无法恢复已加密凭据，丢失ingress.key需重新生成入口配置。当前没有自动备份/迁移工具或完整灾难恢复实测。

升级先保留旧二进制和完整备份，停止服务，安装新前缀，使用相同运行目录启动并检查健康/登录/媒体/BT校验/分享。schema当前采用幂等CREATE TABLE，未建立通用版本迁移框架；不能盲目回滚到不理解新表/策略的旧版本。强制SIGKILL/断电仍可能留下暂存或未保存resume，重启通过确认偏移/重新校验恢复已知数据，不承诺零数据损失。

日志收集、磁盘空间、审计和超时边界见monitoring.md。公开部署与备份演练仍由运营者在实际部署网络验证。
