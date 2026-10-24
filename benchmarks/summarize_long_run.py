"""事后评估：发射边界只供计分，绝不进入run_full_video.py。"""

import base64, csv, gzip, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
NAMES = {
    "cz12_day": "长征十二号遥二 · 白天",
    "cz8a_day": "长征八号甲遥三 · 白天（P1+P2）",
    "cz8a_night": "长征八号甲遥11 · 夜间",
}


def duration(s):
    n = int(round(s))
    return f"{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}"


def main():
    run = ROOT / "full_run_v3"
    summary = json.loads((run / "summary.json").read_text())
    if not summary["complete"]:
        raise RuntimeError("Run incomplete")
    gt = {
        m["id"]: m
        for m in json.loads((ROOT / "groundtruth.json").read_text())["missions"]
    }
    cfg = summary["config"]
    stats = {
        s["id"]: {
            "id": s["id"],
            "name": NAMES[s["id"]],
            "bvid": s["bvid"],
            "pages": s["pages"],
            "duration_s": s["end_s"] - s["start_s"],
            "prelaunch_s": gt[s["id"]]["prelaunch_end_local_s"],
            "uncertain_boundary_s": gt[s["id"]]["ignition_visible_by_local_s"]
            - gt[s["id"]]["prelaunch_end_local_s"],
            "images": 0,
            "pre_images": 0,
            "pre_unknown_images": 0,
            "pre_positive_images": 0,
            "pre_candidate_cancellations": 0,
            "pre_confirmed_alerts": [],
            "valid_after_confirmations": [],
            "retracted_alerts": 0,
        }
        for s in cfg["segments"]
    }
    active = {s["id"]: {} for s in cfg["segments"]}
    with gzip.open(run / "predictions.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            st = stats[r["segment"]]
            st["images"] += 1
            t = float(r["local_s"])
            before = t < st["prelaunch_s"]
            if before:
                st["pre_images"] += 1
                st["pre_unknown_images"] += int(r["event"] in {"", "None"})
                st["pre_positive_images"] += int(r["event"] == "1")
                st["pre_candidate_cancellations"] += int(
                    r["action"] == "candidate_cancelled"
                )
            if r["action"] == "confirmed":
                event = {
                    "sample": int(r["sample"]),
                    "global_s": float(r["global_s"]),
                    "local_s": t,
                    "decision_id": int(r["decision_id"]),
                }
                active[r["segment"]][event["decision_id"]] = event
                if before:
                    st["pre_confirmed_alerts"].append(event)
            elif r["action"] == "retracted":
                active[r["segment"]].pop(int(r["decision_id"]), None)
                st["retracted_alerts"] += 1
    for key, st in stats.items():
        st["valid_after_confirmations"] = [
            e for e in active[key].values() if e["local_s"] >= st["prelaunch_s"]
        ]
        st["pre_observable_fraction"] = 1 - st["pre_unknown_images"] / st["pre_images"]
    totals = {
        "video_seconds": summary["processed_seconds"],
        "processed_images": summary["processed_images"],
        "fps": summary["sample_fps"],
        "prelaunch_seconds": sum(s["prelaunch_s"] for s in stats.values()),
        "uncertain_boundary_seconds": sum(
            s["uncertain_boundary_s"] for s in stats.values()
        ),
        "pre_images": sum(s["pre_images"] for s in stats.values()),
        "pre_unknown_images": sum(s["pre_unknown_images"] for s in stats.values()),
        "pre_confirmed_false_alerts": sum(
            len(s["pre_confirmed_alerts"]) for s in stats.values()
        ),
        "pre_positive_images": sum(s["pre_positive_images"] for s in stats.values()),
        "hit_missions": sum(
            bool(s["valid_after_confirmations"]) for s in stats.values()
        ),
        "total_missions": len(stats),
    }
    evaluation = {
        "totals": totals,
        "missions": list(stats.values()),
        "strict_false_alert_rule": "Every confirmed action before earliest visible ignition counts, including ones later retracted. Candidate cancellations are not emitted alerts.",
        "limitations": [
            "Rules revised after seeing false alarms in these same videos; this is a fitted full replay, not unseen-source validation.",
            "Finite observation cannot guarantee zero false alerts in future RTMP streams.",
            "Warmup/camera-change images return unknown, and are disclosed; no ignition-time-based arming.",
            "Visual boundary uncertainty totals 11 seconds, not telemetry truth.",
            "Inference uses all10FPS images from the full joined H264 file at320x180; source is not60FPS.",
            "Video has no audio track; only images were downloaded for this task.",
        ],
    }
    (ROOT / "long_evaluation.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2)
    )
    rows = ""
    for st in stats.values():
        rows += f"<tr><td>{st['name']}</td><td>{len(st['pages'])}</td><td>{duration(st['duration_s'])}</td><td>{duration(st['prelaunch_s'])}</td><td>{len(st['pre_confirmed_alerts'])}</td><td>{'检出' if st['valid_after_confirmations'] else '未检出'}</td><td>{st['pre_unknown_images']}</td></tr>"
    cards = ""
    for st in stats.values():
        ev = (
            st["valid_after_confirmations"][0]
            if st["valid_after_confirmations"]
            else (st["pre_confirmed_alerts"][0] if st["pre_confirmed_alerts"] else None)
        )
        if ev:
            p = run / f"confirmed_{st['id']}_{ev['sample']:07d}.jpg"
            if p.exists():
                cards += f'<figure><img src="data:image/jpeg;base64,{base64.b64encode(p.read_bytes()).decode()}"><figcaption>{st["name"]}：确认检出画面</figcaption></figure>'
    old = ROOT / "full_run_v1/confirmed_cz12_day_0006373.jpg"
    failure = ""
    if old.exists():
        failure = f'<img style="max-width:480px;width:100%" src="data:image/jpeg;base64,{base64.b64encode(old.read_bytes()).decode()}">'
    sources = "".join(
        f'<li><a href="https://www.bilibili.com/video/{s["bvid"]}/">{s["name"]}</a> · 全部分P：{", ".join(map(str, s["pages"]))}</li>'
        for s in stats.values()
    )
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SpaceLens长时段点火误报测试</title><style>body{{background:#0b121d;color:#e8edf4;font:16px/1.8 system-ui,sans-serif;margin:0}}main{{max-width:1120px;margin:auto;padding:36px 24px}}h1{{font-size:33px;line-height:1.35}}h2{{margin-top:32px}}p{{color:#bfccdd}}a{{color:#81cfff}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card{{padding:20px;background:#152235;border-radius:14px;border:1px solid #30445d}}.big{{font-size:32px;font-weight:700;color:white}}.note{{background:#302315;border-left:4px solid #efab57;padding:18px;margin:22px 0}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{padding:11px;border-bottom:1px solid #30415a;text-align:left}}th{{color:#92badf}}.scroll{{overflow:auto}}.shots{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}figure{{margin:0;background:#142032;padding:10px;border-radius:12px}}figure img{{width:100%}}figcaption{{font-size:13px;color:#b6cce4}}code{{color:#a7d9ff}}.small{{font-size:13px;color:#9db3ca}}@media(max-width:650px){{.cards,.shots{{grid-template-columns:1fr}}main{{padding:24px 14px}}h1{{font-size:27px}}}}</style><main><p class="small">SpaceLens 国内发射 · 完整拼接视频 · 2026-10-25</p><h1>长时段点火识别：发射前是否误报</h1><div class="cards"><div class="card"><div class="big">{duration(totals["video_seconds"])}</div>拼接视频 · {totals["processed_images"]:,}张图片</div><div class="card"><div class="big">{duration(totals["prelaunch_seconds"])}</div>发射前累计时长</div><div class="card"><div class="big">{totals["pre_confirmed_false_alerts"]} 次 / {totals["hit_missions"]}/{totals["total_missions"]}</div>发射前确认误报 / 发射区间检出</div></div><div class="note">本结果来自看过失败案例后修改规则，再对同一完整视频从头回放，不是盲测。零误报只适用于下表这些实际测试素材，不能保证以后任意直播绝对不误报。</div><h2>完整来源与逐场结果</h2><div class="scroll"><table><thead><tr><th>发射任务</th><th>分P数</th><th>视频长度</th><th>点火前时长</th><th>确认误报</th><th>发射区间</th><th>点火前未知图片</th></tr></thead><tbody>{rows}</tbody></table></div><p>按投稿全部分P核对、下载；长八甲遥三P1和P2连续拼接，检测状态不在两P之间人为重置。只在不同发射任务之间新建状态机。检测从每场片头开始，不读取已知点火时间来控制启停。</p><p>发射前每一次confirmed动作都计误报，即使随后撤销也不能消掉这次误报；疑似取消不属于已发出报警。点火边界为初步视觉标注，三场合计有{totals["uncertain_boundary_seconds"]:.0f}秒过渡不确定区间。共{totals["pre_unknown_images"]:,}张点火前图片处于背景初始化或相机变更后的未知状态，单列披露；没有把它们当作“明确未点火”判断。</p><h2>确实识别到了发射</h2><div class="shots">{cards}</div><p>评价标准是发射区间内出现至少一次有效且未撤销的确认；不要求找准第一帧，也不回溯准确点火时刻。检测的是可见点火，不单独证明离架。</p><h2>长录像发现的问题与改动</h2><p>旧规则在长十二白天片段的第10分37秒附近，将放气水汽及背景变化误作点火，失败图片保留如下。V1/V2是发现错误后停止的诊断运行，不冒称完整验证；只有V3完整结束才计入上表。</p>{failure}<p>V3仍完全使用传统视觉：限定主直播画面，HSV暖色与白色亮区、连通域、原有灯光排除、相对背景增亮、8/10持续确认。新增缓慢光照变化的背景更新（每张alpha0.02，有较强火焰证据时冻结），以及上半部背景边缘的相位相关，在相机平移超过2.5像素且相关响应足够时重建背景。不能单靠“很亮”确认火焰，必须有新增高饱和暖色成分。</p><p>完整拼接文件为852×480的H264、无音轨；按10FPS解码并缩到320×180逐张处理，没有跳过长时间负样本。完整解码应无错误，日志、逐图片预测、配置和输入SHA256随程序包交付。观看版如果提供，只用于便捷观看，严格复现请使用校验值一致的完整MP4。</p><h2>复现</h2><p>下载原文件的两个分卷后，放在同一目录，用<code>python merge_video.py 下载目录</code>合并并校验，得到单个<code>spacelens_day_night_full.mp4</code>。再运行<code>python run_full_video.py --config run_config_v3.json --output my_replay</code>。检测脚本不读取groundtruth.json，后者只用于事后统计误报。</p><h2>原投稿</h2><ul>{sources}</ul><p class="small">来源作品权利归SpaceLens及相应创作者；本次资料用于私下离线验证，未公开发布视频。未来仍需新的长时段盲测；本轮未连接RTMP。</p></main></html>"""
    (ROOT / "spacelens_long_test_report.html").write_text(body)
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
