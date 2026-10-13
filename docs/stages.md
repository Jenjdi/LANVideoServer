# 阶段报告

## 阶段 0：环境检查与方案（2026-10-12，已完成）

1. **完成内容**：检查 `/workspace/LANVideoServer` 和 Git 状态，确认仅有 unborn `work` 分支、无源码、无既有用户文件；未覆盖既有内容。检查系统、编译器、CMake、SQLite、OpenSSL、FFmpeg、BT 开发库及内存。
2. **关键设计和原因**：C++20 + 原生 Web，SQLite 本地持久化；libsodium 负责成熟密码哈希；libtorrent 接管真实 BT。HTTP 使用 cpp-httplib，避免手写不完整的 HTTP 解析器。远程使用可替换 HTTPS 反向代理适配。版本、许可证、架构、表/API 草案与风险均在 `architecture.md`，草案不视为实现。
3. **新增文件**：`docs/architecture.md`、`LICENSE`，同时为阶段 1 准备构建和配置骨架。
4. **实际检查命令**：`git status --short --untracked-files=all`、`find /workspace -name AGENTS.md`、`uname -a`、`cat /etc/os-release`、`g++ --version`、`pkg-config --modversion openssl`、`ffmpeg -version`、`free -h`；对开发头文件和工具使用 `command -v`/`ls`。
5. **结果**：Debian 13.6、GCC 14.2、OpenSSL 3.5.7、FFmpeg 7.1.5；系统起初没有 CMake、SQLite/BT 等开发头文件。普通 apt 系统安装失败，原因是系统目录不可写。已改为使用工作区内 apt 状态目录、Debian 签名索引及 `.deb` 解包，保留校验；CMake 安装到独立 venv。
6. **限制**：当前平台以 Debian 13 x86_64 为目标；未证明公网 BT、浏览器视频播放、跨 NAT 或 HTTPS 部署。未创建 Git commit，所有代码仍是可审查的新增工作区文件。
7. **下一阶段**：立即实现并验证最小可运行骨架。

## 阶段 1：最小可运行骨架（2026-10-12，已完成）

1. **完成内容**：CMake C++20 项目；环境变量配置与输入校验；支持 `info`/`error` 的 JSON 行日志；SQLite WAL、外键、预编译参数及锁；健康接口；有界工作队列、请求体和超时限制；统一 JSON 错误；SIGINT/SIGTERM 关闭；工作区安装、启动和集成测试脚本。
2. **关键设计和原因**：默认回环监听，避免在尚无认证时暴露网络服务。数据目录 0700、新文件 umask 0077；日志不遍历环境或输出请求正文。数据库访问集中于模块，使用绑定参数而非拼接外部输入。信号处理器只设置 `sig_atomic_t`，独立线程调用正常关闭。显式关闭 `SO_REUSEPORT`，防止第二个服务进程在同端口分流。
3. **新增文件**：`CMakeLists.txt`、`.gitignore`、`.env.example`、`include/core.hpp`、`src/core.cpp`、`src/main.cpp`、`scripts/setup.sh`、`scripts/run.sh`、`tests/integration.py`、`README.md`、本报告。仅 `build/`、`var/` 是仓库内预期生成且忽略的内容；开发工具在 `/workspace/toolchain`。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   bash scripts/setup.sh
   # 已安装依赖后的常用命令：
   /workspace/toolchain/venv/bin/cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   bash scripts/run.sh
   # 另一终端：
   curl --fail http://127.0.0.1:8080/api/health
   ```

5. **实际测试结果**：安装脚本最终退出 0；构建完成。CTest 1 个集成 runner 执行 **8 项测试，全部通过、无跳过**，测试输出保存在 `build/Testing/Temporary/LastTest.log`。健康接口返回 200 和正确 JSON；验证 SQLite WAL/表及权限；64 个请求（16 并发）均成功；未知路径返回 JSON 404；第二进程绑定失败退出 1；6 组非法配置被拒绝；两个启动/停止周期证明持久化及 `error` 日志过滤；8 MiB + 1 请求返回 413。进程均经 SIGTERM 正常退出 0。另实际执行 `bash scripts/run.sh`，从本机请求默认 8080 端口，得到 `{"status":"ok","version":"1.0.0"}`。上述时长只是测试运行耗时，不作为吞吐/性能等级证明。

   修复记录：首次编译发现 cpp-httplib 0.18.7 线程池仅接受两个参数，修正版本接口；链接器要求显式链接 OpenSSL，增加 CMake 导入目标；端口冲突测试最初超时，定位库默认端口复用并关闭；所有失败检查在修复后重跑通过。

6. **限制与未实现事项**：本阶段没有浏览器页面、登录、角色、媒体扫描、Range、BT 任务、代理或远程分享；准备了相关依赖，但没有把准备依赖当作实现。尚未进行模糊测试、完整安全审计、浏览器播放或公网验证。远端 Git 仓库仍为空，工作区成果未推送；云配置保存也不等于发布或新任务恢复实测。
7. **下一阶段**：一次性管理员初始化、Argon2id 密码哈希、随机会话与失效、登录/退出/修改密码、管理员用户管理，先验证服务端角色与越权边界，再进入媒体阶段。

## 阶段 2：登录与权限（2026-10-12，已完成）

1. **完成内容**：本机一次性管理员初始化（隐藏输入、标准输入模式，无默认密码）；Argon2id；随机会话、Cookie/CSRF、Bearer；登录/退出/修改密码；管理员创建、禁用、启用、删除、重置密码、分配角色；服务端权限校验；审计；限流、会话上限及交互式 API 客户端。
2. **关键设计和原因**：采用本机 CLI 初始化，避免公开 HTTP 首次占用管理员入口。libsodium 负责密码哈希和随机数，SQLite 只保存会话摘要。管理变更在事务中重新鉴权，并使目标用户原有会话失效；最后一个有效管理员不能被禁用/降级/删除。Cookie 写请求要求 CSRF，固定角色从数据库取得，不接受客户端自报角色。密码校验有两个并发槽，登录采用真实来源 IP 限流。详细协议见 `auth.md`。
3. **新增或修改文件**：新增 `include/auth.hpp`、`src/auth.cpp`、`tests/auth_integration.py`、`scripts/auth-client.py`、`docs/auth.md`；修改 `include/core.hpp`、`src/core.cpp`（事务与 TTL 配置）、`src/main.cpp`（CLI 和路由注册）、`CMakeLists.txt`（认证源文件与测试）、`.env.example`、骨架回归测试及项目文档。未删除阶段 1 文件或重置运行数据；新增 users/sessions 表。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   /workspace/toolchain/venv/bin/cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   # 本机实际使用前，由使用者在终端隐藏输入创建自己的管理员：
   ./build/lanvideo --init-admin
   bash scripts/run.sh
   python3 scripts/auth-client.py me
   python3 scripts/auth-client.py users
   ```

