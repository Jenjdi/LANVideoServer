# 阶段 2：认证、权限和操作手册

阶段 2 提供真实 C++ API 和交互式命令行客户端；阶段 3 已补充浏览器登录、管理与密码界面，操作见 `media.md`。
本机 HTTP 已实测；配置 HTTPS origin 时的 Cookie/Origin 规则已测试，但未部署 TLS 终止服务器，不能据此称公网 HTTPS 已实现。

## 管理员一次性初始化

先构建，然后在项目工作目录运行：

```sh
cd /workspace/LANVideoServer
./build/lanvideo --init-admin
```

输入 3–64 位 ASCII 账号（字母、数字、点、下划线、连字符），输入并确认至少 12 字节、最多 1024 字节的密码。密码回显关闭，CLI 成功退出 0，并记录不含凭据的初始化事件。失败退出 1，JSON 日志说明原因且不包含输入值。

初始化在 `BEGIN IMMEDIATE` 事务中检查用户表和持久化 `auth_initialized` 标记，然后原子地写入管理员与标记；两次初始化并发仅一个成功。同一数据库不能再次初始化，HTTP `POST /api/bootstrap` 固定拒绝。保护最后一个有效管理员，避免通过普通管理操作清空管理员后重新初始化。

自动化可运行 `./build/lanvideo --init-admin-stdin`，从标准输入提供一个 `{"username":...,"password":...}` JSON 对象（最多 4096 字节）；必须从可信进程、安全存储或隐藏输入提供密码。不要把密码写进命令行、环境变量、代码、配置样例、日志或 shell 历史。程序没有设置默认账号或密码。该命令可以在 HTTP 服务运行期间执行，SQLite WAL 及事务保证并发安全，服务即时识别初始化状态。

初始化失败无需删除数据库重试；纠正账号/密码输入即可。已初始化数据库不会被第二次命令重置。不要删除用户数据来绕过一次性流程。

## 人工操作与演示

所有下列命令均提示登录账号与隐藏输入密码；创建/重置/修改密码另提示隐藏输入并确认。客户端只在内存中保留 Cookie 和 CSRF，完成操作后自动注销。

```sh
cd /workspace/LANVideoServer
bash scripts/run.sh
# 另一终端：
python3 scripts/auth-client.py me
python3 scripts/auth-client.py users
python3 scripts/auth-client.py create demo_user --role user
python3 scripts/auth-client.py create second_admin --role admin
# 以下 2 仅为示例用户 ID，以创建/列表返回的实际 ID 为准：
python3 scripts/auth-client.py disable 2
python3 scripts/auth-client.py enable 2
python3 scripts/auth-client.py set-role 2 admin
python3 scripts/auth-client.py reset-password 2
python3 scripts/auth-client.py change-password
python3 scripts/auth-client.py delete 2
```

`me` 和 `change-password` 支持普通用户。其他命令由服务端校验管理员角色，普通用户直接调用接口也会得到 403。客户端默认连接回环 HTTP；部署真实 HTTPS 入口后可传 `--url https://你的实际域名`，Python 保留 TLS 证书校验。

## 已实现 API

|方法与路径|授权|正文/结果|
|---|---|---|
|GET `/api/bootstrap/status`|公开|`initialized` 状态，不含凭据|
|POST `/api/bootstrap`|任何调用都拒绝|405，提示使用本机 CLI|
|POST `/api/login`|公开，限流|`username,password`；返回 `user,csrf_token,expires` 并设置 Cookie；不在 JSON 返回会话令牌|
|GET `/api/me`|已登录|当前用户 `id,username,role` 和用于刷新页面的 CSRF 值|
|POST `/api/logout`|已登录|注销当前会话，清除 Cookie；其他会话不受影响|
|POST `/api/password`|已登录|`old_password,new_password`；成功后撤销自己的全部会话，需要重新登录|
|GET `/api/users`|管理员|`id,username,role,disabled` 列表；不返回密码摘要|
|POST `/api/users`|管理员|`username,password,role`，role 默认 user；成功 201|
|PATCH `/api/users/:id`|管理员|`role` 和/或布尔值 `disabled`；撤销目标全部会话|
|DELETE `/api/users/:id`|管理员|删除用户；外键级联删除会话|
|POST `/api/users/:id/password`|管理员|`password`；重置密码并撤销目标全部会话|

JSON 写接口要求 `Content-Type: application/json`，正文上限 4 KiB，拒绝未定义的字段、错误类型、弱长度/超长密码、非法角色及越界 ID。用户名大小写敏感。
`PATCH` 不提供修改账号或直接写入密码摘要的通道，`disabled` 必须为 JSON 布尔值。列表中的 `disabled` 是 SQLite 的 0/1 值。
密码长度按 UTF-8 字节计，不允许 NUL；长度策略不是密码强度等级证明。

