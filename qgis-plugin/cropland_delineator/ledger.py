"""操作台账：标注 GPKG 旁边的 JSONL 侧车文件（<名>_ops.jsonl）。

每行一笔操作的 JSON 记录，整格底板上的破坏性编辑由此变得可重放：
    {"id":7,"cell":"海城市_2_6","seq":3,"kind":"road",
     "params":{"width":25.0},"geom":"LINESTRING(...)",
     "sess":2,"sidx":5,"ts":"2026-09-05T10:12:33"}

- seq：格内步号（同一事件的多行共享同一 seq，如 OSM 按路展开的一组）。
- kind：base=建底板 / baseline=存量格基线快照 / osm / road / rect / erase /
  add / split。重放时 seq 最小的一组（base/baseline）是重置起点。
- geom：图层 CRS 下的 WKT（base/baseline 也存实体几何，重放不依赖渔网现状）。
- sess/sidx：记录时（撤销栈会话号, 栈位），QGIS 撤销（Ctrl+Z）触发台账对齐
  丢弃——保证台账与实际几何状态不脱节。跨会话的历史行二者为 null。

纯 stdlib、无 QGIS 依赖，方便无头测试；写盘用 tmp+rename 原子替换。
"""

import json
import os
from datetime import datetime


class Ledger:
    def __init__(self, path):
        self.path = path
        self.rows = []
        self.load()
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()  # 空侧车立即可见

    # ---------- 读写 ----------

    def load(self):
        self.rows = []
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # 崩溃留下的半行：容忍
                if isinstance(row, dict) and "cell" in row and "kind" in row:
                    row.setdefault("seq", 0)
                    row.setdefault("params", {})
                    row.setdefault("geom", "")
                    row["sess"] = row.get("sess")
                    row["sidx"] = row.get("sidx")
                    self.rows.append(row)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in self.rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def add(self, row):
        """追加一行（自动补 id/ts），并立即落盘。"""
        row["id"] = max((r.get("id", 0) for r in self.rows), default=0) + 1
        row.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        self.rows.append(row)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---------- 查询 ----------

    def cell_rows(self, cell):
        return sorted((r for r in self.rows if r["cell"] == cell),
                      key=lambda r: (r["seq"], r.get("id", 0)))

    def next_seq(self, cell):
        return max((r["seq"] for r in self.rows if r["cell"] == cell),
                   default=0) + 1

    def steps(self, cell):
        """{seq: [同步各行]}，按 seq 升序。"""
        out = {}
        for r in self.cell_rows(cell):
            out.setdefault(r["seq"], []).append(r)
        return out

    def max_seq(self, cell):
        return max((r["seq"] for r in self.rows if r["cell"] == cell),
                   default=0)

    # ---------- 变更 ----------

    def truncate_after(self, cell, seq):
        """丢弃该格 seq 之后的行（回退已发生，未来的步作废）。"""
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if r["cell"] != cell or r["seq"] <= seq]
        self.save()
        return before - len(self.rows)

    def delete_step(self, cell, seq):
        """删除该格的某一步（该 seq 的全部行，OSM 组一步多行同删）。

        留下的 seq 有空洞没关系——重放按序遍历。返回删除行数。
        """
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if r["cell"] != cell or r["seq"] != seq]
        self.save()
        return before - len(self.rows)

    def reset_cell(self, cell):
        """重建底板=该格从头开始：清掉该格全部行。"""
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["cell"] != cell]
        self.save()
        return before - len(self.rows)

    def drop_after_stack(self, sess, sidx):
        """QGIS 撤销对齐：丢掉当前会话里栈位晚于 sidx 的行。跨会话行不动。"""
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if not (r.get("sess") == sess
                             and r.get("sidx") is not None
                             and r["sidx"] > sidx)]
        self.save()
        return before - len(self.rows)

    def update_params(self, cell, seq, params):
        """改某步参数（如道路宽度），返回受影响行数。"""
        n = 0
        for r in self.rows:
            if r["cell"] == cell and r["seq"] == seq:
                r["params"].update(params)
                n += 1
        if n:
            self.save()
        return n
