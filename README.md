# Ignition · 火箭点火监测服务器

接入 RTMP / RTSP 直播或上传视频，网页实时预览；以 **10 FPS** 运行传统视觉检测和纠错状态机。确认点火、撤销确认时，可以分别执行管理员配置的服务器指令。无需深度学习模型、GPU 或第三方 AI API。

这是独立的 Python 服务器。一个进程监测一路输入，关闭网页不会停止监测；多个网页共享同一任务。新任务是新的识别区间，不回溯或估计精确点火时刻。

## 本机启动

需要 Linux / macOS、Python 3.12+、FFmpeg / FFprobe。Windows 推荐 WSL2。

```bash
# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y ffmpeg python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 http://127.0.0.1:8000 ，使用 `cat data/admin-token` 查看首次启动自动生成的管理令牌，再在网页登录。也可用环境变量 `ADMIN_TOKEN` 指定令牌。不要把令牌提交到仓库。

1. 选择「直播流」，填写 RTMP / RTSP 地址；或选择「视频文件」上传视频。
2. 在「检测区域与执行指令」中设置 ROI 和两类指令。默认只检测，启用事件指令后才执行。
3. 点击「开始监测」。文件按原始播放速度处理，结束后自动停止；直播断开后自动重连。
4. 点火确认时页面出现提示并记录事件。若确认错误，可点击「撤销此确认」，触发对应补偿指令。
5. 开始新任务或变更机位/ROI 前先停止。新任务重新建立背景，默认 ROI 适配此次 SpaceLens 主画面布局，应根据实际画面调整。

```bash
# 运行在服务器上的业务脚本示例，填入网页指令配置
/bin/sh /opt/hooks/on-ignition.sh
/bin/sh /opt/hooks/on-retract.sh
```

命令收到环境变量 `IGNITION_JOB_ID`、`IGNITION_DECISION_ID`、`IGNITION_ACTION`（`confirmed` / `retracted`）。事件字段不会被拼接到 shell 脚本中。输出最多保留 8 KB，超时默认 10 秒、最多 120 秒；最多同时执行 4 个命令。完成/超时后清理该命令的子进程组，请通过系统服务管理器启动需要常驻的业务。

同一 `(job_id, decision_id, action)` 在 SQLite 持久去重，最多派发一次。服务器崩溃时已领取的命令不会自动重试；这不等同于业务效果“恰好一次”。需要可靠事务的业务脚本应自行使用事件键做幂等。撤销命令执行补偿操作，不会自动逆转确认指令造成的所有效果。指令失败、队列满、超时均会在任务动态中展示。

## Docker Compose 与 RTMP 推流

```bash
docker compose up -d --build
docker compose exec monitor cat /app/data/admin-token
```

网页：http://127.0.0.1:8000 。默认端口仅绑定本机。

- OBS 推流服务器：`rtmp://127.0.0.1:1935`，串流密钥：`rocket`。
- 或完整推流地址：`rtmp://127.0.0.1:1935/rocket`。
- **网页内填写**：`rtmp://mediamtx:1935/rocket`（监测服务在 Compose 网络内拉流）。
- 已有外部 RTMP 地址可直接填写，不必经过 MediaMTX。
- Compose 的指令在容器内由非 root 用户运行；`./hooks` 只读挂载到 `/opt/hooks`。本机安装的指令在宿主机服务账号下运行。

Compose 固定 MediaMTX `1.21.2`。推流入口仅开放 `rocket` 路径，默认仅在本机可达。远端推流可通过 SSH 隧道进入；如修改 `RTMP_BIND` / `WEB_BIND` 对外开放，应在可信网络使用，并配置流服务鉴权及网页 HTTPS 反向代理。这里的管理令牌不负责 MediaMTX 的 RTMP 推流鉴权。

```bash
# FFmpeg 推流示例
ffmpeg -re -i launch.mp4 -c:v libx264 -preset veryfast -tune zerolatency -an -f flv rtmp://127.0.0.1:1935/rocket
```

## 检测与误判纠正

检测链路：FFmpeg 采样 10 FPS → 320×180 BGR 图片 → 暖色/高亮连通域 → 曝光补偿、静态灯光抑制、缓慢背景更新 → 机位平移保护 → 状态机。