5. **实际执行结果**：构建成功；两个 CTest runner 共执行 **31 项测试：8 项骨架回归 + 23 项认证集成，全部通过，0 失败、0 跳过**。最后一次全套运行约 11.65 秒，只是测试耗时，不作为性能等级证据。真实 CLI 初始化与并发初始化（两个独立进程，只一个胜出）、Cookie 与摘要保存、错误凭据、匿名/普通用户越权、CSRF、歧义认证、创建/禁用/启用/角色变更/删除、修改与重置密码后全部会话撤销、仅注销当前会话、重启持久化与过期拒绝、同密码不同盐、敏感日志检查、20 次限流、10 会话上限、两个管理员并发自禁用仍保留一个、Secure/Origin 策略、JSON/正文限制均通过。伪终端还验证了 C++ 密码输入不回显、终端回显恢复，以及 Python 客户端真实登录→列用户→CSRF 注销且不回显密码。

   首次编译修正了泛型路由 lambda 的 dependent template 语法。测试中发现 HTTPError 对象未关闭的 ResourceWarning，修正测试与客户端资源释放后重跑通过。事务提交后立即释放数据库连接锁，避免日志输出占用业务锁，重跑相关并发及完整回归均通过。

   已停止上一阶段由本任务启动的旧服务，确认退出 0，并用 `bash scripts/run.sh` 启动新版本；实际默认 8080 端口健康接口 200，初始化状态接口 200/false，匿名 `/api/me` 返回 401。没有替使用者创建实际 `var/` 管理员；测试随机凭据均留在临时目录并已清理。
6. **限制与未实现事项**：浏览器登录/管理页面尚未实现；媒体与任务资源级授权等待后续模块，目前只验证用户管理权限。HTTP 仍仅作为本机开发传输；Secure Cookie 标志测试不代表部署了公网 HTTPS。限流状态不跨重启，反向代理来源策略、日志轮转、MFA/找回、重置后强制改密和独立安全审计未实现。BT、视频播放、边下边播、远程分享仍未实现；工作区未提交或推送，不宣称新任务恢复已验证。
7. **下一阶段任务**：阶段 3，管理员共享根目录与扫描、普通用户媒体授权、每次读取的路径保护、HTTP Range 及播放历史、原生浏览器登录/播放页面；使用真实视频验证 Range 字节与解码/拖动流程，如无可视浏览器条件明确区分 HTTP/解码测试与人工浏览器实测。

## 阶段 3：媒体、局域网播放与新增本地视频上传（2026-10-12，已完成）

