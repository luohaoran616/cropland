"""SAM 点选分割常驻 worker：stdin/stdout 各一行 JSON。

协议：
  → {"cmd":"embed","npy":"/abs/cell.npy"}          图片=uint8 HxWx3（原始分辨率）
  ← {"ok":true,"h":H,"w":W,...}
  → {"cmd":"predict","points":[[c,r],...],"labels":[1|0,...],"fresh":bool}
  ← {"ok":true,"mask":"<base64 np.packbits>","score":s,"h":H,"w":W}
  → {"cmd":"ping"}                                   ← {"ok":true,"pong":true}

首点（fresh=1）多掩码取分最高者并记其低分辨率 logits；后续点把 logits
作为 mask_input 迭代细化——这是 segment_anything 官方 notebook 的标准
交互式用法。mask_input 只在同一次点选会话内传递，落地/取消后由 fresh 重置。

运行环境：nn/.venv（torch + segment-anything）。权重：ckpt/sam_vit_b_01ec64.pth。
用法：.venv/bin/python sam_click.py <ckpt路径>
"""
import base64
import json
import os
import sys

# 自愈路径：确保本仓库 venv 的 site-packages 在 sys.path 里。
# 实测有"同一解释器命令行可导入、被 QGIS 拉起却 ModuleNotFoundError"的灵异
# 场景（混合布局 venv / 环境变量残留等），显式注入一劳永逸。
_here = os.path.dirname(os.path.abspath(__file__))
for _sp in (os.path.join(_here, ".venv", "Lib", "site-packages"),
            os.path.join(_here, ".venv", "lib", "site-packages"),
            os.path.join(_here, ".venv", "lib",
                         "python%d.%d" % sys.version_info[:2], "site-packages")):
    if os.path.isdir(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

try:
    import numpy as np
    import torch
    from segment_anything import SamPredictor, sam_model_registry
except ImportError as _e:  # 死也要死明白：把现场打到 stderr 给插件日志
    sys.stderr.write(
        "import 失败：%r\n  executable=%s\n  prefix=%s\n  sys.path=%s\n"
        % (_e, sys.executable, sys.prefix, sys.path))
    sys.stderr.flush()
    raise


def main(ckpt):
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    sam = sam_model_registry["vit_b"](checkpoint=ckpt)
    sam.to(device=dev)
    pred = SamPredictor(sam)
    logits = None  # 当前点选会话的低分辨率 logits（1,256,256）

    def reply(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    reply({"ok": True, "event": "ready", "device": dev})

    for line in sys.stdin:
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "ping":
                reply({"ok": True, "pong": True})
            elif cmd == "embed":
                img = np.load(req["npy"])
                pred.set_image(img[:, :, :3])
                logits = None
                h, w = img.shape[:2]
                reply({"ok": True, "h": int(h), "w": int(w)})
            elif cmd == "predict":
                pts = np.asarray(req["points"], dtype=np.float32)
                labels = np.asarray(req["labels"], dtype=np.int64)
                fresh = bool(req.get("fresh"))
                masks, scores, low = pred.predict(
                    point_coords=pts,
                    point_labels=labels,
                    mask_input=None if fresh or logits is None else logits,
                    multimask_output=fresh or logits is None,
                )
                k = int(np.argmax(scores)) if masks.shape[0] > 1 else 0
                mask = np.asarray(masks[k], dtype=bool)
                logits = np.asarray(low[k:k + 1], dtype=np.float32)
                reply({
                    "ok": True,
                    "mask": base64.b64encode(np.packbits(mask)).decode("ascii"),
                    "score": round(float(scores[k]), 4),
                    "h": int(mask.shape[0]),
                    "w": int(mask.shape[1]),
                })
            else:
                reply({"ok": False, "error": f"unknown cmd: {cmd}"})
        except Exception as exc:  # 单条请求出错不死进程
            try:
                reply({"ok": False, "error": repr(exc)})
            except Exception:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  "ckpt/sam_vit_b_01ec64.pth"))
