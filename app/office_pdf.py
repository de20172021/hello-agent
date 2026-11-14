"""借本机 Office 把 docx/pptx/xlsx 渲染成 PDF，供右侧面板内嵌显示。

为什么要绕这一道：Chromium（浏览器、Electron、Tauri 都一样）不会渲染 Office 格式。
要在右侧面板里「看到和原文件一样的页面」，最靠谱的是让 Office 自己排版导出 PDF——
版式、字体、分页、图表位置都是原始效果，不是把内容抽出来重排。

导出结果按 (路径, mtime, size) 缓存到 data/preview/，文件改过会自动重新导出。
没装 Office 或非 Windows 时 convert() 返回 None，调用方退回下载卡片。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from .settings import DATA_DIR

CACHE_DIR = DATA_DIR / "preview"
# 能交给 Office 渲染的格式
CONVERTIBLE = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}
# 缓存保留时间
_TTL = 7 * 86400
# 转换失败后多久再试（避免每次点开都卡一次冷启动）
_FAIL_TTL = 600

_LOCK = threading.Lock()
_FAILS: dict[str, float] = {}
_pruned_at = 0.0


def available() -> bool:
    """只有 Windows + 装了 Office + 有 pywin32 才走这条路。"""
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _cache_key(src: Path) -> str:
    try:
        st = src.stat()
    except OSError:
        st = None
    raw = f"{src.resolve()}|{st.st_mtime_ns if st else 0}|{st.st_size if st else 0}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_path(src: Path) -> Path:
    return CACHE_DIR / f"{_cache_key(src)}.pdf"


def _prune() -> None:
    """顺手清掉过期缓存，每小时最多一次。"""
    global _pruned_at
    if _pruned_at and time.time() - _pruned_at < 3600:
        return
    _pruned_at = time.time()
    if not CACHE_DIR.is_dir():
        return
    deadline = time.time() - _TTL
    for item in CACHE_DIR.glob("*.pdf"):
        try:
            if item.stat().st_mtime < deadline:
                item.unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------------------ 各格式转换


def _word_to_pdf(src: Path, out: Path) -> None:
    import win32com.client as com

    app = com.DispatchEx("Word.Application")
    try:
        app.Visible = False
        try:
            app.DisplayAlerts = 0
        except Exception:  # noqa: BLE001
            pass
        doc = app.Documents.Open(
            str(src), ReadOnly=True, AddToRecentFiles=False, Visible=False
        )
        try:
            # wdExportFormatPDF = 17
            doc.ExportAsFixedFormat(
                OutputFileName=str(out),
                ExportFormat=17,
                OpenAfterExport=False,
                OptimizeFor=0,
                IncludeDocProps=False,
            )
        finally:
            doc.Close(False)
    finally:
        app.Quit()


def _ppt_to_pdf(src: Path, out: Path) -> None:
    import win32com.client as com

    app = com.DispatchEx("PowerPoint.Application")
    try:
        try:
            app.DisplayAlerts = 0
        except Exception:  # noqa: BLE001
            pass
        pres = app.Presentations.Open(
            str(src), ReadOnly=True, Untitled=False, WithWindow=False
        )
        try:
            # ppSaveAsPDF = 32
            pres.SaveAs(str(out), 32)
        finally:
            pres.Close()
    finally:
        app.Quit()


def _excel_to_pdf(src: Path, out: Path) -> None:
    import win32com.client as com

    app = com.DispatchEx("Excel.Application")
    try:
        app.Visible = False
        app.DisplayAlerts = False
        wb = app.Workbooks.Open(str(src), ReadOnly=True, UpdateLinks=0)
        try:
            # xlTypePDF = 0
            wb.ExportAsFixedFormat(0, str(out))
        finally:
            wb.Close(False)
    finally:
        app.Quit()


_CONVERTERS = {
    ".docx": _word_to_pdf,
    ".doc": _word_to_pdf,
    ".pptx": _ppt_to_pdf,
    ".ppt": _ppt_to_pdf,
    ".xlsx": _excel_to_pdf,
    ".xls": _excel_to_pdf,
}


# ------------------------------------------------------------------ 对外入口


def convert(src: Path, *, force: bool = False) -> Path | None:
    """把 Office 文档渲染成缓存里的 PDF；不支持或失败返回 None。"""
    suffix = src.suffix.lower()
    fn = _CONVERTERS.get(suffix)
    if fn is None or not available():
        return None

    out = _cache_path(src)
    key = out.stem
    if out.exists() and not force:
        return out
    if not force and _FAILS.get(key, 0) > time.time() - _FAIL_TTL:
        return None

    # Office COM 是单实例 + 重量级，串行化，避免并发开一堆 WINWORD
    with _LOCK:
        if out.exists() and not force:
            return out
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _prune()
        fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="conv_", dir=str(CACHE_DIR))
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.unlink(missing_ok=True)

        import pythoncom

        pythoncom.CoInitialize()
        try:
            fn(src, tmp)
        except Exception:  # noqa: BLE001  任何 COM 异常都当「没渲染成」
            tmp.unlink(missing_ok=True)
            _FAILS[key] = time.time()
            return None
        finally:
            pythoncom.CoUninitialize()

        if not tmp.exists() or tmp.stat().st_size < 512:
            tmp.unlink(missing_ok=True)
            _FAILS[key] = time.time()
            return None
        os.replace(tmp, out)
        _FAILS.pop(key, None)
        return out