1. **完成内容**：将管理员本地视频上传加入功能项并实际实现；共享目录设置和默认目录、递归扫描、可选 FFprobe、搜索/格式筛选/分页、媒体授权、HTTP Range、私有播放历史、原生浏览器登录/用户管理/上传/授权/播放/密码界面。上传只显示真实传输字节和服务端确认，不模拟进度、速度或其他未实现功能。
2. **关键设计和原因**：用流式内容读取器写隐藏暂存文件，完整接收及媒体探测后重新鉴权，用不覆盖目标的原子重命名发布。逐级 openat/O_NOFOLLOW 与持有描述符防止路径穿越、叶子/父目录符号链接替换；每次媒体 GET/HEAD 重新验证权限。更换目录或文件指纹变化时重建 ID 并撤销旧授权，避免继承权限。隔离 cpp-httplib 的第二次 Range 处理，独立规范单范围边界，保持浏览器可定位读取。取消传输与撤销已完成保存明确区分；断开/关闭清理未完成暂存。CSP 不放宽 unsafe-eval，文件名只按文本插入页面。
3. **新增或修改文件**：新增 `include/files.hpp`、`include/media.hpp`、`src/files.cpp`、`src/media.cpp`、`web/index.html`、`web/style.css`、`web/app.js`、`tests/media_integration.py`、`tests/browser_integration.py`、`tests/requirements.txt`、`docs/media.md`、`docs/dependencies.md` 和 `docs/browser-evidence.json`；修改核心配置/SQL 类型绑定、main 路由与正文边界、CMake、安装脚本、示例配置及文档。保留已有认证和用户数据，添加 media/grants/history 表与 media_root 设置；运行与截图输出仍位于忽略的 build/var。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   bash scripts/setup.sh
   # 安装完成后的单独构建/复测：
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   # 仅在尚未初始化管理员时，由使用者隐藏输入设置自己的密码：
   ./build/lanvideo --init-admin
   # 服务未运行时启动；然后在实际浏览器访问服务根路径 /。
   bash scripts/run.sh
   ```

5. **实际测试及结果**：更新后的安装脚本在独立 venv 中实际执行，构建成功、退出 0。4 个 runner 执行 **60 项测试：8 骨架 + 23 认证 + 26 媒体/上传 + 3 浏览器，全部通过，0 失败、0 跳过**，总测试耗时 45.79 秒。真实视频完整字节对比及 FFmpeg 解码、闭合/开放/后缀/尾部截断/超大后缀/HEAD/If-Range、多种无效 Range、32 个并发 Range、9 MiB 上传、重名不覆盖、中文文件名、上传大小/类型/路径/CSRF、无 FFprobe 回退、授权/撤销、私有历史与重启、缺失/改变文件、父目录和叶子符号链接、数据库路径篡改、旧授权不继承、客户端断开均通过。

   活动上传实际进入暂存状态后被中断，未发布且临时文件清理；全部数据接收完成后用延迟包装器执行真正的 FFprobe 并断开连接，验证校验期间取消不会发布；未完成上传期间 SIGTERM，真实进程在 15 秒测试上限内退出 0 并清理。

   Chromium 154.0.8037.57 实际完成本地文件选择→XHR 上传→管理员授权→普通用户登录→原生 video 播放→定位到 6 秒→暂停保存→刷新恢复。此次 12 秒 320×180 MP4 记录：加载元数据 0.0561 秒；调用播放后 currentTime 超过 1 秒耗时 1.0274 秒；观测 30 个解码帧；持久化与恢复位置均为 6.260885 秒；观察到 waiting=1、stalled=0、playing=2（包含定位过程，不是持续卡顿率）。媒体请求实际携带 Range 并得到 206，越权请求实际得到 403。完整原始记录副本在 `docs/browser-evidence.json`；真实页面截图在 `build/Testing/artifacts/admin-upload.png` 与 `viewer-playback.png`。这些小样本记录不是性能等级、吞吐或 BT 启动/缓冲指标。

   修复记录：单个 JSON 数值构造 SQL 参数列表产生歧义，导致修改文件后的重扫失败；显式转换 ID 并检查参数数组后，更新和删除旧索引均复测通过。增大上传上限后改为头部拒绝非上传超限请求，原测试客户端 sendall 与提前关闭发生 BrokenPipe，改用真实头部响应断言 413。Playwright 的字符串轮询违反 CSP，改为函数轮询而不放宽 CSP。浏览器发现取消晚于保存提交的竞态，界面如实提示刷新确认，同时补充发布前断开检查；测试确认已保存文件若存在必为完整数据。补充前端账号切换的异步结果与历史队列隔离，避免旧账号的延迟请求混入新状态。
6. **当前限制和未实现事项**：本次真实浏览器和 HTTP 测试均在本机完成，未验证跨设备局域网可达性、公网 HTTPS、其他浏览器或全部视频编码；只有 MP4/H.264 被实际解码验证。上传专用于管理员视频，未实现普通用户私有上传、任意文件网盘、分块/断点上传、总磁盘配额或删除视频文件 API。FFprobe 是有界元数据探测，不是所有帧完整性证明。正常关闭可清理暂存，强制 SIGKILL/断电仍可能留隐藏暂存或完整但未入库的文件。容器 Chromium 沙箱 helper 不可用，只对生成的本机测试夹具使用无沙箱模式并限制外部请求，不改变产品/系统部署设置。BT、BT 代理、已校验分片播放、远程分享、HTTPS 部署未实现；未宣称安全等级、生产性能或新任务恢复。实际 var 数据库不包含固定演示账号，须由使用者初始化管理员。
7. **下一阶段任务**：阶段 4，接入已准备的真实 libtorrent，受控下载目录、种子元数据路径/大小校验、任务持久化与暂停恢复、出站代理和超时/错误说明；使用真实本地 seeder/downloader 验证 peer 通信、进度与分片哈希，再单独报告公网 Tracker/Peer 条件和限制，不用模拟结果替代。

## 阶段 4：新增断点续传、真实 BT 下载与出站代理

1. **完成内容**：新增持久化本地视频分块上传，支持中断、刷新和服务重启后继续；接入 libtorrent 2.0.11，实现 v1 种子导入、真实 TCP/HTTP(S) Tracker 下载、暂停/恢复/重校验、持久化 resume、任务所有者校验、可配置受控下载目录；加入 HTTP/SOCKS5 出站代理、可选认证、凭据加密保存、实际连接测试和错误处理；提供真实任务管理界面。
2. **关键设计和原因**：上传按 1 MiB 清单逐块 SHA256，fsync 后持久化确认偏移，重复块可安全重试，完整文件再次校验与原子发布；浏览器增量哈希限制内存，不仅依赖文件名/修改日期。BT 协议、分片调度和 SHA1 校验交给成熟 BSD 库；完成率仅计 have-piece 位图。每任务独立私有目录，并标记排除普通媒体读取，避免预分配/未校验数据进入播放器。HTTP 端口先绑定再启动 BT 监听；代理修改须暂停全部任务并关闭 Peer 连接。日志不使用含潜在凭据的 libtorrent message()。
3. **新增/修改文件**：新增 include/uploads.hpp、include/bt.hpp、include/proxy.hpp 和 src/uploads.cpp、src/bt.cpp、src/proxy.cpp；修改 main.cpp、media.cpp、media.hpp、CMakeLists.txt、web/index.html、web/app.js；增加 web/sha256.min.js 及许可证/完整性记录；新增 tests/uploads_integration.py、bt_fixture.cpp、bt_integration.py、browser_bt.py，扩充原有浏览器与认证测试、改进夹具失败清理；新增 docs/uploads.md、bt.md 和实测 JSON，更新 README、架构及依赖说明。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   /workspace/toolchain/venv/bin/cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DPython3_EXECUTABLE=/workspace/toolchain/venv/bin/python
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   bash scripts/run.sh
   # 首次账号只由用户通过隐藏输入创建，未提供默认密码：
   ./build/lanvideo --init-admin
   ```

