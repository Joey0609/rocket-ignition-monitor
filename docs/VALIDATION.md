# 验证记录

日期：2026-10-25。Python 3.12，Linux，依赖版本见 requirements.txt。

## 自动检查

- `python -m pytest -q`：29 个测试通过。覆盖连续确认、候选取消、切镜/时间间隔保护、背景恢复后自动撤销、人工撤销、指令持久去重、超时及失败记录、未授权访问、跨域写入、上传文件路径、真实 FFmpeg 解码、结束与停止重启。
- 自动纠错端到端测试：合成短视频先出现持续橙色区域，再恢复原背景。一次 confirmed 和一次 retracted，分别执行测试命令，文件中恰有 `CR` 两个字符。它验证纠错链路，不代表真实火箭误判纠正准确率。
- `python scripts/http_smoke.py`：真实 Uvicorn 服务启动后，经 HTTP 登录、上传视频、建立 WebSocket、读取 MJPEG JPEG 数据、确认/人工撤销、检查两次测试指令与事件计数。结果见 [http-smoke.json](http-smoke.json)。
- `python scripts/rtmp_smoke.py rtmp://127.0.0.1:19355/rocket`：官方 MediaMTX v1.21.2，FFmpeg 合成 60 FPS RTMP 推流；检测按 10 FPS，确认/撤销测试命令各执行一次，断流进入重连。结果见 [rtmp-smoke.json](rtmp-smoke.json)。测试需要先在同一主机启动 MediaMTX，监听对应端口和 `rocket` 路径。
- Ruff 格式与未使用符号检查、JavaScript 语法检查通过。

## 网页检查范围

云端网页预览环境没有 Python 运行依赖，无法在那里直接启动 FastAPI。使用隔离的静态视觉夹具检查了桌面监测台布局、真实发射画面与 ROI 叠层、确认状态、动态记录；该夹具不提交到仓库，不作为后端已联通的证据。后端实际 HTTP / Cookie / WebSocket / MJPEG 联通由上面的独立真实服务脚本验证。

浏览器完整交互与服务器同环境的端到端联调、手机实机、Docker 镜像构建、长时间多人并发未在本环境完成。Docker Compose 与部署文件已提供，不能将“配置已写好”描述为这些环境均已验证。

## 真实长视频

见 [benchmark.json](benchmark.json) 和 [复现方法](../benchmarks/README.md)：3 个发射任务、4 个分 P、9:21:44.9、337,049 张图片、严格发射前 7:54:56、0 次错误确认、3/3 区间检出、208 张发射前图片未知。

这些视频参与了规则修正。结果仅对本次已测试素材成立，不是对新源的准确率保证。参考运行没有已确认事件被自动撤销的真实样本，自动撤销在合成序列和服务指令链路中验证。
