# 轻量级视频共享与传输软件 V1.0

C++20 视频共享服务，面向个人和小型团队。服务端管理账号、资源授权、上传和下载任务，浏览器使用原生 `<video>` 播放器。

## 功能

- 一次性管理员初始化，登录、退出、修改密码，以及用户创建、禁用、删除、密码重置和角色分配。
- 视频目录扫描、搜索、格式筛选和按用户授权；HTTP Range 播放、拖动与播放位置保存。
- 本地视频上传，1 MiB 分块断点续传，SHA256 校验、持久化偏移和禁止覆盖发布。
- 基于 libtorrent 的 v1 种子下载，任务暂停、恢复、重校验与重启恢复；显示库提供的进度、速度和连接数。
- 已校验 BT 分片播放，播放位置优先调度；未就绪区域返回等待与重试结果。
- HTTP、SOCKS5 出站代理，认证凭据加密保存、连接测试和超时处理。
- Nginx HTTPS 反向代理适配，入口验证、TLS 诊断及限时、可撤销的单视频分享链接。
- 管理员运行状态、磁盘空间统计和操作审计。

## 构建与测试

当前验证环境为 Debian 13 x86_64、GCC 14、CMake 3.31。完整 Release 回归通过 15 组、141 项用例；安全审查及 ASan/UBSan 范围见 [测试记录](docs/review-test-results.json)。依赖及许可证见 [dependencies.md](docs/dependencies.md)。

在 Debian 13 开发机安装依赖：

```sh
sudo apt-get install build-essential cmake libssl-dev libsqlite3-dev libcpp-httplib-dev \
  nlohmann-json3-dev libsodium-dev libtorrent-rasterbar-dev libboost-system-dev \
  python3-venv ffmpeg chromium nginx
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
PYTHON=.venv/bin/python bash scripts/build.sh
```

`build.sh` 默认生成 Release 并运行全部 CTest；可设置 `CMAKE`、`PYTHON`、`LOCAL_DEPS`、`BUILD_TYPE`、`JOBS`，或传入构建目录。云工作区已有工具链时：

```sh
CMAKE=/workspace/toolchain/venv/bin/cmake \
PYTHON=/workspace/toolchain/venv/bin/python bash scripts/build.sh
```

`scripts/setup.sh` 专用于 Debian 13 云环境的无 sudo 依赖准备。只构建服务可使用 `cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF`。

## 启动

```sh
export LANVIDEO_DATA="$PWD/var"
./build-release/lanvideo --init-admin
bash scripts/run.sh
```

初始化时交互输入用户名和密码，同一数据库只允许一次。默认监听 `127.0.0.1:8080`，在浏览器打开服务地址后登录；按 Ctrl+C 或发送 SIGTERM 停止。没有默认账号或密码。配置项见 [.env.example](.env.example)，程序读取环境变量。

管理员可在管理页创建用户、设置共享目录、扫描或上传视频，并给普通用户授权。BT 导入任务初始暂停，恢复后开始下载；选择任务中的视频文件可播放已校验分片。

## 部署与范围

默认 HTTP 适用于本机和受信任局域网；公网部署使用 HTTPS 入口，后端保留回环监听。BT 出站代理与 HTTPS 反向代理分别配置。反向代理部署需要已有的网络可达入口，本版本未实现 STUN/UDP 打洞，也未完成公网跨 NAT 实测。BT 验证使用本机独立 Seeder、Tracker 和代理；浏览器播放、拖动和 HTTPS 分享均有实际测试记录。

Linux 发行包需要系统动态库，包含程序、网页、说明和许可证，不包含账号数据库、密钥或下载文件。构建安装、反向代理配置和备份见 [部署说明](docs/deployment.md) 与 [远程访问说明](docs/remote.md)。

## 文档与发布

[用户手册](docs/user-manual.md) · [技术架构](docs/architecture.md) · [安全审查](docs/security-review.md) · [测试说明](docs/testing.md) · [功能状态](docs/feature-status.md) · [软件说明书](docs/copyright-draft.md)

```sh
python3 scripts/package.py --build build-release --output build/artifacts
python3 scripts/copyright-package.py --output build/artifacts
python3 tests/package_smoke.py build/artifacts/实际发行包.tar.gz
```

GitHub 的 `v*` 标签触发构建、测试和 Release 发布；软件说明书与源程序另存为独立 ZIP。项目代码采用 [MIT](LICENSE) 许可证，第三方许可通知保留在 [docs/licenses](docs/licenses)。
