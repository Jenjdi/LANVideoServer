# 分发依赖告示

本目录保存当前已验证Debian开发包/系统OpenSSL的完整copyright告示，以及其引用的公共许可证文本；来源为签名/包校验已验证的sysroot与当前系统安装。保留上游与Debian包装的区分，不把外部代码改为本项目MIT。

nlohmann/json为编译进应用的MIT头文件实现；Boost头文件使用BSL-1.0；cpp-httplib/libsodium/libtorrent为动态依赖。完整记录保留版权持有者与许可条件；这些许可文本不是软件自研源码。SQLite公开领域声明见dependencies.md。浏览器js-sha256的完整MIT许可另见../js-sha256-LICENSE.txt。OpenSSL告示与Apache-2.0文本一并保留。

本应用包不携带系统动态库、FFmpeg、Chromium或Nginx二进制。若部署者另外打包这些工具/库，需保留对应分发物的许可证及第三方告示；当前文本不代表已经满足任意其他分发形式的全部义务。完整名称/版本/用途见../dependencies.md。