5. **实际执行的测试及结果**：最终全量 7 个 runner，**79 项通过、0 失败、0 跳过**，148.99 秒；构建成功。分布为骨架 8、认证 23、媒体 26、原生浏览器 4、续传 7、BT 10、BT 浏览器 1。上传验证真实断流、重启、偏移重试、块校验失败、权限/CSRF、根目录变化及暂存篡改；浏览器第二分块断开后刷新，以 1048576 字节的确认偏移继续，文件字节/完整 SHA256 一致。BT 使用独立真实 libtorrent seeder、实际 compact HTTP Tracker 与转发 TCP 数据的 HTTP/SOCKS5 认证代理；核对整个视频文件与分片完成状态，暂停并跨重启继续，验证损坏分片重校验后重新下载、代理错误凭据/超时、失败不直连、路径和账号隔离。另一个 BT 测试实际完成 12 次停止、重新启动、健康查询和会话恢复。原始数据见 [stage4-test-results.json](stage4-test-results.json)、[bt-evidence.json](bt-evidence.json)、[bt-browser-evidence.json](bt-browser-evidence.json)、[browser-evidence.json](browser-evidence.json)；截图位于 build/Testing/artifacts。BT seeder 的测试上传限速为 512 KiB/s，观测速度是本机夹具数据，不能作为公网基准。

   实施期间修复了泛型 lambda 编译问题；测试曾与可执行文件重链接重叠造成启动权限失败，后改为先完成构建再测试。直接重校验已完成任务曾不能恢复连接，改为保留文件并重建 handle 后验证通过；测试临时挂起真实 seeder，以观察损坏分片下降，再恢复 seeder 验证修复，未注入进度。新增 BT 目录防护曾拒绝带尾部分隔符的媒体父目录，已修正规范化并重跑原媒体测试；另一次偶发服务启动失败促使改进启动原因输出、预先保留 HTTP 端口与重复启动验证。最终上述测试均通过，未通过删断言或跳过失败获得结果。

   当前持久化工作区通过 bash scripts/run.sh 启动，健康返回 status=ok/version=1.0.0；var 中用户/任务仍为 0，管理员未替用户创建，proxy.key 权限实测 0600。复用环境的 start_skill 已保存到配置草稿，包含启动/健康检查及当前验证范围；保存不等于发布或新任务恢复验证。
