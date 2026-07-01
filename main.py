# -- coding: utf-8 --
import sys
import os
import sqlite3
import hashlib
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QGridLayout, QPushButton, QLineEdit, QLabel,
                               QTreeView, QFileSystemModel, QSplitter, QGroupBox,
                               QTextEdit, QListWidget, QListWidgetItem,
                               QScrollArea, QMessageBox, QFileDialog, QSizePolicy,
                               QDialog, QInputDialog, QMenu)
from PySide6.QtCore import Qt, QThread, Signal, QDir, QSize, QRegularExpression
from PySide6.QtGui import QFont, QColor, QPixmap, QImage, QRegularExpressionValidator

# 尝试导入Pillow用于图片处理
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ==========================================
# 【新增】自定义 QLabel，实现图片等比例缩放与居中
# ==========================================
class ScaledPixmapLabel(QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pixmap = None
        self.setAlignment(Qt.AlignCenter)

    def setPixmap(self, pixmap):
        self._pixmap = pixmap
        if pixmap and not pixmap.isNull():
            # 等比例缩放并居中
            scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            super().setPixmap(scaled)
        else:
            super().setPixmap(pixmap)

    def resizeEvent(self, event):
        # 当窗口大小改变时，重新计算缩放比例
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            super().setPixmap(scaled)
        super().resizeEvent(event)

# ==========================================
# 1. 数据库管理模块 (已清理无用分类代码)
# ==========================================
class DatabaseManager:
    def __init__(self):
        self.business_conn = None
        self.business_cursor = None
        self.current_business_path = None

    def switch_business_database(self, folder_path):
        if self.current_business_path == folder_path: return
        if self.business_conn: self.business_conn.close()
        self.current_business_path = folder_path
        business_file = os.path.join(folder_path, "image_tags.db")
        self.business_conn = sqlite3.connect(business_file)
        self.business_cursor = self.business_conn.cursor()
        self.business_cursor.execute('PRAGMA encoding = "UTF-8";') 
        self._init_business_tables()

    def _init_business_tables(self): 
        self.business_cursor.executescript("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                file_name TEXT,
                file_size INTEGER,
                md5_hash TEXT,
                comment TEXT,
                year TEXT,
                month TEXT,
                day TEXT,
                province TEXT,
                city TEXT,
                district TEXT
            );
        """)
        
        try: self.business_cursor.execute("ALTER TABLE images DROP COLUMN rating")
        except: pass
        
        new_cols = ['year', 'month', 'day', 'province', 'city', 'district']
        for col in new_cols:
            try: self.business_cursor.execute(f"ALTER TABLE images ADD COLUMN {col} TEXT")
            except: pass
        self.business_conn.commit()

    def get_or_create_image(self, file_path):
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        self.business_cursor.execute("INSERT OR IGNORE INTO images (file_path, file_name, file_size) VALUES (?, ?, ?)",
                                    (file_path, file_name, file_size))
        self.business_conn.commit()
        
        self.business_cursor.execute(
            "SELECT id, file_path, file_name, file_size, md5_hash, comment, year, month, day, province, city, district "
            "FROM images WHERE file_path = ?", 
            (file_path,)
        )
        return self.business_cursor.fetchone()

    def update_image_info(self, file_path, **kwargs):
        updates, params = [], []
        allowed_fields = ['md5_hash', 'comment', 'year', 'month', 'day', 'province', 'city', 'district']
        for key, value in kwargs.items():
            if key in allowed_fields:
                updates.append(f"{key} = ?")
                params.append(value)
        if updates:
            params.append(file_path)
            self.business_cursor.execute(f"UPDATE images SET {', '.join(updates)} WHERE file_path = ?", params)
            self.business_conn.commit()

    def rename_image_in_db(self, old_path, new_path, new_name):
        self.business_cursor.execute("UPDATE images SET file_path = ?, file_name = ? WHERE file_path = ?",
                                     (new_path, new_name, old_path))
        self.business_conn.commit()

# ==========================================
# 2. 异步线程
# ==========================================
class MD5Worker(QThread):
    finished = Signal(str)
    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    def run(self):
        md5 = hashlib.md5()
        try:
            with open(self.file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(4096), b""): md5.update(chunk)
            self.finished.emit(md5.hexdigest())
        except Exception: self.finished.emit("Error")

class ImageLoaderWorker(QThread):
    image_loaded = Signal(QPixmap, str)
    def __init__(self, file_path, max_size=QSize(800, 600)):
        super().__init__()
        self.file_path = file_path
        self.max_size = max_size

    def run(self):
        # ==========================================
        # 第一层：尝试使用 Pillow (处理特殊色彩模式)
        # ==========================================
        if PIL_AVAILABLE:
            # 【核心修复】解除 2 亿像素限制，防止超大卫星图直接触发 DecompressionBombError
            Image.MAX_IMAGE_PIXELS = None 
            try:
                with Image.open(self.file_path) as img:
                    try: img.draft('RGB', (self.max_size.width(), self.max_size.height()))
                    except: pass
                    
                    try: resample = Image.Resampling.LANCZOS
                    except AttributeError: resample = Image.ANTIALIAS
                    
                    # 先缩小，再转色
                    img.thumbnail((self.max_size.width(), self.max_size.height()), resample)
                    if img.mode != 'RGB': img = img.convert('RGB')
                    
                    img_bytes = img.tobytes()
                    qimg = QImage(img_bytes, img.width, img.height, img.width * 3, QImage.Format_RGB888)
                    pixmap = QPixmap.fromImage(qimg)
                    self.image_loaded.emit(pixmap, f"{img.width}×{img.height} (Pillow加载)")
                    return # 成功则直接结束
            except MemoryError:
                print("Pillow 内存溢出，准备降级到 Qt 底层...")
            except Exception as e:
                print(f"Pillow 加载异常: {e}，准备降级到 Qt 底层...")

        # ==========================================
        # 第二层：降级使用 Qt 原生 QImageReader (杀手锏)
        # 利用 C++ 底层直接缩放，绕过 Python 内存瓶颈，支持读取 TIFF 金字塔
        # ==========================================
        try:
            from PySide6.QtGui import QImageReader
            reader = QImageReader(self.file_path)
            
            # 【核心杀手锏】让 Qt 在 C++ 底层解码时直接缩放
            # 如果 TIFF 包含金字塔图层，Qt 会直接读取缩小版，内存占用极小！
            reader.setScaledSize(self.max_size)
            reader.setQuality(50) # 适当降低质量换取加载速度
            
            qimg = reader.read()
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                self.image_loaded.emit(pixmap, f"{qimg.width()}×{qimg.height()} (Qt底层加速加载)")
                return
        except Exception as e:
            print(f"Qt 原生加载异常: {e}")

        # ==========================================
        # 彻底失败
        # ==========================================
        self.image_loaded.emit(QPixmap(), "❌ 文件过大/格式特殊，超出预览极限")

# ==========================================
# 4. 主窗口
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片标签管理工具 v1.1.0")
        self.resize(1400, 900)
        self.db = DatabaseManager()
        self.current_image_path = None
        self.current_folder_path = os.getcwd()
        self.md5_worker = None
        self.image_loader = None

        self.cache_date = {'year': '', 'month': '', 'day': ''}
        self.cache_loc = {'province': '', 'city': '', 'district': ''}
        
        # 【核心修复】在初始化 UI 之前，必须先初始化数据库连接！
        # 否则 init_ui 末尾调用 load_image_list 时，business_cursor 为 None 会导致崩溃。
        self.db.switch_business_database(self.current_folder_path)
        
        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- 左侧 ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(5, 5, 5, 5)

        btn_open = QPushButton("打开文件夹")
        btn_open.clicked.connect(self.open_folder)
        
        self.path_label = QLineEdit()
        self.path_label.setReadOnly(True)

        self.tree_view = QTreeView()
        self.file_model = QFileSystemModel()
        self.file_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)
        self.file_model.setRootPath(QDir.rootPath())
        self.tree_view.setModel(self.file_model)
        self.tree_view.setRootIndex(self.file_model.index(QDir.currentPath()))
        self.tree_view.clicked.connect(self.on_tree_folder_clicked)
        self.tree_view.hideColumn(1); self.tree_view.hideColumn(2); self.tree_view.hideColumn(3)

        self.image_list = QListWidget()
        self.image_list.itemClicked.connect(self.on_image_item_clicked)

        info_group = QGroupBox("📄 文件基本信息")
        info_layout = QVBoxLayout()
        self.lbl_name = QLabel("名称: -")
        self.lbl_size = QLabel("大小: -")
        self.lbl_path = QLabel("路径: -")
        self.lbl_md5 = QLabel("MD5: -")
        for lbl in [self.lbl_name, self.lbl_size, self.lbl_path, self.lbl_md5]:
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            info_layout.addWidget(lbl)
        info_group.setLayout(info_layout)

        left_layout.addWidget(btn_open)
        left_layout.addWidget(self.path_label)
        left_layout.addWidget(self.tree_view, 1)
        left_layout.addWidget(self.image_list, 2)
        left_layout.addWidget(info_group)

        # --- 右侧 ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 10, 10, 10)

        preview_group = QGroupBox("🖼️ 图片预览")
        preview_layout = QVBoxLayout()
        
        # 【修改】使用自定义的 ScaledPixmapLabel 替代普通 QLabel，实现完美等比例缩放
        self.preview_label = ScaledPixmapLabel("请选择图片")
        self.preview_label.setMinimumSize(400, 300)
        self.preview_label.setStyleSheet("QLabel { background-color: #f0f0f0; border: 1px solid #ccc; }")
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        self.lbl_image_info = QLabel("")
        self.lbl_image_info.setAlignment(Qt.AlignCenter)
        self.lbl_image_info.setStyleSheet("QLabel { color: gray; font-size: 10px; }")
        preview_layout.addWidget(self.preview_label)
        preview_layout.addWidget(self.lbl_image_info)
        preview_group.setLayout(preview_layout)

        rename_group = QGroupBox("✏️ 修改文件名")
        rename_layout = QHBoxLayout()
        btn_rename = QPushButton("修改文件名称")
        btn_rename.clicked.connect(self.rename_file)
        self.input_new_name = QLineEdit()
        self.input_new_name.setPlaceholderText("输入新文件名 (自动补全扩展名)")
        rename_layout.addWidget(btn_rename)
        rename_layout.addWidget(self.input_new_name)
        rename_group.setLayout(rename_layout)

        # 日期时间
        date_group = QGroupBox("📅 日期时间")
        date_layout = QVBoxLayout()
        date_input_layout = QHBoxLayout()
        
        self.in_year = QLineEdit(); self.in_year.setPlaceholderText("年"); self.in_year.setFixedWidth(120)
        self.in_month = QLineEdit(); self.in_month.setPlaceholderText("月"); self.in_month.setFixedWidth(120)
        self.in_day = QLineEdit(); self.in_day.setPlaceholderText("日"); self.in_day.setFixedWidth(120)
        
        num_validator = QRegularExpressionValidator(QRegularExpression(r"\d*"))
        self.in_year.setValidator(num_validator)
        self.in_month.setValidator(num_validator)
        self.in_day.setValidator(num_validator)
        
        btn_inherit_date = QPushButton("继承")
        btn_inherit_date.clicked.connect(self.inherit_date)
        
        date_input_layout.addWidget(self.in_year)
        date_input_layout.addWidget(self.in_month)
        date_input_layout.addWidget(self.in_day)
        date_input_layout.addWidget(btn_inherit_date)
        date_input_layout.addStretch()
        
        date_cache_layout = QHBoxLayout()
        self.cache_year = self._create_cache_box('year', 120)
        self.cache_month = self._create_cache_box('month', 120)
        self.cache_day = self._create_cache_box('day', 120)
        date_cache_layout.addWidget(self.cache_year)
        date_cache_layout.addWidget(self.cache_month)
        date_cache_layout.addWidget(self.cache_day)
        date_cache_layout.addStretch()
        
        date_layout.addLayout(date_input_layout)
        date_layout.addLayout(date_cache_layout)
        date_group.setLayout(date_layout)

        # 所在地区
        loc_group = QGroupBox("📍 所在地区")
        loc_layout = QVBoxLayout()
        self.lbl_loc_path = QLabel("路径: -")
        self.lbl_loc_path.setStyleSheet("QLabel { color: gray; font-size: 12px; }")
        self.lbl_loc_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        loc_layout.addWidget(self.lbl_loc_path)

        loc_input_layout = QHBoxLayout()
        self.in_prov = QLineEdit(); self.in_prov.setPlaceholderText("省"); self.in_prov.setFixedWidth(160)
        self.in_city = QLineEdit(); self.in_city.setPlaceholderText("市"); self.in_city.setFixedWidth(160)
        self.in_dist = QLineEdit(); self.in_dist.setPlaceholderText("区"); self.in_dist.setFixedWidth(160)
        
        btn_inherit_loc = QPushButton("继承")
        btn_inherit_loc.clicked.connect(self.inherit_loc)
        
        loc_input_layout.addWidget(self.in_prov)
        loc_input_layout.addWidget(self.in_city)
        loc_input_layout.addWidget(self.in_dist)
        loc_input_layout.addWidget(btn_inherit_loc)
        loc_input_layout.addStretch()

        loc_cache_layout = QHBoxLayout()
        self.cache_prov = self._create_cache_box('province', 160)
        self.cache_city = self._create_cache_box('city', 160)
        self.cache_dist = self._create_cache_box('district', 160)
        loc_cache_layout.addWidget(self.cache_prov)
        loc_cache_layout.addWidget(self.cache_city)
        loc_cache_layout.addWidget(self.cache_dist)
        loc_cache_layout.addStretch()

        loc_layout.addLayout(loc_input_layout) 
        loc_layout.addLayout(loc_cache_layout)
        loc_group.setLayout(loc_layout)

        comment_group = QGroupBox("📝 注释")
        comment_layout = QVBoxLayout()
        self.txt_comment = QLineEdit()
        self.txt_comment.setPlaceholderText("输入注释 (最大30字)")
        self.txt_comment.textChanged.connect(self.on_comment_changed)
        comment_layout.addWidget(self.txt_comment)
        comment_group.setLayout(comment_layout)

        # 图片预览保持在最上方，下方放置田字型网格布局
        right_layout.addWidget(preview_group, 1)
        
        # 【修改】使用 QGridLayout 实现四个区域的田字型布局
        grid_layout = QGridLayout()
        grid_layout.addWidget(date_group, 0, 0)    # 第0行第0列 (左上)：日期时间
        grid_layout.addWidget(rename_group, 0, 1)  # 第0行第1列 (右上)：修改文件名
        grid_layout.addWidget(loc_group, 1, 0)     # 第1行第0列 (左下)：所在地区
        grid_layout.addWidget(comment_group, 1, 1) # 第1行第1列 (右下)：注释
        
        # 设置列拉伸因子，确保左右两列宽度平均分配
        grid_layout.setColumnStretch(0, 1)
        grid_layout.setColumnStretch(1, 1)
        
        # 【新增】强制限制四个功能模块的高度，使其适应内容，不被撑开
        for group in [date_group, loc_group, rename_group, comment_group]:
            # 水平方向保持扩展 (Expanding)，垂直方向限制为最大内容高度 (Maximum)
            group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            
        right_layout.addLayout(grid_layout, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

        blue_btn_style = """
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                padding: 5px 10px;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #005A9E;
            }
        """
        for btn in [btn_open, btn_rename, btn_inherit_date, btn_inherit_loc]:
            btn.setStyleSheet(blue_btn_style)

        self._bind_inputs()
        self.load_image_list(self.current_folder_path)

    def _create_cache_box(self, key, width=80):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        box = QLineEdit()
        box.setReadOnly(True)
        box.setPlaceholderText("双击复制")
        box.setStyleSheet("QLineEdit { background: #f0f0f0; color: blue; border: 1px solid #ccc; }")
        box.mouseDoubleClickEvent = lambda e: QApplication.clipboard().setText(box.text())
        layout.addWidget(box, 1)
        
        btn_x = QPushButton("X")
        btn_x.setFixedSize(25, 25)
        btn_x.setStyleSheet("QPushButton { color: red; font-weight: bold; border: 1px solid #ccc; background: white; }")
        btn_x.clicked.connect(lambda: self.clear_cache(key, box))
        layout.addWidget(btn_x)
        
        widget.box = box
        widget.setFixedWidth(width) 
        return widget

    def _bind_inputs(self):
        inputs = [
            (self.in_year, 'year'), (self.in_month, 'month'), (self.in_day, 'day'),
            (self.in_prov, 'province'), (self.in_city, 'city'), (self.in_dist, 'district')
        ]
        for inp, key in inputs:
            inp.editingFinished.connect(lambda k=key: self.on_input_finished(k))

    def on_input_finished(self, key):
        if key in self.cache_date:
            line_edit = self.in_year if key=='year' else (self.in_month if key=='month' else self.in_day)
            if not self.validate_date(key, line_edit):
                self.update_cache_from_input(key)
                return  
        self.update_cache_from_input(key)

    def validate_date(self, key, line_edit):
        text = line_edit.text().strip()
        if not text:
            return True
        if key == 'year':
            if not (text.isdigit() and len(text) == 4):
                QMessageBox.warning(self, "输入错误", "年份必须是4位数字！")
                line_edit.clear()
                return False
        elif key == 'month':
            if not (text.isdigit() and 1 <= int(text) <= 12):
                QMessageBox.warning(self, "输入错误", "月份必须是1~12的数字！")
                line_edit.clear()
                return False
        elif key == 'day':
            if not (text.isdigit() and 1 <= int(text) <= 31):
                QMessageBox.warning(self, "输入错误", "日期必须是1~31的数字！")
                line_edit.clear()
                return False
        return True

    def update_cache_from_input(self, key):
        if key in self.cache_date:
            val = self.in_year.text() if key=='year' else (self.in_month.text() if key=='month' else self.in_day.text())
            self.cache_date[key] = val
            self._update_cache_display('date')
        elif key in self.cache_loc:
            val = self.in_prov.text() if key=='province' else (self.in_city.text() if key=='city' else self.in_dist.text())
            self.cache_loc[key] = val
            self._update_cache_display('loc')
        self.save_current_inputs()

    def _update_cache_display(self, group):
        if group == 'date':
            self.cache_year.box.setText(self.cache_date['year'])
            self.cache_month.box.setText(self.cache_date['month'])
            self.cache_day.box.setText(self.cache_date['day'])
        elif group == 'loc':
            self.cache_prov.box.setText(self.cache_loc['province'])
            self.cache_city.box.setText(self.cache_loc['city'])
            self.cache_dist.box.setText(self.cache_loc['district'])

    def clear_cache(self, key, box_widget):
        if key in self.cache_date: self.cache_date[key] = ''
        elif key in self.cache_loc: self.cache_loc[key] = ''
        box_widget.clear()
        self._update_cache_display('date')
        self._update_cache_display('loc')

    def inherit_date(self):
        self.in_year.setText(self.cache_date['year'])
        self.in_month.setText(self.cache_date['month'])
        self.in_day.setText(self.cache_date['day'])
        self.save_current_inputs()

    def inherit_loc(self):
        self.in_prov.setText(self.cache_loc['province'])
        self.in_city.setText(self.cache_loc['city'])
        self.in_dist.setText(self.cache_loc['district'])
        self.save_current_inputs()

    def save_current_inputs(self):
        if not self.current_image_path: return
        kwargs = {
            'year': self.in_year.text(), 'month': self.in_month.text(), 'day': self.in_day.text(),
            'province': self.in_prov.text(), 'city': self.in_city.text(), 'district': self.in_dist.text(),
        }
        self.db.update_image_info(self.current_image_path, **kwargs)
        self.load_image_list(self.current_folder_path)

    def rename_file(self):
        new_name = self.input_new_name.text().strip()
        if not new_name or not self.current_image_path:
            QMessageBox.warning(self, "提示", "请输入新文件名！")
            return
        if '.' not in new_name:
            _, ext = os.path.splitext(self.current_image_path)
            new_name += ext
        new_path = os.path.join(os.path.dirname(self.current_image_path), new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "错误", "目标文件名已存在！")
            return
        try:
            os.rename(self.current_image_path, new_path)
            self.db.rename_image_in_db(self.current_image_path, new_path, new_name)
            self.current_image_path = new_path
            self.input_new_name.clear()
            self.load_image_list(self.current_folder_path)
            self.load_image_info(new_path)
            QMessageBox.information(self, "成功", "文件名修改成功！")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"修改失败: {e}")

    def open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if folder:
            self.path_label.setText(folder)
            self.tree_view.setRootIndex(self.file_model.index(folder))
            self.current_folder_path = folder
            self.db.switch_business_database(folder)
            self.load_image_list(folder)

    def on_tree_folder_clicked(self, index):
        folder_path = self.file_model.filePath(index)
        if os.path.isdir(folder_path):
            self.current_folder_path = folder_path
            self.db.switch_business_database(folder_path)
            self.load_image_list(folder_path)

    def load_image_list(self, folder_path):
        self.image_list.clear()
        if not os.path.isdir(folder_path): return
        image_files = [f for f in os.listdir(folder_path) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tif', '.tiff'))]
        
        if image_files:
            placeholders = ','.join(['?'] * len(image_files))
            sql = f"SELECT file_name, year, month, day, province, city, district, comment FROM images WHERE file_name IN ({placeholders})"
            self.db.business_cursor.execute(sql, image_files)
            db_status = {row[0]: row[1:] for row in self.db.business_cursor.fetchall()}
        else: db_status = {}

        for img_file in image_files:
            status = db_status.get(img_file, (None,)*8)
            is_unmarked = not any(status)
            item = QListWidgetItem()
            if is_unmarked:
                item.setText(f"⚠️ [未标记] {img_file}")
                item.setForeground(QColor("red"))
            else:
                item.setText(f"✅ [已标记] {img_file}")
                item.setForeground(QColor("black"))
            item.setData(Qt.UserRole, os.path.join(folder_path, img_file))
            self.image_list.addItem(item)

    def on_image_item_clicked(self, item):
        file_path = item.data(Qt.UserRole)
        if file_path: self.load_image_info(file_path)

    def load_image_info(self, file_path):
        self.current_image_path = file_path
        img_data = self.db.get_or_create_image(file_path)
        
        self.lbl_name.setText(f"名称: {img_data[2]}")
        self.lbl_size.setText(f"大小: {img_data[3] / 1024:.2f} KB")
        self.lbl_path.setText(f"路径: {file_path}")
        self.lbl_md5.setText(f"MD5: {img_data[4] if img_data[4] else '计算中...'}")
        self.lbl_loc_path.setText(f"路径: {file_path}")

        self.load_image_preview(file_path)
        if not img_data[4]: self.start_md5_calc(file_path)

        def safe_get(idx, default=''):
            return img_data[idx] if idx < len(img_data) and img_data[idx] else default
        
        self.txt_comment.blockSignals(True)
        self.txt_comment.setText(safe_get(5))
        self.txt_comment.blockSignals(False)

        self.in_year.setText(safe_get(6)); self.in_month.setText(safe_get(7)); self.in_day.setText(safe_get(8))
        self.in_prov.setText(safe_get(9)); self.in_city.setText(safe_get(10)); self.in_dist.setText(safe_get(11))

    def load_image_preview(self, file_path):
        self.preview_label.setText("加载中...")
        if self.image_loader and self.image_loader.isRunning():
            self.image_loader.quit(); self.image_loader.wait()
        self.image_loader = ImageLoaderWorker(file_path, QSize(800, 600))
        self.image_loader.image_loaded.connect(self.on_image_loaded)
        self.image_loader.start()

    def on_image_loaded(self, pixmap, info_text):
        if not pixmap.isNull(): 
            self.preview_label.setPixmap(pixmap)
        else: 
            self.preview_label.setText("无法预览")
        self.lbl_image_info.setText(info_text)

    def start_md5_calc(self, file_path):
        if self.md5_worker and self.md5_worker.isRunning(): self.md5_worker.quit()
        self.md5_worker = MD5Worker(file_path)
        self.md5_worker.finished.connect(self.on_md5_finished)
        self.md5_worker.start()

    def on_md5_finished(self, md5_hash):
        if self.current_image_path and md5_hash != "Error":
            self.lbl_md5.setText(f"MD5: {md5_hash}")
            self.db.update_image_info(self.current_image_path, md5_hash=md5_hash)

    def on_comment_changed(self):
        if not self.current_image_path: return
        text = self.txt_comment.text()
        if len(text) > 30:
            self.txt_comment.blockSignals(True)
            self.txt_comment.setText(text[:30])
            self.txt_comment.blockSignals(False)
            self.txt_comment.setCursorPosition(30)
        else:
            self.db.update_image_info(self.current_image_path, comment=text)
            self.load_image_list(self.current_folder_path)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
