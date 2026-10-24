"""Build an inline-image audit report from local, private source-video evidence."""

import argparse, base64, json, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

p = argparse.ArgumentParser()
p.add_argument("--research", type=Path, required=True)
p.add_argument("--video", type=Path, required=True)
p.add_argument("--out", type=Path, required=True)
args = p.parse_args()
project = Path(__file__).resolve().parents[1]
audit = json.loads((project / "docs/engine-glow-audit.json").read_text())
font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
font = (
    ImageFont.truetype(str(font_path), 17)
    if font_path.exists()
    else ImageFont.load_default()
)
font_small = (
    ImageFont.truetype(str(font_path), 14)
    if font_path.exists()
    else ImageFont.load_default()
)
figures = []
for m in audit["missions"]:
    key = m["id"]
    (args.research / key).mkdir(parents=True, exist_ok=True)
    late = args.research / key / f"confirmed_{m['confirmed_global_s']:.1f}.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(m["confirmed_global_s"]),
            "-i",
            str(args.video),
            "-frames:v",
            "1",
            str(late),
        ],
        check=True,
    )
    times = [
        m["last_no_glow_global_s"],
        m["first_glow_global_s"],
        m["confirmed_global_s"],
    ]
    files = [
        args.research / key / f"global_{times[0]:.1f}.jpg",
        args.research / key / f"global_{times[1]:.1f}.jpg",
        late,
    ]
    for file, t in zip(files, times):
        if not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(t),
                    "-i",
                    str(args.video),
                    "-vf",
                    "fps=10",
                    "-frames:v",
                    "1",
                    str(file),
                ],
                check=True,
            )
    labels = [
        "BEFORE ENGINE GLOW",
        "FIRST VISIBLE ENGINE GLOW",
        "V3 ACTUAL CONFIRMATION",
    ]
    sheet = Image.new("RGB", (1020, 470), "#f5f5f2")
    draw = ImageDraw.Draw(sheet)
    for i, (file, t, label) in enumerate(zip(files, times, labels)):
        im = Image.open(file)
        x = i * 340
        sheet.paste(im.resize((340, 192)), (x, 0))
        crop = im.crop((350, 350, 490, 465)).resize(
            (280, 230), Image.Resampling.NEAREST
        )
        sheet.paste(crop, (x + 30, 197))
        draw.text((x + 9, 432), label, font=font_small, fill="#222222")
        draw.text((x + 9, 451), f"joined video: {t:.1f} s", font=font, fill="#222222")
    panel = args.research / f"{key}_engine_glow_comparison.jpg"
    sheet.save(panel, quality=94)
    figures.append(
        f'<figure><img src="data:image/jpeg;base64,{base64.b64encode(panel.read_bytes()).decode()}"><figcaption>{m["name"]}：左为初始火光前，中为首次可见火光，右为 V3 实际确认。下半幅是同一发动机区域的最近邻放大，没有补画火焰。</figcaption></figure>'
    )
