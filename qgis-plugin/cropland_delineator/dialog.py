"""Main dialog: pick imagery + fishnet layers, run delineation for selected cells."""

import os
import re
import signal
import tempfile
import shutil

from qgis.PyQt.QtCore import Qt, QProcess
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QGroupBox,
    QLineEdit,
    QToolButton,
    QProgressBar,
    QPlainTextEdit,
    QLabel,
    QFileDialog,
    QMessageBox,
)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsVectorFileWriter,
    QgsCoordinateTransform,
    QgsWkbTypes,
    Qgis,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel

from . import inference

PLUGIN_ROOT = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.dirname(PLUGIN_ROOT)
INFERENCE_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
INFERENCE_WORKER = os.path.join(PROJECT_ROOT, "inference_worker.py")
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "DelineateAnythingv2.pt")


class DelineateDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.proc = None
        self.queue = []          # [(cell_id, cutline_gpkg, workdir, out_gpkg, json_path)]
        self.results = []
        self.total_cells = 0

        missing = inference.check_environment(
            INFERENCE_PYTHON, INFERENCE_WORKER, MODEL_PATH
        )
        if missing:
            raise RuntimeError(
                "推理环境不完整，缺少：\n" + "\n".join(missing)
            )

        self.setWindowTitle("Cropland Delineator — 样方耕地矢量化")
        self.setMinimumWidth(560)
        self.setup_ui()

    # ---------- UI ----------

    def setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.cbo_raster = QgsMapLayerComboBox()
        self.cbo_raster.setFilters(
            QgsMapLayerProxyModel.Filter.RasterLayer
        )
        form.addRow("影像图层：", self.cbo_raster)

        self.cbo_grid = QgsMapLayerComboBox()
        self.cbo_grid.setFilters(
            QgsMapLayerProxyModel.Filter.PolygonLayer
        )
        form.addRow("样方渔网图层：", self.cbo_grid)

        self.spin_buffer = QSpinBox()
        self.spin_buffer.setRange(0, 2000)
        self.spin_buffer.setSingleStep(50)
        self.spin_buffer.setValue(100)
        self.spin_buffer.setSuffix(" m")
        self.spin_buffer.setToolTip(
            "裁剪时向格子外扩的距离，让边界处的耕地被完整框出"
        )
        form.addRow("边界缓冲：", self.spin_buffer)
        layout.addLayout(form)

        adv = QGroupBox("高级参数")
        adv_form = QFormLayout(adv)
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.05, 0.95)
        self.spin_conf.setSingleStep(0.05)
        self.spin_conf.setValue(0.15)
        self.spin_conf.setToolTip("低于该置信度的田块被丢弃；调低可召回更多小田块")
        adv_form.addRow("置信度阈值：", self.spin_conf)

        self.spin_min_area = QSpinBox()
        self.spin_min_area.setRange(0, 1000000)
        self.spin_min_area.setSingleStep(500)
        self.spin_min_area.setValue(0)
        self.spin_min_area.setSuffix(" m²")
        self.spin_min_area.setSpecialValueText("自动")
        self.spin_min_area.setToolTip(
            "过滤小于该面积的田块。0=自动：2500×像素面积/100，"
            "5m 影像≈625 m²。设小于该值的数等于没有效果；"
            "想保留更小田块需调低置信度而非调小此值"
        )
        adv_form.addRow("最小田块面积：", self.spin_min_area)

        self.chk_all_cells = QCheckBox("处理全部格子（忽略选中状态）")
        adv_form.addRow(self.chk_all_cells)
        self.chk_clip = QCheckBox("结果裁切回格子范围（去掉缓冲带内的越界部分）")
        adv_form.addRow(self.chk_clip)
        layout.addWidget(adv)

        out_row = QHBoxLayout()
        self.edit_output = QLineEdit(os.path.join(
            tempfile.gettempdir(), "cropland_fields.gpkg"
        ))
        btn_browse = QToolButton()
        btn_browse.setText("…")
        btn_browse.clicked.connect(self.browse_output)
        out_row.addWidget(self.edit_output)
        out_row.addWidget(btn_browse)
        layout.addWidget(QLabel("输出 GeoPackage："))
        layout.addLayout(out_row)

        self.btn_run = QPushButton("处理选中的渔网格子")
        self.btn_run.clicked.connect(self.start)
        layout.addWidget(self.btn_run)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(220)
        self.log.setStyleSheet("font-family: monospace; font-size: 11px;")
        layout.addWidget(self.log)

    def browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "输出文件", self.edit_output.text(), "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.endswith(".gpkg"):
                path += ".gpkg"
            self.edit_output.setText(path)

    _TQDM_RE = re.compile(r"\S.*\d+%[|│]")

    def append_log(self, text):
        self.log.appendPlainText(text)

    def _replace_last_line(self, text):
        from qgis.PyQt.QtGui import QTextCursor

        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(
            QTextCursor.MoveOperation.StartOfLine,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertText(text)

    # ---------- run ----------

    def start(self):
        raster = self.cbo_raster.currentLayer()
        grid = self.cbo_grid.currentLayer()
        if raster is None or grid is None:
            QMessageBox.warning(self, "提示", "请先选择影像图层和渔网图层。")
            return
        image_path = inference.local_raster_path(raster)
        if image_path is None:
            QMessageBox.warning(
                self,
                "不支持",
                "影像图层不是本地栅格文件（在线图层暂不支持），请加载本地 GeoTIFF。",
            )
            return
        if grid.crs().isGeographic():
            QMessageBox.warning(
                self,
                "CRS 问题",
                "渔网图层是地理坐标系，无法按米做缓冲。请先重投影到投影坐标系。",
            )
            return
        if self.chk_all_cells.isChecked():
            cells = list(grid.getFeatures())
            self.append_log(f"[i] 全量模式：处理渔网全部 {len(cells)} 个格子")
        else:
            cells = grid.selectedFeatures()
            if not cells:
                QMessageBox.warning(
                    self,
                    "没有选中要素",
                    "请先在渔网图层中选中要处理的格子（可多选），"
                    "或勾选「处理全部格子」。",
                )
                return

        out_path = self.edit_output.text().strip()
        if not out_path.endswith(".gpkg"):
            out_path += ".gpkg"
            self.edit_output.setText(out_path)

        self.tmp_root = tempfile.mkdtemp(prefix="cropland_delineator_")
        self._oom = False
        self.append_log(f"[i] 影像：{image_path}")
        min_area = self.spin_min_area.value()
        min_area_txt = "自动(≈625)" if min_area == 0 else f"{min_area} m²"
        self.append_log(
            f"[i] 格子数：{len(cells)}，缓冲 {self.spin_buffer.value()} m"
            f"，置信度 {self.spin_conf.value():.2f}，最小面积 {min_area_txt}"
        )
        self.append_log(f"[i] 临时目录：{self.tmp_root}")

        buffer_m = self.spin_buffer.value()
        raster_crs = raster.crs()
        self.queue = []
        self.results = []
        try:
            for idx, feat in enumerate(cells):
                cell_id = inference.cell_display_id(feat, idx)
                geom = QgsGeometry(feat.geometry())
                orig_geom = QgsGeometry(feat.geometry())
                if buffer_m > 0:
                    geom = geom.buffer(float(buffer_m), 8)
                xform = QgsCoordinateTransform(
                    grid.crs(), raster_crs, QgsProject.instance()
                )
                geom.transform(xform)
                orig_geom.transform(xform)
                cutline = inference.write_cutline_gpkg(
                    geom, orig_geom, raster_crs, self.tmp_root, idx
                )
                self.queue.append((cell_id, cutline, image_path))
        except Exception as exc:
            QMessageBox.critical(self, "准备失败", f"写出裁剪范围失败：{exc}")
            return

        self.total_cells = len(self.queue)
        self.progress.setVisible(True)
        self.progress.setRange(0, self.total_cells)
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)
        self.run_next_cell()

    def run_next_cell(self):
        if not self.queue:
            self.finish()
            return
        cell_id, cutline, image_path = self.queue.pop(0)
        workdir = os.path.join(self.tmp_root, cell_id)
        os.makedirs(workdir, exist_ok=True)
        out_gpkg = os.path.join(workdir, "delineated.gpkg")
        json_path = inference.write_worker_params(
            workdir,
            image=image_path,
            cutline=cutline,
            output=out_gpkg,
            model=MODEL_PATH,
            confidence=self.spin_conf.value(),
            minimum_area_m2=self.spin_min_area.value(),
            clip_to_cell=self.chk_clip.isChecked(),
        )

        self.append_log(f"[cell {cell_id}] 推理开始…")
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(workdir)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_worker_output)
        self.proc.finished.connect(lambda code, status, cid=cell_id, out=out_gpkg:
                                   self.on_worker_done(code, status, cid, out))
        self.proc.errorOccurred.connect(
            lambda err, cid=cell_id: self.append_log(
                f"[cell {cid}] QProcess 错误：{err}")
        )
        self.proc.start(INFERENCE_PYTHON, [INFERENCE_WORKER, json_path])

    def _kill_proc_tree(self):
        """Kill the worker AND its forked multiprocessing children.

        proc.kill() only hits the main process: DA's forked workers survive,
        keep churning CPU and pin the dead parent's CUDA context open
        (the "phantom GPU memory" leak). The worker makes itself a process
        group leader, so killpg takes down the whole tree.
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        running = proc.state() != QProcess.ProcessState.NotRunning
        if running:
            pid = proc.processId()
            killed_group = False
            if pid > 0:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    killed_group = True
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if not killed_group:
                proc.kill()
            proc.waitForFinished(3000)
        proc.deleteLater()

    def on_worker_output(self):
        if self.proc is None:
            return
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        if "OutOfMemoryError" in text:
            self._oom = True
        # tqdm redraws progress with \r: collapse those into one live line
        for chunk in text.split("\n"):
            for line in chunk.split("\r"):
                if not line.strip():
                    continue
                if self._TQDM_RE.match(line.strip()):
                    self._replace_last_line(line.strip())
                else:
                    self.append_log(line)

    def on_worker_done(self, code, _status, cell_id, out_gpkg):
        ok = code == 0 and os.path.exists(out_gpkg)
        if ok:
            self.results.append(out_gpkg)
            self.append_log(f"[cell {cell_id}] 完成 ✓")
        else:
            self.append_log(f"[cell {cell_id}] 失败（退出码 {code}）")
            if getattr(self, "_oom", False):
                self.append_log(
                    "[提示] 显存不足：终端运行 nvidia-smi 查看占用，"
                    "kill -9 <PID> 清理残留推理进程后重试"
                )
                self._oom = False
            if code == 3:  # worker preflight refused: not enough free VRAM
                left = len(self.queue)
                self.queue = []
                self.append_log(
                    f"[中止] 显存不足或被占用，剩余 {left} 格不再处理；"
                    "清理占用后重新运行即可"
                )
        self.progress.setValue(self.total_cells - len(self.queue))
        self.run_next_cell()

    def finish(self):
        out_path = self.edit_output.text().strip()
        try:
            merged = inference.merge_outputs(self.results, out_path, self.iface)
        except Exception as exc:
            self.append_log(f"[error] 合并输出失败：{exc}")
            self.append_log(
                "[提示] 若输出路径正被 QGIS 图层占用，请换一个输出文件名重试"
            )
            self.btn_run.setEnabled(True)
            self.progress.setVisible(False)
            self.proc = None
            self.cleanup_tmp()
            return
        if merged:
            self.append_log(f"[done] 输出：{out_path}（已加载到图层面板）")
            self.iface.messageBar().pushMessage(
                "Cropland Delineator",
                f"完成：{len(self.results)}/{self.total_cells} 个格子，结果已加载",
                Qgis.MessageLevel.Success,
            )
        else:
            self.append_log("[done] 没有成功输出的格子")
        self.btn_run.setEnabled(True)
        self.progress.setVisible(False)
        self.proc = None
        self.cleanup_tmp()

    def cleanup_tmp(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def closeEvent(self, event):
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.append_log("[i] 关闭窗口：终止运行中的推理进程（含其子进程）")
            self._kill_proc_tree()
            self.cleanup_tmp()
        super().closeEvent(event)