6. **限制和未实现事项**：只验证本机真实 TCP/Tracker/代理，公共 swarm、HTTPS Tracker 实际互通、公网代理、跨 NAT 均未实测。v2/混合种子、磁力链接、DHT、UDP/uTP、Web Seed、STUN/UDP 打洞未提供。出站代理/测试目标暂限 IPv4 字面地址；HTTP/SOCKS5 到代理的认证不是加密传输。没有总磁盘配额或后台定时清理，过期上传在启动/创建时尝试清理；已下载文件移除任务后保留。单个 info-hash 全局一个任务。边下载边播放及远程授权分享仍未实现，默认 Web 服务仅回环 HTTP，不是公网 HTTPS 部署；需复制已完成的文件到共享目录后扫描/授权。未声明安全/匿名/性能等级。
7. **下一阶段任务**：阶段 5 独立的已校验分片读取接口、媒体字节位置到 BT 分片映射、播放区域优先调度及公平性、未完成区域等待/重试策略；继续使用真实视频、seeder 和浏览器记录启动/缓冲行为。阶段 6 再提供可部署 HTTPS 反向代理适配和可过期/撤销授权链接，不把公网 IP 当作打洞成功。

## 阶段 5：已校验分片边下载边播放

1. **完成内容**：新增独立 BT 视频文件列表和受权限保护的分片 Range 播放接口，支持未完成下载时读取已有分片；提供播放焦点调度、等待/重试、校验失败隔离、原生浏览器播放器与自己的 BT 播放记录持久化。普通媒体目录隔离继续生效。
2. **关键设计和原因**：libtorrent have-piece 状态只作为读取前提；异步读取整个分片后通过 OpenSSL SHA1 再核对 v1 元数据，传输不可变内存快照；每块最多 64 KiB，输出前重新校验权限、路径和任务身份。视频文件起始字节 F + 浏览器实际 Range 字节 b 映射到 floor((F+b)/L) 分片和余数片内偏移；容器时间索引由浏览器处理，不用时长比例猜测字节。开放 Range 提供最多 1 MiB 的连续可读前缀；闭合/后缀范围保持准确。未完成区域最多等待 2 秒后返回 503/Retry-After；损坏片 409 隔离。焦点租约 10 秒、约 1 MiB 窗口；2 秒优先级 7、1 秒恢复默认 4，保留其他分片的调度机会。关闭、取消、重校验终止旧读取。
3. **新增/修改文件**：新增 src/bt_stream.cpp、tests/bt_stream_integration.py、tests/browser_bt_stream.py、docs/bt-streaming.md；修改 include/bt.hpp、src/bt.cpp、src/main.cpp、CMakeLists.txt、web/app.js/index.html；扩展真实 seeder 支持多文件种子和可配置测试限速，测试夹具支持零填充长视频；更新 README、架构、媒体和 BT 说明、OpenSSL 用途说明。原始本阶段数据为 stage5-test-results.json 与 bt-stream-browser-evidence.json。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   # 可只运行新增测试（先完成构建）：
   /workspace/toolchain/venv/bin/python tests/bt_stream_integration.py build/lanvideo build/bt_fixture
   /workspace/toolchain/venv/bin/python tests/browser_bt_stream.py build/lanvideo build/bt_fixture
   bash scripts/run.sh
   ```

5. **实际执行测试及结果**：最终 **9 个 runner、90 项通过，0 失败/跳过**，255.04 秒；构建成功。新增 10 项传输测试包括真实未完成前缀、503 等待、完整/闭合/后缀/开放 Range、无效 Range 重认证、权限/CSRF、重启后的历史、真实优先级周期及持续焦点下完成全部下载、损坏片拒绝、符号链接、并发读取、任务移除、服务关闭取消；多文件视频从种子第 37001 字节开始，验证跨文件分片 SHA1 与跨片输出字节一致。

   原生 Chromium 测试使用 45 秒 640×360 H.264 faststart MP4，**没有填充字节**，真实 seeder 限速 128 KiB/s。最新实测启动至 currentTime>1 为 17.336 秒，播放开始时下载完成率 49.611%，已解码 29 帧；拖动到 35 秒耗时 9.420 秒并确认 seeked 与后续解码，保存位置 35.000237 秒，刷新后从记录继续。观察到 2 次 waiting、0 次 stalled；这些是实际事件数，不是「无卡顿」等级判断。源视频 4095098 字节，原始 SHA256、HTTP 206/503 与事件时间线见 [bt-stream-browser-evidence.json](bt-stream-browser-evidence.json)，汇总见 [stage5-test-results.json](stage5-test-results.json)，截图在 build/Testing/artifacts/bt-stream-player.png。限速、单机小样本和重试策略明显影响启动/定位时间，不外推到公网吞吐或其他编码。

   首轮测试遇到 Peer 尚未连通的正常 503，改为执行协议要求的真实重试，仍要求在下载完成前返回正确字节。浏览器测试暴露手动重试未清空定时器，以及重新加载产生的 0 秒事件覆盖 35 秒定位；已修复并通过真正的 seeked/解码断言，没有用自然播放到 35 秒代替拖动。进一步区分「数据尚未完成」与「完整数据多次解码失败」，避免刚下载完就误报编码不兼容。原有 79 项功能回归全部保留并通过。

   我此前启动的旧工作区进程已用 SIGTERM 停止；新版通过 scripts/run.sh 启动，health 实际返回 ok，匿名 BT 播放/文件列表为 401，证明新路由加载且未暴露数据；工作区用户、任务、BT 历史仍为 0，没有替用户创建默认账号。start_skill 草稿已更新；保存不等于发布新快照或验证新任务恢复。
6. **限制和未实现事项**：仅实测本机 libtorrent/Tracker 和 Chromium H.264 MP4，其他浏览器/容器/编码未保证兼容。逐片读取和二次校验有额外开销；已发送响应头后的错误只能中断连接。HEAD 不证明可用性，无稳定 If-Range 验证器；一个任务仅保存最新焦点，周期调度不保证无 Peer/限速下的绝对公平。30 次连续自动重试后需手动重试；不转码、不实现 MSE/HLS。公共 swarm、公网代理、HTTPS Tracker 互通、跨 NAT 未验证；远程授权链接、HTTPS 反向代理、STUN/UDP 打洞仍未实现。未宣称性能、安全或匿名性等级。
7. **下一阶段任务**：阶段 6 独立远程访问适配层、可部署的 HTTPS 反向代理示例、短期授权链接的范围/有效期/撤销与服务端校验、连接失败和回退说明及实际测试。不会把公网 IP 获取或本机连接描述为跨 NAT 穿透成功。


## 阶段 6：HTTPS 反向代理与授权分享

1. **完成内容**：新增独立远程访问适配接口及本地/同机反向代理实现；提供实际运行的 Nginx HTTPS 配置生成器、TLS/服务身份诊断、失败封闭与显式本地回退。新增管理员签发的单视频读取链接、期限30秒至7天、撤销、独立播放会话、专用原生播放页及管理界面。
2. **关键设计和原因**：TLS入口与BT出站代理分开；后端绑定回环，私有入口密钥及单一转发头保护全部数据接口，畸形Range错误路径也验证入口。分享用256bit随机令牌，仅存哈希；URL片段交换成Path=/share/的HttpOnly/SameSite/HTTPS Secure cookie，令牌不进入请求URI或日志。链接限定一个完整媒体，不能调用主系统/BT接口；原有Media Range复用且每64KiB复验授权、路径与文件身份。签发者账号、角色或密码变更永久撤销委托授权。诊断验证证书链/域名、nonce和进程实例，不接受任意目标、不跳转、不自动降级HTTP。
3. **新增/修改文件**：新增 include/remote.hpp、shares.hpp，src/remote.cpp、shares.cpp，web/share.html、share.js，scripts/configure-remote.py，tests/shares_integration.py、remote_integration.py、browser_remote.py，docs/remote.md、stage6-test-results.json、remote-browser-evidence.json，deploy/README.md。修改 core.hpp/core.cpp、main.cpp、auth.cpp、media.hpp/media.cpp、CMakeLists.txt、web/index.html/app.js/style.css、scripts/setup.sh、.env.example、README与架构/依赖说明；修复原BT测试夹具的提前拒绝读取与断连socket清理；更新browser_bt_stream.py为90秒无填充真实视频，并保留独立阶段6分片播放证据。现有文件与数据保留，未提交或推送Git。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   bash scripts/setup.sh  # 签名包/固定Python依赖、构建、全量CTest
   /workspace/toolchain/venv/bin/cmake --build build -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
   /workspace/toolchain/venv/bin/ctest --test-dir build -R 'shares_integration|remote_integration|browser_remote' --output-on-failure
   bash scripts/run.sh
   ```

   真实HTTPS部署需要自己的域名/完整证书链/私钥；配置生成器和双进程启动的完整命令见 remote.md，不预置公开密钥或默认密码。
