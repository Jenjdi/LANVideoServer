# 运行监控、审计与基础安全检查

## 实际监控接口

`GET /api/health`公开：真实执行SQLite SELECT 1后返回status/version；不包含路径、账号或任务数据。它只证明HTTP/数据库探测成功，不证明磁盘充足、BT连通、外网可达或已运行完整测试。HTTP客户端需设置超时并检查状态码与JSON内容，不能仅凭PID/端口判断服务可用。

管理员登录后访问：

|接口|实际来源/限制|
|---|---|
|`GET /api/admin/status`|事务内SQLite计数快照、单调时钟运行秒数、statvfs数据卷可用字节、已配置上限。没有随机CPU/吞吐/Peer指标|
|`GET /api/admin/audit?limit=50&before_id=N`|只读审计降序页，limit1..100，before_id为正整数；返回next_before_id或null，插入新记录不改变旧页范围|
|`GET /api/tasks`|真实libtorrent下载/校验状态、速度和连接数；统计与主监控快照不是同一时间点|
|`GET/POST /api/remote[/test]`|管理员配置/诊断结果；本机到HTTPS入口的TLS/进程身份验证，不是公网客户探测|

每个请求校验当前会话和管理员角色；匿名401、普通用户403；禁用/变更角色使旧会话失效。参数预编译绑定，未知/越界/重复参数拒绝。httplib会折叠完全相同的query对，阶段7在原始请求target上解码键并拒绝重复，包括字面相同和编码别名，畸形Range错误路径也执行同一检查。

`tasks.requested_paused`是数据库命令状态，不能冒充瞬间真实暂停连接数。`shares.active`为未撤销、未过期的配置记录数；禁用/密码/角色变化会将签发者记录永久撤销。`sessions`为未过期数据库会话数，不是当前TCP连接数。`data_fs_free_bytes`只表示运行数据卷，探测失败为null；不覆盖其他挂载卷、不证明用户配额。`limits`是配置，不是实测速率/并发能力。界面手动刷新并显示快照时间。

## 日志和审计

stdout输出逐行JSON：time、level、event、fields。`LANVIDEO_LOG_LEVEL=info|error`；错误级别只过滤stdout info，不关闭数据库audit。操作日志使用受控ID、角色、禁用状态、数量与固定事件枚举；不输出HTTP头/正文、会话/CSRF/分享令牌、密码/密码哈希、入口密钥、代理认证或原始libtorrent message()。代理凭据使用secretbox加密保存，密钥文件0600；会话/分享令牌只存SHA256。

Nginx示例关闭access_log，分享链接令牌用URL片段，避免进入请求URI；专用播放cookie为HttpOnly。日志输出由终端/systemd或运营者收集，本程序没有内置轮转、自动审计清理/归档或远程告警。不要把生成的含密钥nginx.conf当普通日志上传；审计/运行数据与发布包分开。

检查事件：startup_failure、internal_error、json_type_error、bt_stream_checksum_failed、BT保存超时及远程entry测试失败。以事件类型与受控状态排查，再根据实际磁盘、网络与权限复现；不要用未测量的CPU/带宽数值判断性能等级。

## 本次基础安全检查的范围

代码检查与集成测试覆盖：Argon2id盐/非明文密码、一次性初始化、注销/到期/禁用/角色/改密、最后管理员、CSRF及Origin、重复认证/输入、SQLite绑定、目录openat/O_NOFOLLOW、范围200/206/416、文件替换/缺失、普通用户隔离、上传/清单/种子上限、分片二次哈希、代理失败不直连、TLS证书链/主机名、入口头伪造、分享范围/到期/撤销、日志敏感值、监控管理员边界和审计纯文本显示。

这是一组有界的代码与功能检查；没有独立渗透测试、依赖漏洞数据库扫描、抓包级DNS/IPv6泄漏验证、全编码模糊测试、压力/耐久或计时侧信道审计。不能据此宣称某安全等级、匿名等级或生产性能。公开health或设置CSP不等于整体产品已经安全认证。

发布包包含逐文件SHA256清单和外部依赖/许可证说明；本次验证安装后迁移路径、网页资源、无默认账号、权限拒绝与SIGTERM关闭。安装包不包含运行数据库、密钥、真实凭据、种子、下载视频或BT测试可执行文件。清单证明本次打包内容完整性，不证明发布者数字签名/恶意软件认证。

测试命令及逐项实际结果见 [testing.md](testing.md) 与 stage7-test-results.json。
