"""将两个下载分卷还原为已检测的单个MP4，并验证字节校验值。"""

import argparse, hashlib, json, pathlib

p = argparse.ArgumentParser()
p.add_argument("directory", nargs="?", default=".")
a = p.parse_args()
root = pathlib.Path(a.directory)
# 文件与本脚本、video_identity.json、download_volumes.json放在同一目录。
meta = pathlib.Path(__file__).resolve().parent
parts = json.loads((meta / "download_volumes.json").read_text())
identity = json.loads((meta / "video_identity.json").read_text())
dest = root / identity["filename"]
if dest.exists():
    raise SystemExit(f"Output already exists: {dest}")
whole = hashlib.sha256()
tmp = dest.with_suffix(".assembling")
try:
    with tmp.open("wb") as out:
        for part in parts:
            src = root / part["name"]
            h = hashlib.sha256()
            size = 0
            with src.open("rb") as inp:
                while data := inp.read(8 * 1024 * 1024):
                    out.write(data)
                    h.update(data)
                    whole.update(data)
                    size += len(data)
            if size != part["bytes"] or h.hexdigest() != part["sha256"]:
                raise RuntimeError(f"Invalid volume: {src.name}")
    if whole.hexdigest() != identity["sha256"]:
        raise RuntimeError("Full video hash mismatch")
    tmp.replace(dest)
    print("Restored and verified:", dest)
except Exception:
    if tmp.exists():
        tmp.unlink()
    raise
