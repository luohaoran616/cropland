"""Plugin bootstrap: register menu entry and toolbar action."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtWidgets import QMessageBox


class CroplandDelineatorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.action_annotate = None
        self.annotate_dialog = None

    def initGui(self):
        self.action = QAction(QIcon(), "Cropland Delineator", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToVectorMenu("&Cropland Delineator", self.action)
        self.iface.addToolBarIcon(self.action)

        self.action_annotate = QAction(
            QIcon(), "耕地标注工作台", self.iface.mainWindow())
        self.action_annotate.setCheckable(True)
        self.action_annotate.triggered.connect(self.run_annotate)
        self.iface.addPluginToVectorMenu("&Cropland Delineator", self.action_annotate)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&Cropland Delineator", self.action)
        self.iface.removePluginMenu(
            "&Cropland Delineator", self.action_annotate)
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None
        if self.annotate_dialog is not None:
            dock = self.annotate_dialog
            self.annotate_dialog = None
            try:
                dock.close()  # 可见状态下走 closeEvent（保存会话、收回工具）
            except Exception:
                pass
            self.iface.removeDockWidget(dock)
            try:
                dock._teardown()   # 摘应用级事件过滤器
                dock.setParent(None)
                dock.deleteLater()  # 真正销毁：reload 后不残留重复面板
            except Exception:
                pass

    def run(self):
        from .dialog import DelineateDialog

        if self.dialog is not None:
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        try:
            self.dialog = DelineateDialog(self.iface)
        except RuntimeError as exc:  # e.g. inference env not configured yet
            QMessageBox.critical(
                self.iface.mainWindow(), "Cropland Delineator", str(exc)
            )
            return
        self.dialog.show()

    def run_annotate(self):
        from .annotate import AnnotateDock

        dock = self.annotate_dialog
        if dock is not None:
            # 菜单是开关：勾=显示并激活，取消=收起（走 close 保存会话）
            if self.action_annotate.isChecked():
                dock.show()
                dock.raise_()
            else:
                dock.close()
            return
        dock = AnnotateDock(self.iface)
        self.annotate_dialog = dock
        # 侧栏自身的 X / 视图菜单切换 → 同步菜单勾选态
        dock.visibilityChanged.connect(self.action_annotate.setChecked)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        # AITracer 同侧占位时叠成标签页共享空间（不并排挤占）；
        # 只在首次创建时做一次，用户手动拖开的话 QGIS 会记住
        try:
            from qgis.PyQt.QtWidgets import QDockWidget

            mw = self.iface.mainWindow()
            for other in mw.findChildren(QDockWidget):
                if (other is not dock and not other.isHidden()
                        and "aitracer" in other.windowTitle().lower()):
                    mw.tabifyDockWidget(other, dock)
                    break
        except Exception:
            pass
        dock.show()