- 3/4 帧达到弱阈值进入疑似阶段；不会执行点火指令。
- 最近 10 帧中至少 8 帧达到强阈值，且持续至少 0.6 秒，才确认点火。
- 疑似消失/证据不够时取消候选并短暂冷却。
- 确认后 3 秒内，连续 8 帧低证据、且同机位画面恢复点火前背景时自动撤销；只因火光被烟遮住不会撤销。
- 人工撤销可用于补充误判反馈。每次撤销与原 decision_id 关联；再出现足够证据可以形成新确认。
- 切镜、断流、初始化、处理间隔过大时显示暂不可判定。真实点火发生在这些阶段可能漏检。重连保留任务确认记录，不会因连接恢复而重复确认。

分数是视觉证据，**不是置信概率**。白烟、灯光、曝光、导播回放、其他画中画火箭、ROI 内火源等都可能影响判断。需要先接入点火前的稳定画面建立背景；接入时火焰已经存在，可能被当作背景。算法不理解“当前直播”与“历史回放”的语义。

浏览器不直接解码 RTMP，服务端转为 MJPEG 预览。为保持已验证的图片处理路径，当前预览为 320×180 / 10 FPS、无音频；适合确认接入与检测区域，非全清直播播放器。页面动画独立于视频帧率，并遵循减少动态效果的系统偏好。

## 已完成验证

SpaceLens 三场国内发射，4 个分 P 完整下载并拼接，**9:21:44.9**，处理 **337,049 张图片**。长征八号甲遥三 P1 → P2 之间保持同一任务状态机。

|任务|场景|完整时长|区间检出|发射前错误确认|
|---|---|---:|---:|---:|
|[长征十二号遥二](https://www.bilibili.com/video/BV1sAtFzbEpo)|白天|2:24:34|1/1|0|
|[长征八号甲遥三](https://www.bilibili.com/video/BV1WT8qzMEsM) P1+P2|白天|4:32:18|1/1|0|
|[长征八号甲遥11](https://www.bilibili.com/video/BV1t7hb6GEt4)|夜间|2:24:53|1/1|0|

合计严格发射前区间 **7:54:56**，错误确认 **0 次**，208 帧返回未知；视觉边界不确定区间合计 11 秒，单列排除。任何发射前确认即计误报，即使后来撤销也不能从误报统计中删除。目标是每个区间至少一次未撤销的有效确认，不评精确点火时刻。

**这些视频也用于修正规则，是拟合后完整回放，不是盲测。3/3 不能证明未来直播准确率 100%，也不能保证以后零误报。** 真实长视频中没有出现“已确认后自动撤销”的实例；该路径由合成图片和指令联调验证。长视频来自平台提供的码率版本，源视频并非 60 FPS；另以合成 60 FPS RTMP 验证采样和指令链路，不能混作真实发射准确率数据。下载视频不随源码发布。

详见 [验证记录](docs/VALIDATION.md)、[可机读统计](docs/benchmark.json)。

## 配置与维护

|环境变量|默认|说明|
|---|---|---|
|`DATA_DIR`|`./data`|令牌、配置、事件库、上传视频|
|`ADMIN_TOKEN`|首次随机生成|管理令牌；设置后覆盖文件令牌|
|`MAX_UPLOAD_MB`|4096|单次上传上限 MB|

仅启动 **1 个 Uvicorn worker / 1 个服务实例**；SQLite 事件、任务、预览共用单实例状态。反向代理须转发 WebSocket、保留 Host/Origin，设置上传上限，关闭 `/preview.mjpg` 的代理缓冲。TLS 在反向代理终止时，应只信任指定代理并正确传递 `X-Forwarded-Proto`。配置和录入的指令保存在本机权限受限文件中，包含敏感业务指令时应保护数据目录。

上传文件保存在随机 ID 路径，不使用用户文件名作为磁盘路径。任务记录最多展示最近 120 条；SQLite 和上传目录持续保留，需按业务保留周期归档清理。事件导出不包含 RTMP URL 的口令/查询令牌。重启服务不会自动恢复上次拉流或补跑命令。

```bash
pip install pytest httpx
python -m pytest -q
# 单张图片或图片序列路径也可脱离服务器运行
python run_images.py /path/to/images --roi 0.03,0.10,0.80,0.83 --out result.json
```

官方参考：[FastAPI 流式响应](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse)、[WebSocket](https://fastapi.tiangolo.com/advanced/websockets/)、[FFmpeg 协议](https://ffmpeg.org/ffmpeg-protocols.html)、[fps filter](https://ffmpeg.org/ffmpeg-filters.html#fps)、[MediaMTX 安装](https://mediamtx.org/docs/kickoff/install)。