5. **实际执行的测试及结果**：最终验证覆盖12个runner、113项通过、0遗留失败/跳过：更新后setup.sh全量执行11个runner通过，短BT视频时序断言失败的runner修正真实夹具后单独完整复测通过。安装与构建成功；每次运行和用时记录在stage6-test-results.json，没有将失败的单次全量运行记为全通过。新增10项分享、12项远程与1项HTTPS浏览器测试：逐字节核对200/206/后缀/HEAD/416、真实等待30秒到期、持续大文件传输中撤销、账号禁用/重启/改密/路径替换/根目录变化、范围隔离与会话额度；真实Nginx TLS证书链/主机名的正负验证、转发头伪造/重复、畸形Range直连、HTTP到TLS端口拒绝、代理停止/重启、显式本地回退、错误服务与302不跳转、管理员/CSRF与私有文件权限。浏览器通过管理界面创建链接、播放H.264/定位8秒、确认已解码帧、撤销后404与清空播放器；最新本机HTTPS页面至metadata为0.6605秒、点击播放至currentTime>1为1.0191秒、30帧、8秒定位0.2789秒（单机夹具，不作公网性能结论）；原始数据见 remote-browser-evidence.json，截图位于 build/Testing/artifacts/remote-share-player.png。

   首轮C++编译发现同一auto声明混合string/size_t，已拆分并构建成功。Nginx首轮-t发现未使用的FastCGI等默认临时目录仍指向系统路径，已全部设置到私有数据目录并复测。配置测试发现Python的port or 443把端口0当作默认，已修为明确None判断并保留0/65536拒绝断言。首轮全量有BT超大请求测试发生BrokenPipe：服务在读头后已返回413并关闭，测试还在sendall；已用只发送实际Content-Length头、读取真实413的方式修复客户端竞态，未取消大小断言；代理夹具早期断连后upstream未进入with而泄露socket，也已在finally清理。修复后完整BT runner复测通过，再实际执行更新后的setup.sh验证安装、构建和全套测试的重复运行。重复setup验证另发现45秒视频偶尔在第一帧前已经完成下载，原有progress<1断言正确拒绝这次演示。改用90秒真实视频，保持同样128KiB/s限速与45秒起播超时、不加入填充、不改完成率断言；完整浏览器runner复测通过，最新真实8185184字节视频在起播时已验证完成率12.010%、起播9.181秒、30帧、35秒定位27.810秒，原始数据独立保存于stage6-bt-stream-browser-evidence.json，不替换阶段5的45秒历史记录。未用跳过或降低断言换取通过。
