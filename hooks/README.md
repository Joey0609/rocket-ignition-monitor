管理员可将脚本放在此目录，Compose 会将它只读挂载到 /opt/hooks。
示例指令：`/bin/sh /opt/hooks/on-ignition.sh`。
这里不包含默认执行的脚本。容器内指令只拥有 monitor 用户的权限；需要调用外部业务时，在镜像中安装相应客户端并配置凭据。