rows = "".join(
    f"<tr><td>{m['name']}</td><td>{m['last_no_glow_global_s']:.1f}–{m['first_glow_global_s']:.1f}</td><td>{m['confirmed_global_s']:.1f}</td><td>{m['delay_lower_s']:.1f}–{m['delay_upper_s']:.1f} 秒</td><td>{m['pre_glow_false_confirmations']}</td></tr>"
    for m in audit["missions"]
)
html = f"""<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>发动机初始火光识别复评</title><style>body{{margin:0;background:#f4f5f1;color:#223329;font:15px/1.85 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:32px 22px}}h1{{font-size:30px}}h2{{font-size:21px;margin-top:32px}}.verdict{{border-left:5px solid #b44b32;padding:16px 20px;background:#fce8df}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #cfd7cc;text-align:left;padding:11px 9px}}figure{{margin:24px 0}}img{{width:100%;height:auto;border:1px solid #d2d9ce}}figcaption{{font-size:12px;color:#64735f}}code{{background:#e9ede4;padding:2px 5px}}li{{margin:6px 0}}</style><main><p>SpaceLens 国内发射 · 2026-10-25 · 更正报告</p><h1>发动机出现火光即算点火：确认究竟晚了多久</h1><div class="verdict"><strong>撤回此前“3/3 即证明点火识别通过”的结论。</strong><br>V3 在三场中的确认分别比初始发动机火光晚约 5.3、3.9、15.8 秒。最终区间命中不能掩盖迟报。当前已发布 V3 尚未证明符合早期点火识别要求。</div><h2>评价口径</h2><p>发动机底部初始火光、亮芯，或可归因于发动机点火的局部照亮首次可见，就视为点火已发生。不等待大片起飞尾焰露出，也不等待离架。原视频以 852×480、10 FPS 相邻画面复核；下列时间只用于离线评分，线上不回溯补发。</p><p>标注来自单人视觉复核，不是发动机遥测真值。0.1–0.2 秒范围是采样画面中可辨视觉变化的区间，不表示物理点火时间具有同样精度。</p><h2>已发布 V3 的完整回放复评</h2><div class="scroll"><table><thead><tr><th>任务</th><th>初始火光参考区间 / 拼接视频秒</th><th>实际确认 / 拼接视频秒</th><th>确认延迟</th><th>发射前错误确认</th></tr></thead><tbody>{rows}</tbody></table></div><p><strong>1 秒内确认：0/3；2 秒内确认：0/3；5 秒内确认：1/3。</strong>这些只是迟报程度分档，并非用户已约定的验收时限。最终区间命中仍为 3/3，但不能作为及时点火识别通过的结论。</p><p>完整视频 9:21:44.9，共 337,049 张图片；复评后的发射前观察约 7:54:58，284,980 张采样图片中 208 张返回未知，错误确认 0 次。每次发射前确认均计误报，后来撤销也不删除。本次重新检查连续画面并重算已有完整逐帧记录，没有把复评包装成“新算法又完整跑完 9 小时”。</p><p>以上仅是解码后视频时间延迟，未包含 RTMP 传输、解码缓冲、网页通知和执行命令的时间，实际在线提示可能更晚。</p><h2>初始发动机火光与旧版确认画面对比</h2>{"".join(figures)}<h2>迟报原因</h2><ol><li>旧检测区域下边界在画面高度 83%，这几场初始火光主要位于约 85%–93%，一部分早期证据在区域外。</li><li>缩到 320×180 并做 3×3 开运算，会丢掉小面积火光；宽区域面积归一化及确认阈值更容易在大火焰出现后才通过。</li><li>暖色条件可能忽略白色亮芯，静态灯光排除可能压掉亮区附近新火光。</li><li>弱候选固定等待超时可能在强证据开始增强时取消候选，再进入冷却。</li></ol><p>这些是实现与画面定位结果，不代表每个因素的独立影响均已做消融验证。</p><h2>修正尝试没有被冒称成功</h2><p>本轮尝试了原尺寸局部发动机区域、较小连通域、白色亮芯和更快确认。短片中一个试验配置可将长十二确认推到初始火光后约 0.2–0.4 秒，但放回发射前录像后，两种试验配置分别在片头 17.8 秒和 63.3 秒错误确认。失败后停止该尝试，未完整跑完，也未替换线上 V3。不能宣称已经修好或同时实现早检与零误报。</p><h2>验证范围</h2><p>此前 29 项服务/状态机测试及 HTTP、60 FPS RTMP 链路记录仍是软件链路证据，不能用来证明初始发动机火光识别及时。新增四项离线评价逻辑测试通过。真实自动撤销准确率尚无独立实景样本支持。这些录像曾参与调参，均不是盲测。</p><p>逐场标注、确认延迟与时效评分脚本已保存到工程，供复核。原有区间命中统计仅作为历史记录保留。</p></main></html>"""
args.out.write_text(html)
print("corrected report rendered", args.out)