Cookie：`lv_session`，256 位随机值，`HttpOnly; SameSite=Strict; Path=/; Max-Age=...`。配置非空 `LANVIDEO_PUBLIC_ORIGIN=https://...` 后加 `Secure`，设置的 origin 必须精确匹配浏览器 Origin（不要添加默认端口或尾部路径）。配置 HTTPS origin 后，本机 HTTP 浏览器不会发送 Secure Cookie，应经真正的 HTTPS 入口访问。

使用 Cookie 认证的写请求必须携带登录或 `/api/me` 返回的 `X-CSRF-Token`。值由会话随机令牌派生，服务端用恒定时间比较验证；不写入日志。没有开跨域 CORS。已配置公开 origin 时，带不匹配 Origin 的登录或写请求返回 403。
非浏览器客户端也可以使用 `Authorization: Bearer <会话值>`；Bearer 不依赖自动发送的 Cookie，写请求不要求 CSRF，但仍校验会话、角色及配置的 Origin。禁止同时使用 Cookie 与 Bearer，重复/歧义的会话 Cookie 或 Authorization 头返回 400。会话不支持 URL 查询参数传递。

常见错误：未登录、注销、过期或被撤销 → 401；普通用户越权、缺少 CSRF、旧密码不正确 → 403；重复账号、最后管理员保护、重复初始化 → 409；JSON 类型/字段/长度错误 → 400；正文太大 → 413；错误 Content-Type → 415；限流或哈希槽忙 → 429；内存不足无法哈希 → 503。登录失败不区分账号不存在、密码错误或账号禁用。

## 数据与并发设计

- 新增 `users(id,username,password_hash,role,disabled)` 和 `sessions(token_hash,user_id,expires,created)`；保持既有 settings/audit 数据。
- 使用 libsodium Argon2id v1.3，当前参数为 64 MiB、2 次迭代、p=1，每次创建/修改密码由库随机生成盐。测试验证同一密码对应不同摘要并通过实际登录验证。
- 数据库只存 SHA-256 会话摘要，不存原始会话。缺失账号也做真实 dummy Argon2id 验证，避免直接返回形成明显快速分支；没有做计时侧信道的完整审计。
- 会话默认 28800 秒，可配置 `LANVIDEO_SESSION_TTL=30..86400`；过期在每次请求检查并在登录时清理。每用户最多 10 个会话，新登录淘汰最旧会话。
- 对用户名、会话、密码摘要和所有数据库参数使用预编译绑定；不拼接外部 SQL 输入。所有管理变更在事务内重新检查会话和角色，最后管理员检查与更新/删除处于同一事务。
- 同进程连接使用递归锁，跨进程使用 SQLite 写事务与 busy timeout；提交后释放连接锁再输出日志。CLI 初始化、用户创建及密码更新均不重写数据库或删除原有记录。
- 登录 IP 限流窗口为 60 秒、20 次尝试，包括成功尝试；来源取实际 TCP `remote_addr`，不信任客户端 `X-Forwarded-For`。窗口仅在本进程内，重启不保留；阶段6反向代理模式先由全局入口密钥验证同机代理，再使用由Nginx覆盖的X-Real-IP；本地模式仍取TCP来源。客户端伪造转发头不被直接信任。
- 哈希/验证最多两个并发槽（每个 64 MiB）；忙时返回 429。一般 HTTP 限制仍由阶段 1 的有界队列、正文和超时实现。
- 数据库 audit 永久记录关键初始化、登录、注销、账号管理和密码变更事件；只含时间、actor 和受控 ID/角色/禁用字段。`LANVIDEO_LOG_LEVEL=error` 可过滤 stdout info 日志，但不会关闭数据库审计。阶段7新增管理员只读审计查询及界面；仍无内置日志轮转。

## 验证与边界

```sh
/workspace/toolchain/venv/bin/cmake --build build -j2
/workspace/toolchain/venv/bin/ctest --test-dir build --output-on-failure
```

阶段 2 增加 23 项真实集成测试（含伪终端密码隐藏及交互客户端），连同阶段 1 共 31 项。测试使用临时数据库、真实服务进程和 HTTP 请求；会话过期测试仅在测试临时数据库中写入过期时间，不增加任何生产时间/权限绕过接口。完整结果见阶段报告。

还没有 MFA、邮箱找回、密码重置强制首次修改、自动证书、独立安全审计。阶段3已实现Web登录和媒体资源授权，阶段4/5已实现BT任务所有者及分片读取权限，当前测试证明已列出的边界，不能据此宣称整体软件达到某种安全等级。