6. **当前限制和未实现事项**：仅本机真实TLS/Nginx/Chromium与生成夹具测试，公网DNS/证书续期、外部客户端/跨NAT、其他浏览器均未实测；未实现STUN/UDP打洞/协调服务/自建中继。当前反向代理仅支持同机，公网可达性由部署网络提供。分享只针对完整媒体，未分享BT未完成分片，不能限制收件人保存或追回已缓冲字节。播放会话最长1小时、单链接64会话，无全局后台记录清理。没有生产性能、安全或兼容性等级声明。默认服务仍回环HTTP，实际var用户/任务为0，未替用户初始化密码。新版已通过scripts/run.sh启动，健康200/ok，匿名远程/分享管理与分享信息接口401，分享页200；install_script和start_skill已保存为草稿，仍需发布，不等于发布或新任务恢复证明。
7. **下一阶段任务**：阶段7，补齐操作手册、可审查部署流程、监控说明和软著说明书初稿；复核功能状态与测试证据、执行交付构建及基础安全检查，不填写未经实测的公网能力、性能数据或项目经历。


## 阶段7：交付、监控与说明书

1. **完成内容**：新增管理员实际运行状态/只读审计及界面、原始query重复键防护、可迁移网页资源定位；完成Release构建/CMake安装、带SHA256清单/许可证/第一方源码清单的应用归档；提供用户、部署/运维、监控、测试、功能状态、复试演示手册和软著说明书初稿。真实运行数据、代理/入口密钥、种子和视频不进入发布包。
2. **关键设计和原因**：监控只显示SQLite事务快照、单调运行时钟和statvfs实际空间，暂停计数明确为请求状态，真实BT速度/Peer仍来自libtorrent；管理员角色每次验证。审计分页用before_id避免新事件挤移历史，纯textContent显示。资源从安装前缀../share/lanvideo/web定位，固定文件由openat/O_NOFOLLOW读取，显式LANVIDEO_WEB_ROOT覆盖且启动校验；安装不保留编译sysroot RPATH，运行依赖外部库而非伪称免依赖。包内容由明确CMake安装规则决定，归一化时间不冒充研发日期，原始令牌/数据库/密钥不打包。
3. **新增/修改文件**：新增 include/monitor.hpp、src/monitor.cpp、scripts/package.py、tests/monitor_integration.py、delivery_integration.py、package_smoke.py；新增docs/user-manual.md、deployment.md、monitoring.md、demo-guide.md、feature-status.md、testing.md、copyright-draft.md、stage7-test-results.json及stage7原始JSON，保留历史阶段记录；新增docs/licenses完整上游包告示/公共许可及deploy/lanvideo.service.example。修改CMakeLists.txt、core.hpp/core.cpp、main.cpp、media.cpp、shares.cpp、web/index.html/app.js/style.css、.env.example、browser_integration.py和README/架构/认证/依赖文档。未删除已有数据，未提交或推送Git。
4. **构建、启动和测试命令**：

   ```sh
   cd /workspace/LANVideoServer
   /workspace/toolchain/venv/bin/cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE=/workspace/toolchain/venv/bin/python
   /workspace/toolchain/venv/bin/cmake --build build-release -j2
   /workspace/toolchain/venv/bin/ctest --test-dir build-release --output-on-failure
   /workspace/toolchain/venv/bin/ctest --test-dir build-release --rerun-failed --output-on-failure
   /workspace/toolchain/venv/bin/python scripts/package.py --build build-release --output build/artifacts
   # 将实际生成文件代入；本云安装测试显式提供已验证私有运行库：
   /workspace/toolchain/venv/bin/python tests/package_smoke.py build/artifacts/ACTUAL_RELEASE_FILENAME.tar.gz --library-path /workspace/toolchain/sysroot/usr/lib/x86_64-linux-gnu
   ./build-release/lanvideo
   ```

