# 阶段 4：真实 BT 下载与出站代理

C++ Bt 模块链接 libtorrent-rasterbar 2.0.11（BSD-3-Clause），使用其 Tracker/Peer 协议、分片调度与 SHA1 完整性校验。界面显示的速度、Peer/Seed 连接数来自 libtorrent status；完成率根据实际 have-piece 位图和各分片大小计算，排除未校验的在途块。连接数不是全网 Peer 总量。

## 操作与支持范围

登录 → BT 下载 → 导入 `.torrent`（默认暂停） → 恢复开始下载。普通用户只能操作自己的任务，管理员可管理全部。完成后进入 seeding 并可能向其他 Peer 上传；暂停可停止任务的传输。可以暂停、恢复、重新校验、移除；移除任务保留私有目录中的下载文件，由管理员在服务器上管理文件。当前单个 info-hash 全局只允许一个任务，这是共享 libtorrent session 的限制；不允许通过重复导入接管另一用户的任务。

管理员可设置新任务下载目录；旧任务始终保留原目录。服务端创建 `task-<numeric-id>` 私有目录，保存路径由服务端决定。标记 `.bt-task` 的受控 BT 目录和其下级禁止通过普通媒体接口分享；扫描其祖先目录时跳过这些任务目录，避免将预分配的未校验文件用于视频播放。当前需要将已完整下载的文件手动复制到共享目录并扫描/授权；阶段 5 才提供校验分片播放接口。

实现支持 **v1 种子、TCP Peer、HTTP/HTTPS Tracker**。限制：元数据 1 MiB、文件最多 1000 个、路径最多 16 层、总大小 100 GiB、最多 262144 分片、分片尺寸 16 KiB..4 MiB、HTTP(S) Tracker 最多 32 个、任务总计 32 个/每用户 8 个、全局连接 128/每任务 32。禁止符号链接、隐藏路径、绝对路径、上级路径、重复路径、重复 bencode 键、尾随元数据和带 userinfo 的 Tracker URL。

v2/混合种子、磁力链接、DHT、LSD、UDP Tracker/uTP、UPnP/NAT-PMP、Web Seed 未提供；不将依赖库的能力自动算作应用已完成的功能。HTTPS Tracker 启用系统证书校验，未禁用 libtorrent 的 SSRF 限制。单机本地真实测试不证明公网可连接；本环境尚未测试公共 Tracker、真实互联网 swarm、跨 NAT 下载或公网代理。

## API

|接口|行为|
|---|---|
|POST `/api/torrents`|原始 `application/x-bittorrent` 文件；受限 Content-Length；当前账号创建，201 返回 task|
|GET `/api/tasks`、`/api/tasks/:id`|当前用户可访问任务；状态、真实校验字节、速度、连接 Peer、分片数、错误|
|POST `/api/tasks/:id/pause`、`resume`、`recheck`|异步 libtorrent 控制；查看实际 paused_actual 和 state 以确认完成|
|DELETE `/api/tasks/:id`|取消任务；保留下载文件|
|GET `/api/bt/config`|管理员下载根目录、脱敏代理配置|
|POST `/api/bt/root`|管理员 JSON `{path}`；已存在、不含符号链接的绝对目录；对新任务生效|
|POST `/api/proxy`|管理员配置 `{type,host,port,username,password,timeout_ms}`；type 为 none/socks5/http；修改前暂停全部任务|
|POST `/api/proxy/test`|管理员目标 `{host,port}`；真实代理握手及目标 TCP 连接，返回实测 elapsed_ms；不是下载完成验证|

## 下载断点与分片验证

SQLite tasks 保存 owner、root、name、info_hash、paused、created 及 libtorrent 的 bencoded resume（以 hex 存储）；原始元数据独立持久化到数据目录 bt-meta。后台每 5 秒请求保存，经 save_resume_data_alert 获取磁盘 flush 后的 resume；暂停及关闭时也请求保存。关闭最多等待 3 秒保存回执；超时明确记录，未存状态可通过文件重哈希恢复。服务关闭还等待 libtorrent 终止网络与磁盘工作。

