# HTTPS 入口部署

同机 Nginx 配置由 `scripts/configure-remote.py` 生成，要求自己的 HTTPS origin 和已有证书/私钥，且拒绝覆盖既有文件。生成结果含随机入口密钥，必须放入 Git 忽略的私有运行目录（如 var/nginx.conf），不能作为普通示例提交。

完整命令、TLS与入口边界、代理失败回退、网络前提和实际测试范围见 [远程访问说明](../docs/remote.md)。默认运行仍为回环 HTTP。不会自动配置域名、证书续期、端口映射或内网穿透。