5. **实际执行的测试及结果**：Debug与Release均构建成功，新增8项监控、3项安装与1项原生浏览器用例通过。Release全量运行14个runner，13个完整runner通过，交付runner发现文档改为保留相对链接的子目录后，生成安装规则仍为旧平铺布局；重新生成CMake安装规则后，完整3项交付runner复测通过。最终**14个runner、125项通过、0遗留失败/跳过**，属于全量加失败runner修复复测的合并覆盖，未把首次失败的单次全量运行标为全通过。首轮全量332.56秒，交付复测0.78秒；分布8/23/26/5/7/10/1/10/1/10/12/1/8/3，详见stage7-test-results.json。

   安装测试实际将前缀移动后启动二进制，确认使用移后网页、健康200、匿名管理员401、重启保留私有状态、缺失/符号链接资源拒绝。归档工具独立核对每文件SHA256、清单完整性、无私有状态/测试可执行文件、迁移启动、无默认管理员、SIGTERM返回0；提供的外部库是当前签名验证sysroot，不冒称免依赖或其他平台兼容。第一方源码清单统计33文件、3657实际源行（其中C++头/实现2710行），排除第三方SHA256实现与测试夹具；不填充虚构源码。完整Boost/nlohmann等包版权告示与必要公共许可证另作为告示分发，不计自研源码。

   最新真实HTTPS浏览器页面至metadata0.7811秒、30帧、8秒定位0.3199秒；90秒无填充BT视频起播9.179秒、起播时验证完成12.010%、29帧、35秒定位34.420秒。均为本机小样本与128KiB/s Seeder限速，不作公网性能结论；原始数据在stage7-remote-browser-evidence.json、stage7-bt-stream-browser-evidence.json。管理员监控截图为build-release/Testing/artifacts/admin-monitor.png；注入HTML审计行仅用于测试纯文本输出，无生产注入API，不冒充业务事件。

   实施修复了泛型lambda的依赖模板解析编译错误；参数测试发现httplib折叠相同query对，改为在原始target解码键并拒绝重复/编码别名，保留越界/注入/重复断言并全量验证。浏览器测试最初误假设根目录路径会进入审计（实际审计只存changed布尔），改为明确测试夹具注入攻击字符串验证textContent，没有改变生产日志去记录路径。手工检查敏感关键词日志位置确认只存受控事件/actor/task，相关真实凭据/令牌泄露测试均通过。未声称独立渗透/漏洞库扫描/匿名或安全等级。

   当前实际var用户/任务/分享均0，未替用户初始化密码；Release进程已启动，health200/ok，匿名status/audit/remote/shares均401、分享页200。环境复用说明保存为草稿，保存不等于发布、新任务恢复或公网部署。
6. **限制和未实现事项**：仍只验证当前Linux/Chromium/真实本机TCP/TLS夹具；公共swarm、公网入口、公共证书续期、跨设备/跨NAT和其他浏览器未验证；STUN/UDP打洞/协调服务/自建中继、v2/磁力/DHT/UDP、BT分片匿名分享、转码/HLS/MSE、DRM、配额/自动备份/迁移/MFA未提供。systemd模板未在云容器部署。应用包需要外部运行库，第一方源码清单用于审查/申报参考，不是将第三方依赖一起静态打包；完整可构建工程在当前工作区。软著权利人、开发/发表事实等由申请者真实填写，未提交登记。基础检查不是生产性能/安全认证。
7. **下一阶段任务**：计划阶段0–7已完成。后续实际部署由使用者设置自己的管理员、域名/证书及网络入口并进行公网/跨设备验收；软件登记材料需补充真实身份/权利/完成发表事实并按要求排版审阅。本次没有新增未经授权的公网发布、提交登记、Git推送或系统服务修改。
