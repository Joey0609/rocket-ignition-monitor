# SpaceLens 完整回放复现

这里保存的是来源清单、校验值、标注和处理代码；不公开提交下载视频或作者画面。

1. 使用私下交付的 `.001`、`.002` 两个分卷，运行 `python benchmarks/merge_video.py /path/to/volumes`，验证并合并单个 MP4。
2. 将合并后的 `spacelens_day_night_full.mp4` 放在本目录。不要使用重新编码的观看版复现基准。
3. 在仓库根目录运行：

```bash
python benchmarks/run_full_video.py --config run_config_v3.json --output full_run_v3
python benchmarks/summarize_long_run.py
```

第一条命令不读取 `groundtruth.json`，只在不同发射任务边界重建状态机，同一投稿分 P 之间不重置。解码失败会报错，不生成完整成功标志。第二条事后评分，生成 `long_evaluation.json` 及带识别截图的本机 HTML 报告。

`reference_summary.json` 是此次已完成运行的记录，逐图片 gzip CSV 和完整带图报告随私下资料包交付。原视频 SHA256 见 `video_identity.json`，每个分 P 校验见 `main_manifest.json`。原始 Bilibili CDN 链接会过期，未将临时下载令牌写入仓库；来源投稿地址均在清单内。

这是离线高速回放评估；网页服务为实时速率。两者复用同一个图片证据模块、机位保护和状态机，服务另有断流与调度间隔保护。视觉标注不确定过渡段不计入严格发射前统计；未知图片单列。每次错误确认都计误报，撤销不能抹去误报记录。规则曾根据同一批录像修改，不是盲测。

## 发动机初始火光复评

`engine_glow_labels.json` 以发动机首次可见火光、亮芯或局部点火照亮为参考，不等待起飞尾焰。`evaluate_engine_glow.py` 只做离线事后评分，不向检测器提供标注。原有 groundtruth.json 保留为历史记录，不能用其中“火焰已明显露出”的较宽边界判断是否及时。

```bash
python benchmarks/evaluate_engine_glow.py --predictions /path/to/predictions.csv.gz --out docs/engine-glow-audit.json
```

当前 V3 约延迟 5.3、3.9、15.8 秒。最终区间命中 3/3 不代表早期点火能力通过。

生成私下带图报告可先安装 Pillow，再运行 `build_engine_glow_report.py --research /path/to/evidence --video /path/to/spacelens_day_night_full.mp4 --out /path/to/report.html`。该脚本按人工标注提取对比帧，仅生成报告，不参与检测。