任务恢复时重新验证元数据与下载目录，清除 resume 中重命名和 Web Seed，强制重新校验已下载文件；不信任仅由文件存在或旧位图推断的完整性。身份权限变更后接口立即重新验证；后台会在至多 5 秒内暂停被禁用或删除用户的任务。

手动重新校验先移除旧 handle，再在同一目录创建新的 handle，丢弃旧 resume 位图及已完成任务的 Peer 状态，保留文件与任务身份。当前应用曾在已完成任务直接 force_recheck 后出现连接恢复失败，重建 handle 的处理已通过实际修复测试；未据此声称已定位库的内部缺陷；不将旧 Peer/进度状态沿用到新 handle。自动重启恢复仍在新会话中强制 recheck。

重新校验是异步操作，校验期间界面展示 checking；损坏分片回到未完成状态，恢复下载从真实 Peer 重新请求。连接失败由库进行重试与回退，应用未产生模拟 Peer 或速度。

## 出站代理

支持 HTTP 代理（Peer 使用 CONNECT、HTTP Tracker 使用代理请求）及 SOCKS5，包含可选用户名/密码。代理地址和连接测试目标当前限 IPv4 字面地址；BT Tracker 仍支持域名。测试超时 100..10000 ms，通过非阻塞 socket 和 poll 控制；BT 连接超时换算为至少 1 秒。HTTP/SOCKS5 在客户端与代理之间本身不加密认证信息，适用于可信出口/局域网或受保护通道。HTTPS Tracker 和公网 Web 入口不是同一概念。

代理配置使用 libsodium secretbox（随机 nonce、认证加密）保存到数据库，本机随机 32 字节 key 位于 0600 proxy.key，所属用户及权限在启动时校验；丢失 key 无法恢复凭据。密钥与密文在同一服务账号下，不能防御获取该账号完整文件权限的攻击者。配置接口只返回 credentials_configured，不回显用户名或密码；修改时必须重新输入。日志/审计不记录密码、令牌、代理凭据、原始 Tracker URL 或 libtorrent message()。

Tracker 与 Peer 均启用代理，代理失败不改为直连；DHT、UDP、局域网发现和自动端口映射关闭。代理修改要求所有任务处于请求暂停状态，且实际 Peer 连接已经关闭；已暂停连接由 libtorrent 关闭，恢复使用新配置。尚未经过抓包级 IPv6/DNS 泄漏审计，不声称具备匿名性等级。反向代理与中继是阶段 6 的独立入站访问模块，本阶段没有内网穿透。

## 可重复真实验证

CMake 构建 tests/bt_fixture.cpp：使用真实文件生成 v1 元数据和各分片哈希，再启动独立 libtorrent seeder，读取/校验文件后才宣告 ready。Python 测试启动紧凑格式 HTTP Tracker 和实际转发 TCP 数据的 HTTP/SOCKS5 代理，下载端运行正式 lanvideo 服务。测试断点、重启、文件字节、任务权限、错误凭据、超时、代理失败不直连、损坏文件校验与恢复、路径防护。

```sh
/workspace/toolchain/venv/bin/cmake --build build -j2
/workspace/toolchain/venv/bin/python tests/bt_integration.py build/lanvideo build/bt_fixture
```

测试只使用自动生成的本机视频和临时目录，不依赖公共种子、不替用户设置管理员密码。原始观测见 build/Testing/artifacts/bt-evidence.json；这是本机实验数据，不是公网性能基准。

## 阶段 5 更新

现已提供独立的已校验分片在线播放、范围到分片精确映射、周期公平调度、等待/重试和自己的持久化 BT 播放记录，详见 [bt-streaming.md](bt-streaming.md)。上文阶段 4 的「边下载边播放未提供」是历史阶段边界；普通媒体目录隔离继续生效，下载内容只通过新的受控分片接口读取。
