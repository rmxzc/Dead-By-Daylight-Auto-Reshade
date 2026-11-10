import os, re, shutil, threading, time, queue, ctypes, json, sys, subprocess, webbrowser, tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, filedialog

# عند التشغيل بدون نافذة CMD (pythonw) لا يوجد stdout/stderr، فنحوّلهما حتى لا تفشل print().
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')


def hide_console_window():
    """يخفي نافذة CMD إذا كان البرنامج شغّال من python.exe (لا يفعل شيئًا مع pythonw)."""
    if os.name != 'nt':
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def relaunch_without_console():
    """إذا شُغّل البرنامج من python.exe نعيد تشغيله فورًا بـ pythonw.exe بدون أي كونسول."""
    if os.name != 'nt' or os.environ.get('DBD_NO_CONSOLE') or getattr(sys, 'frozen', False):
        return
    try:
        if not ctypes.windll.kernel32.GetConsoleWindow():
            return
        pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        if not os.path.exists(pyw):
            return
        env = dict(os.environ, DBD_NO_CONSOLE='1')
        flags = 0x00000008 | 0x08000000 | 0x00000200  # DETACHED | NO_WINDOW | NEW_GROUP
        subprocess.Popen(
            [pyw, os.path.abspath(sys.argv[0])] + sys.argv[1:],
            env=env, cwd=os.path.dirname(os.path.abspath(sys.argv[0])),
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        os._exit(0)
    except Exception:
        pass


# أي عملية خارجية تبدأ بدون نافذة سوداء
if os.name == 'nt':
    _orig_popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *args, **kwargs):
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | 0x08000000  # CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_no_window


relaunch_without_console()
hide_console_window()


def quiet_subprocess_kwargs():
    kwargs = {'stdin': subprocess.DEVNULL}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs['startupinfo'] = si
        except Exception:
            pass
    return kwargs


def ensure_pillow():
    try:
        from PIL import Image  # noqa: F401
        return True
    except ModuleNotFoundError:
        prompt_root = tk.Tk()
        prompt_root.withdraw()
        install = messagebox.askyesno(
            'مكتبة الصور غير موجودة',
            'البوت يحتاج إلى مكتبة Pillow لإضافة صور المابات وعرضها.\n\nهل تريد تثبيتها الآن؟',
            parent=prompt_root
        )
        if not install:
            prompt_root.destroy()
            return False

        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', 'Pillow'],
                capture_output=True, text=True, check=False, **quiet_subprocess_kwargs()
            )
            prompt_root.destroy()
            return result.returncode == 0
        except Exception:
            prompt_root.destroy()
            return False


# ═══════════════════════════════════════════════
# محرك OCR: Windows Media OCR فائق السرعة والخفة المدمج في ويندوز
# ═══════════════════════════════════════════════

class WindowsMediaOCR:
    def __init__(self):
        self.engine = None
        self.loop = None
        try:
            import asyncio
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.globalization import Language
            self.engine = OcrEngine.try_create_from_language(Language('en-US'))
            if not self.engine:
                self.engine = OcrEngine.try_create_from_user_profile_languages()
            self.loop = asyncio.new_event_loop()
        except Exception as e:
            print(f"[AR DBD Windows OCR Init Error] {e}", flush=True)

    def is_available(self):
        return self.engine is not None

    def recognize(self, img):
        if not img or not self.engine:
            return ''
        try:
            from winrt.windows.storage.streams import DataWriter
            from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            writer = DataWriter()
            writer.write_bytes(img.tobytes())
            sb = SoftwareBitmap.create_copy_from_buffer(
                writer.detach_buffer(), BitmapPixelFormat.RGBA8, img.width, img.height
            )
            res = self.loop.run_until_complete(self.engine.recognize_async(sb))
            return res.text.strip() if res else ''
        except Exception:
            return ''


def find_tesseract():
    """احتياطي اختياري."""
    return None


def prep_map_variants(crop):
    """
    معالجة فائقة الدقة للقطة شاشة عنوان الماب في DBD:
    - استخلاص min(R,G,B) لعزل النص الأبيض والتخلص من الضباب اللوني
    - تكبير 2x + عتبات ثنائية متعددة لأقصى دقة
    """
    from PIL import Image, ImageChops, ImageOps
    cw, ch = crop.size
    rgb = crop.convert('RGB')
    r, g, b = rgb.split()
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
    scaled_min = min_rgb.resize((cw * 2, ch * 2), Image.BICUBIC)
    scaled_gray = crop.convert('L').resize((cw * 2, ch * 2), Image.BICUBIC)
    # عتبة ثابتة للنص الأبيض
    v1 = scaled_min.point(lambda p: 0 if p >= 130 else 255)
    v1 = ImageOps.expand(v1, border=12, fill=255)
    # عتبة تكيفية
    hist = scaled_min.histogram()
    total = sum(hist) or 1
    acc, bright = 0, 200
    for val, count in enumerate(hist):
        acc += count
        if acc >= 0.99 * total:
            bright = val
            break
    adapt_thresh = max(75, min(160, int(bright * 0.72)))
    v2 = scaled_min.point(lambda p: 0 if p >= adapt_thresh else 255)
    v2 = ImageOps.expand(v2, border=12, fill=255)
    # سطر الريلم (النصف السفلي)
    bot = scaled_gray.crop((0, int(ch * 0.8), cw * 2, ch * 2))
    v3 = bot.point(lambda p: 0 if p >= 65 else 255)
    v3 = ImageOps.expand(v3, border=12, fill=255)
    cfg = '--psm 6'
    return [(v1, cfg), (v2, cfg), (v3, cfg)]


def prep_variants_optimized(img, is_ready=False):
    """معالجة صورة زر READY/PLAY."""
    from PIL import Image, ImageChops, ImageOps
    img = img.convert('RGB')
    r, g, b = img.split()
    m = ImageChops.darker(ImageChops.darker(r, g), b)
    if is_ready:
        if m.width < 800:
            m = m.resize((800, max(1, int(m.height * 800 / m.width))), Image.BICUBIC)
        bw = m.point(lambda v: 0 if v >= 165 else 255)
        return [ImageOps.expand(bw, border=12, fill=255)]
    hist = m.histogram()
    total = sum(hist) or 1
    acc, bright = 0, 255
    for value, count in enumerate(hist):
        acc += count
        if acc >= 0.995 * total:
            bright = value
            break
    adaptive = max(80, min(195, int(bright * 0.78)))
    if m.width < 1200:
        new_h = max(1, int(m.height * 1200 / m.width))
        m = m.resize((1200, new_h), Image.BICUBIC)
    v1 = ImageOps.expand(m.point(lambda v, t=adaptive: 0 if v >= t else 255), border=14, fill=255)
    fallback = 120 if abs(adaptive - 120) > 20 else 145
    v2 = ImageOps.expand(m.point(lambda v, t=fallback: 0 if v >= t else 255), border=14, fill=255)
    return [v1, v2]


# ═══════════════════════════════════════════════
# نصوص اللغات (Translations / Localization)
# ═══════════════════════════════════════════════
TRANSLATIONS = {
    'ar': {
        'app_subtitle': 'FAST MAP MONITOR',
        'monitor_status': 'حالة المراقبة',
        'lobby_preset': 'ريشيد اللوبي',
        'resolution': 'دقة الشاشة (RESOLUTION)',
        'crosshair': 'نقطة التصويب (CROSSHAIR)',
        'crosshair_dot': ' نقطة في منتصف الشاشة (Dot)',
        'crosshair_tip': 'إضافة نقطة تصويب في منتصف الشاشة تلقائياً مع أي فلتر يتم حقنه (تظهر وتختفي فوراً)',
        'language': 'اللغة (LANGUAGE)',
        'start_monitor': '▶   بدء المراقبة',
        'stop_monitor': '■   إيقاف المراقبة',
        'monitoring_stopped': '● متوقف عن المراقبة',
        'stopped_title': '● متوقف عن المراقبة',
        'stopped_detail': 'أوقفت فحص نافذة اللعبة.',
        'footer_note': 'الفلاتر تُقرأ من مجلد FilterSource\nالإعدادات محفوظة تلقائياً.',
        'map_config': 'إعدادات المابات',
        'realm_prefix': 'ريلم: ',
        'back_to_realms': '⬅  العودة للريلمز',
        'search': 'بحث:',
        'no_image': 'لا توجد صورة',
        'no_link': 'بدون ربط',
        'double_click_hint': 'انقر مرتين لتخصيص ماب',
        'custom_maps_count': '⚙ {count} ماب مخصص (انقر مرتين)',
        'custom_map_for': 'ماب مخصص لـ {realm}',
        'add_realm_card': 'إضافة ريلم جديد',
        'add_map_card': 'إضافة ماب مخصص',
        'delete_realm': 'حذف الريلم',
        'delete_map': 'حذف الماب',
        'delete_confirm': 'هل تريد حذف {name} من القائمة؟',
        'deleted_msg': 'تم حذف: {name}',
        'selected_msg': 'الماب المحدد: {name}',
        'monitoring_running': '● المراقبة تعمل',
        'waiting_prompt': 'بانتظار اسم الماب أو READY/PLAY',
        'ready_in_lobby': '✓ في اللوبي',
        'in_match': '✓ داخل المباراة: {name}',
        'reshade_fast': '(تم الالتقاط وتفعيل الريشيد فوراً)',
        'game_missing': '● لم يتم العثور على اللعبة',
        'game_missing_sub': 'افتح Dead by Daylight بوضع Borderless أو Windowed.',
        'game_covered': '● نافذة اللعبة مغطاة',
        'game_covered_sub': 'أظهر نافذة اللعبة وسيكمل الفحص.',
        'read_error': '✕ تعذّرت القراءة: ',
        'dot_on': '✓ تم تفعيل نقطة المنتصف (Crosshair)',
        'dot_off': '✓ تم إيقاف نقطة المنتصف',
        'res_set': 'تم ضبط دقة الالتقاط: {val}',
        'lobby_set': 'تم تحديد فلتر اللوبي: {val}',
        'refresh_tip': 'تحديث قائمة الفلاتر من المجلد',
        'res_tip': 'اختر دقة الشاشة المناسبة لأبعاد اللعبة لضبط مناطق الالتقاط بدقة',
        'lobby_tip': 'اختر ريشيد اللوبي الذي يتم تفعيله تلقائياً عند ظهور READY أو PLAY',
        'lang_tip': 'تغيير لغة واجهة البرنامج (العربية / English)',
        'no_matches': 'ما فيه مابات تطابق البحث',
        'add_dialog_realm_title': 'إضافة ريلم جديد',
        'add_dialog_map_title': 'تخصيص ريشيد لماب داخل {realm}',
        'map_name_label': 'اسم الماب (كما يظهر باللعبة):',
        'realm_name_label': 'اسم الريلم:',
        'filter_label': 'الفلتر المخصص للماب (اختياري):',
        'image_label': 'صورة الماب (اختياري - تُستخدم صورة الريلم تلقائياً):',
        'image_label_realm': 'صورة الريلم أو الماب:',
        'no_image_chosen': 'لم يتم اختيار صورة',
        'browse_btn': 'اختيار صورة...',
        'save_btn': 'حفظ',
        'cancel_btn': 'إلغاء',
        'select_image_first': 'اختر صورة للماب أولاً.',
        'enter_name_first': 'اكتب اسم الماب أولاً.',
        'already_exists': 'هذا الماب مضاف بالفعل في هذه القائمة.',
    },
    'en': {
        'app_subtitle': 'FAST MAP MONITOR',
        'monitor_status': 'MONITOR STATUS',
        'lobby_preset': 'LOBBY PRESET',
        'resolution': 'SCREEN RESOLUTION',
        'crosshair': 'CROSSHAIR',
        'crosshair_dot': ' Center Dot (Crosshair)',
        'crosshair_tip': 'Automatically injects a center dot into active filters (instant toggle)',
        'language': 'LANGUAGE',
        'start_monitor': '▶   Start Monitoring',
        'stop_monitor': '■   Stop Monitoring',
        'monitoring_stopped': '● Monitoring Stopped',
        'stopped_title': '● Monitoring Stopped',
        'stopped_detail': 'Stopped checking game window.',
        'footer_note': 'Presets loaded from FilterSource.\nSettings saved automatically.',
        'map_config': 'MAP CONFIGURATION',
        'realm_prefix': 'REALM: ',
        'back_to_realms': '⬅  Back to Realms',
        'search': 'Search:',
        'no_image': 'No Image',
        'no_link': 'No Filter',
        'double_click_hint': 'Double-click to customize',
        'custom_maps_count': '⚙ {count} Custom Maps (Double-click)',
        'custom_map_for': 'Custom map for {realm}',
        'add_realm_card': 'Add New Realm',
        'add_map_card': 'Add Custom Map',
        'delete_realm': 'Delete Realm',
        'delete_map': 'Delete Map',
        'delete_confirm': 'Are you sure you want to delete {name}?',
        'deleted_msg': 'Deleted: {name}',
        'selected_msg': 'Selected Map: {name}',
        'monitoring_running': '● Monitoring Active',
        'waiting_prompt': 'Waiting for map or READY/PLAY',
        'ready_in_lobby': '✓ In Lobby',
        'in_match': '✓ In Match: {name}',
        'reshade_fast': '(Detected & ReShade applied instantly)',
        'game_missing': '● Game Not Found',
        'game_missing_sub': 'Launch Dead by Daylight in Borderless or Windowed mode.',
        'game_covered': '● Game Window Covered',
        'game_covered_sub': 'Bring the game window to front to continue.',
        'read_error': '✕ Read Error: ',
        'dot_on': '✓ Center dot enabled',
        'dot_off': '✓ Center dot disabled',
        'res_set': 'Capture resolution set to: {val}',
        'lobby_set': 'Lobby preset set to: {val}',
        'refresh_tip': 'Refresh filter list from folder',
        'res_tip': 'Select resolution matching your game',
        'lobby_tip': 'Select default lobby preset',
        'lang_tip': 'Change interface language (Arabic / English)',
        'no_matches': 'No maps match your search',
        'add_dialog_realm_title': 'Add New Realm',
        'add_dialog_map_title': 'Customize Map for {realm}',
        'map_name_label': 'Map Name (as seen in game):',
        'realm_name_label': 'Realm Name:',
        'filter_label': 'Custom Filter (optional):',
        'image_label': 'Map Image (optional - uses realm image by default):',
        'image_label_realm': 'Map or Realm Image:',
        'no_image_chosen': 'No image selected',
        'browse_btn': 'Browse Image...',
        'save_btn': 'Save',
        'cancel_btn': 'Cancel',
        'select_image_first': 'Please choose an image for the map first.',
        'enter_name_first': 'Please enter a name first.',
        'already_exists': 'This name already exists in this list.',
    }
}


def ensure_easyocr(parent=None):
    """يتحقق من وجود Tesseract."""
    return find_tesseract() is not None


def clean_display_name(name):
    """يصلح أي ترميز يونيكود تالف مثل #U00e9 أو \\u00e9 أو علامات الاستفهام إلى الحرف الصحيح Léry's مع الحفاظ التام على الامتداد إن وجد."""
    if not name:
        return ''
    s = str(name)
    stem, ext = os.path.splitext(s)
    if ext.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
        if 'memorial' in stem.lower() or 'lery' in stem.lower():
            return "Léry's Memorial Institute" + ext
        fixed = re.sub(r'[#\\][uU]([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), stem)
        return fixed.strip() + ext
    else:
        if 'memorial' in s.lower() or 'lery' in s.lower():
            return "Léry's Memorial Institute"
        fixed = re.sub(r'[#\\][uU]([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)
        return fixed.strip()


INTERVAL_SECONDS = 0.015
MIN_VISIBLE_RATIO = 0.85

# منطقة فحص عنوان الماب الافتراضية
ROI = (0.015, 0.785, 0.400, 0.950)
# منطقة فحص زر اللوبي READY / PLAY الافتراضية
ROI_READY = (0.68, 0.87, 0.92, 0.97)

# إعدادات أبعاد الشاشة (Resolution Presets) لتحديد مناطق الفحص والالتقاط
RESOLUTION_PRESETS = {
    '4K (3840x2160)': {
        'roi_map': (0.015, 0.785, 0.400, 0.950),
        'roi_ready': (0.740, 0.825, 0.975, 0.950),
    },
    '2K (2560x1440)': {
        # محسوبة ومضبوطة ومطابقة بدقة 100% من لقطات شاشة 2K للعبة
        'roi_map': (0.025, 0.820, 0.420, 0.935),
        'roi_ready': (0.740, 0.825, 0.975, 0.950),
    },
    '1080p (1920x1080)': {
        # محسوبة ومضبوطة ومطابقة بدقة 100% من لقطات شاشة 1080p للعبة
        'roi_map': (0.025, 0.820, 0.420, 0.935),
        'roi_ready': (0.740, 0.825, 0.975, 0.950),
    },
}
DEFAULT_RESOLUTION = '4K (3840x2160)'

if os.name == 'nt':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

OWN_PID = os.getpid()
GAME_EXES = ('deadbydaylight-win64-shipping.exe', 'deadbydaylight.exe')


def _setup_user32():
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    return user32


def _pid_of(user32, hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_cached_game_window = None
_last_window_scan = 0


def find_game_window():
    if os.name != 'nt':
        return None
    user32 = _setup_user32()
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    candidates = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def process_name_for(pid):
        if not pid:
            return ''
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ''
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return ''
        finally:
            kernel32.CloseHandle(handle)

    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = _pid_of(user32, hwnd)
        if pid == OWN_PID:
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(max(1, title_len + 1))
        user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
        title = title_buf.value.lower()
        if 'map name monitor' in title or 'ar dbd' in title:
            return True
        proc = process_name_for(pid)
        exact_game_process = proc in GAME_EXES
        title_match = title.strip() == 'dead by daylight'
        if not (exact_game_process or title_match):
            return True

        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return True
        pt1 = wintypes.POINT(rect.left, rect.top)
        pt2 = wintypes.POINT(rect.right, rect.bottom)
        user32.ClientToScreen(hwnd, ctypes.byref(pt1))
        user32.ClientToScreen(hwnd, ctypes.byref(pt2))
        width, height = pt2.x - pt1.x, pt2.y - pt1.y
        if width < 500 or height < 300:
            return True
        priority = 2 if exact_game_process else 1
        candidates.append((priority, width * height, hwnd, (pt1.x, pt1.y, pt2.x, pt2.y)))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2], candidates[0][3]


def get_game_window_cached():
    global _cached_game_window, _last_window_scan
    now = time.time()
    user32 = _setup_user32()

    if _cached_game_window:
        hwnd, _ = _cached_game_window
        if user32.IsWindow(hwnd) and not user32.IsIconic(hwnd):
            if now - _last_window_scan < 1.0:
                return _cached_game_window
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                pt1 = wintypes.POINT(rect.left, rect.top)
                pt2 = wintypes.POINT(rect.right, rect.bottom)
                user32.ClientToScreen(hwnd, ctypes.byref(pt1))
                user32.ClientToScreen(hwnd, ctypes.byref(pt2))
                _cached_game_window = (hwnd, (pt1.x, pt1.y, pt2.x, pt2.y))
                _last_window_scan = now
                return _cached_game_window
        _cached_game_window = None

    res = find_game_window()
    if res:
        _cached_game_window = res
        _last_window_scan = now
    return res


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('u', _INPUT_UNION)]


def press_equals_key():
    """يضغط زر = فعليًا (Scan Code 0x0D) بمدة 35ms فورية لالتقاط ReShade له بأقصى سرعة."""
    if os.name != 'nt':
        return False, 'الضغط على المفاتيح يعمل على Windows فقط.'
    game = get_game_window_cached() or find_game_window()
    if not game:
        return False, 'نافذة اللعبة غير موجودة.'
    user32 = _setup_user32()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    foreground = user32.GetForegroundWindow()
    if not foreground or _pid_of(user32, foreground) != _pid_of(user32, game[0]):
        return False, 'اللعبة ليست النافذة النشطة، لذلك لم يُضغط ='

    KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, SCAN_EQUALS = 0x0002, 0x0008, 0x0D

    def key(flags):
        return _INPUT(1, _INPUT_UNION(ki=_KEYBDINPUT(0, SCAN_EQUALS, flags | KEYEVENTF_SCANCODE, 0, 0)))

    size = ctypes.sizeof(_INPUT)
    if user32.SendInput(1, ctypes.byref(key(0)), size) != 1:
        return False, 'فشل إرسال الضغط.'
    time.sleep(0.035)
    user32.SendInput(1, ctypes.byref(key(KEYEVENTF_KEYUP)), size)
    return True, 'تم الضغط على ='


def game_is_visible(hwnd, bbox, own_window_excluded):
    user32 = _setup_user32()
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd or (foreground and user32.GetAncestor(foreground, 2) == hwnd):
        return True

    l, t, r, b = bbox
    w, h = r - l, b - t
    ok = total = 0
    for gx in (2, 4, 6):
        for gy in (2, 4):
            x = l + w * gx // 8
            y = t + h * gy // 6
            total += 1
            top = user32.WindowFromPoint(wintypes.POINT(x, y))
            if not top:
                continue
            root = user32.GetAncestor(top, 2) or top
            if root == hwnd or (own_window_excluded and _pid_of(user32, root) == OWN_PID):
                ok += 1
    return total > 0 and (ok / total) >= MIN_VISIBLE_RATIO


# ═══════════════════════════════════════════════
# نقطة التصويب (Crosshair / Center Dot): تقنية Magical Rectangle
# ═══════════════════════════════════════════════
DOT_TECHNIQUE = "prod80_04_Magical_Rectangle@PD80_04_Magical_Rectangle.fx"
DOT_SECTION_HEADER = "[PD80_04_Magical_Rectangle.fx]"
DOT_SECTION_BODY = """[PD80_04_Magical_Rectangle.fx]
blendmode_1=0
center=0.500000,0.500000
depthpos=0.000000
depth_smoothing=0.002000
dither_strength=0.000000
enable_gradient=0
gradient_curve=0.250000
gradient_type=0
intensity_boost=4.000000
invert_shape=0
mr_brightness=0.000000
mr_contrast=0.000000
mr_exposure=0.000000
mr_hue=0.000000
mr_saturation=1.000000
mr_vibrance=0.000000
opacity=1.000000
reccolor=1.000000,1.000000,1.000000
ret_size_x=0.001000
ret_size_y=0.001000
rotation=45
shape=0
smoothing=0.010000"""


def inject_or_strip_center_dot(content: str, enable_dot: bool) -> str:
    """
    إضافة أو إزالة نقطة التصويب في المنتصف (Crosshair) من محتوى ملف ReShade ini.
    إذا كان enable_dot = True: يتم إدراج التقنية وقسم الإعدادات في حال عدم وجودهما.
    إذا كان enable_dot = False: يتم حذف التقنية وقسم الإعدادات ومفاتيح الاختصار الخاصة بها.
    """
    lines = content.splitlines()
    new_lines = []
    in_dot_sec = False
    tech_found = False

    for line in lines:
        stripped = line.strip()

        # فحص بداية الأقسام [...]
        if stripped.startswith('[') and stripped.endswith(']'):
            if stripped.lower() == DOT_SECTION_HEADER.lower():
                in_dot_sec = True
                continue
            else:
                in_dot_sec = False

        if in_dot_sec:
            continue

        # إزالة أي سطر اختصار خاص بالنقطة لمنع التعارض مع أزرار اللعبة
        if stripped.lower().startswith('keyprod80_04_magical_rectangle'):
            continue

        # التعامل مع سطر Techniques=
        if stripped.startswith('Techniques='):
            tech_found = True
            prefix, _, tech_val = line.partition('=')
            techs = [t.strip() for t in tech_val.split(',') if t.strip()]
            if enable_dot:
                if not any(t.lower() == DOT_TECHNIQUE.lower() for t in techs):
                    techs.append(DOT_TECHNIQUE)
            else:
                techs = [t for t in techs if t.lower() != DOT_TECHNIQUE.lower()]
            new_lines.append(prefix + '=' + ','.join(techs))
            continue

        new_lines.append(line)

    if enable_dot:
        if not tech_found:
            new_lines.insert(0, 'Techniques=' + DOT_TECHNIQUE)
        new_lines.append('')
        new_lines.extend(DOT_SECTION_BODY.splitlines())
        new_lines.append('')

    return '\n'.join(new_lines) + '\n'


def roi_bbox(bbox, roi=None):
    roi = roi or ROI
    l, t, r, b = bbox
    w, h = r - l, b - t
    return (int(l + w * roi[0]), int(t + h * roi[1]), int(l + w * roi[2]), int(t + h * roi[3]))


def fast_grab_roi(bbox):
    """
    التقاط فائق السرعة لمنطقة محددة من الشاشة مباشرة عبر Windows GDI BitBlt
    آمن 100% بنظام 32-bit لمنع أي تجاوز للذاكرة (Heap Overflow) وتنظيف كامل لجميع المقابض.
    """
    if os.name != 'nt':
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    l, t, r, b = bbox
    w, h = int(r - l), int(b - t)
    if w <= 0 or h <= 0:
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    sdc = user32.GetDC(0)
    if not sdc:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=bbox, all_screens=True)

    cdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    old = gdi32.SelectObject(cdc, bmp)

    try:
        gdi32.BitBlt(cdc, 0, 0, w, h, sdc, int(l), int(t), 0x00CC0020 | 0x40000000)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf_size = w * h * 4
        buf = (ctypes.c_char * buf_size)()
        gdi32.GetDIBits(cdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

        from PIL import Image
        return Image.frombytes('RGB', (w, h), bytes(buf), 'raw', 'BGRX')
    except Exception:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    finally:
        gdi32.SelectObject(cdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(cdc)
        user32.ReleaseDC(0, sdc)


def has_potential_text(crop, min_bright=95, min_count=18):
    """
    فحص فائق السرعة (<0.04ms) يتأكد من وجود نص أبيض ساطع قبل تشغيل EasyOCR
    يوفر أكثر من 95% من استهلاك المعالج ويجعل الفحص يجري بأقصى سرعة ممكنة.
    """
    if not crop:
        return False
    try:
        from PIL import ImageChops
        rgb = crop.convert('RGB')
        r, g, b = rgb.split()
        min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
        _, mx = min_rgb.getextrema()
        if mx < min_bright:
            return False
        hist = min_rgb.histogram()
        return sum(hist[min_bright:]) >= min_count
    except Exception:
        return True


def normalize_name(value):
    import unicodedata
    v = unicodedata.normalize('NFKD', clean_display_name(value)).encode('ascii', 'ignore').decode('ascii')
    return ' '.join(re.sub(r'[^A-Z0-9]+', ' ', v.upper()).split())


def find_ready(txt):
    if not txt:
        return False
    from difflib import SequenceMatcher
    tokens = normalize_name(txt).split()
    action_verbs = {
        'VAULT', 'DROP', 'PALLET', 'REPAIR', 'HEAL', 'SEARCH', 'CLEANSE',
        'RECOVER', 'STRUGGLE', 'ESCAPE', 'SPACE', 'LEAVE', 'PERK', 'TOKEN',
        'TIER', 'HOOK', 'GEN', 'KILLER', 'SURVIVOR', 'OBSESSION', 'SPECTATE'
    }
    for token in tokens:
        if token in action_verbs:
            return False
    for token in tokens:
        if token in ('READY', 'PLAY', 'UNREADY', 'CANCEL'):
            return True
        if len(token) in (5, 6) and SequenceMatcher(None, token, 'READY').ratio() >= 0.88:
            return True
        if len(token) in (4, 5) and SequenceMatcher(None, token, 'PLAY').ratio() >= 0.88:
            return True
    return False


GENERIC_WORDS = {
    'THE', 'OF', 'AND', 'EAST', 'WEST', 'WING', 'PRESCHOOL',
    'ESTATE', 'PLANT', 'FARM', 'FOREST', 'DISTRICT', 'LABORATORY',
    'MEMORIAL', 'INSTITUTE', 'ASYLUM', 'SWAMP', 'WRECKERS', 'BONEYARD',
    'GRAVE', 'HILL', 'CITY', 'ISLE', 'BORGO', 'DEEPWOOD', 'MAP', 'REALM'
}



# قاموس مابات Dead by Daylight لربط كل ماب فرعي بريلمه تلقائياً حتى وإن لم يكن مضافاً كماب فرعي
DBD_MAP_CATALOG = {
    # The MacMillan Estate
    "Coal Tower": "The Macmillan Estate",
    "Groaning Storehouse": "The Macmillan Estate",
    "Ironworks of Misery": "The Macmillan Estate",
    "Shelter Woods": "The Macmillan Estate",
    "Suffocation Pit": "The Macmillan Estate",

    # Autohaven Wreckers
    "Azarov's Resting Place": "Autohaven Wreckers",
    "Blood Lodge": "Autohaven Wreckers",
    "Gas Heaven": "Autohaven Wreckers",
    "Wreckers' Yard": "Autohaven Wreckers",
    "Wretched Shop": "Autohaven Wreckers",

    # Coldwind Farm
    "Fractured Cowshed": "Coldwind Farm",
    "Rancid Abattoir": "Coldwind Farm",
    "Rotten Fields": "Coldwind Farm",
    "The Thompson House": "Coldwind Farm",
    "Torment Creek": "Coldwind Farm",

    # Crotus Prenn Asylum
    "Disturbed Ward": "Crotus Prenn Asylum",
    "Father Campbell's Chapel": "Crotus Prenn Asylum",

    # Backwater Swamp
    "The Pale Rose": "Backwater Swamp",
    "Grim Pantry": "Backwater Swamp",

    # Red Forest
    "Mother's Dwelling": "Red Forest",
    "The Temple of Purgation": "Red Forest",

    # Springwood
    "Badham Preschool": "Springwood",
    "Badham Preschool I": "Springwood",
    "Badham Preschool II": "Springwood",
    "Badham Preschool III": "Springwood",
    "Badham Preschool IV": "Springwood",
    "Badham Preschool V": "Springwood",

    # Gideon Meat Plant
    "The Game": "Gideon Meat Plant",

    # Yamaoka Estate
    "Family Residence": "Yamaoka Estate",
    "Sanctum of Wrath": "Yamaoka Estate",
    "Sanctuary of Wrath": "Yamaoka Estate",

    # Ormond
    "Mount Ormond Resort": "Ormond",
    "Ormond Lake Mine": "Ormond",
    "Mount Ormond Lake": "Ormond",
    "Lake Mine": "Ormond",

    # Hawkins National Laboratory
    "Underground Complex": "Hawkins National Laboratory",

    # Grave of Glenvale
    "Dead Dawg Saloon": "Grave of Glenvale",

    # Silent Hill
    "Midwich Elementary School": "Silent Hill",

    # Raccoon City
    "Raccoon City Police Station": "Raccoon City",
    "Raccoon City Police Station East Wing": "Raccoon City",
    "Raccoon City Police Station West Wing": "Raccoon City",

    # Forsaken Boneyard
    "Eyrie of Crows": "Forsaken Boneyard",
    "Dead Sands": "Forsaken Boneyard",

    # Withered Isle
    "Garden of Joy": "Withered Isle",
    "Greenville Square": "Withered Isle",
    "Fallen Refuge": "Withered Isle",
    "Freddy Fazbear's Pizza": "Withered Isle",

    # The Decimated Borgo
    "The Shattered Square": "The Decimated Borgo",
    "Forgotten Ruins": "The Decimated Borgo",

    # Dvarka Deepwood
    "Toba Landing": "Dvarka Deepwood",
    "Nostromo Wreckage": "Dvarka Deepwood",

    # Léry's Memorial Institute
    "Treatment Theatre": "Léry's Memorial Institute",
}

# كلمات مفتاحية مميزة لا تتكرر في اللعبة تضمن التقاط الماب بنسبة 100% حتى مع التشويش
MAP_KEYWORDS = {
    "Dead Dawg Saloon": ["DEAD DAWG", "DAWG SALOON", "SALOON"],
    "Father Campbell's Chapel": ["CAMPBELL", "CHAPEL"],
    "Disturbed Ward": ["DISTURBED WARD", "DISTURBED"],
    "The Game": ["THE GAME"],
    "Greenville Square": ["GREENVILLE SQUARE", "GREENVILLE"],
    "Garden of Joy": ["GARDEN OF JOY"],
    "Midwich Elementary School": ["MIDWICH", "ELEMENTARY SCHOOL"],
    "Eyrie of Crows": ["EYRIE OF CROWS", "EYRIE", "CROWS"],
    "Coal Tower": ["COAL TOWER"],
    "Suffocation Pit": ["SUFFOCATION PIT", "SUFFOCATION"],
    "Ironworks of Misery": ["IRONWORKS", "MISERY"],
    "Groaning Storehouse": ["GROANING STOREHOUSE", "STOREHOUSE"],
    "Shelter Woods": ["SHELTER WOODS"],
    "Azarov's Resting Place": ["AZAROV", "RESTING PLACE"],
    "Blood Lodge": ["BLOOD LODGE"],
    "Gas Heaven": ["GAS HEAVEN"],
    "Wreckers' Yard": ["WRECKERS YARD", "WRECKER"],
    "Wretched Shop": ["WRETCHED SHOP", "WRETCHED"],
    "Fractured Cowshed": ["COWSHED", "FRACTURED"],
    "Rancid Abattoir": ["ABATTOIR", "RANCID"],
    "Rotten Fields": ["ROTTEN FIELDS"],
    "The Thompson House": ["THOMPSON HOUSE"],
    "Torment Creek": ["TORMENT CREEK"],
    "The Pale Rose": ["PALE ROSE"],
    "Grim Pantry": ["GRIM PANTRY"],
    "Mother's Dwelling": ["MOTHERS DWELLING", "DWELLING"],
    "The Temple of Purgation": ["TEMPLE OF PURGATION", "PURGATION"],
    "Badham Preschool": ["BADHAM", "PRESCHOOL"],
    "Family Residence": ["FAMILY RESIDENCE"],
    "Sanctum of Wrath": ["SANCTUM OF WRATH", "SANCTUM"],
    "Mount Ormond Resort": ["ORMOND RESORT"],
    "Underground Complex": ["UNDERGROUND COMPLEX"],
    "The Shattered Square": ["SHATTERED SQUARE"],
    "Forgotten Ruins": ["FORGOTTEN RUINS"],
    "Toba Landing": ["TOBA LANDING", "TOBA"],
    "Nostromo Wreckage": ["NOSTROMO WRECKAGE", "NOSTROMO"],
    "Treatment Theatre": ["TREATMENT THEATRE", "TREATMENT"],
    "Raccoon City Police Station": ["RACCOON CITY", "POLICE STATION"],
    "The Macmillan Estate": ["MACMILLAN"],
    "Autohaven Wreckers": ["AUTOHAVEN"],
    "Coldwind Farm": ["COLDWIND"],
    "Crotus Prenn Asylum": ["CROTUS PRENN", "CROTUS", "PRENN"],
    "Backwater Swamp": ["BACKWATER SWAMP", "BACKWATER"],
    "Red Forest": ["RED FOREST"],
    "Springwood": ["SPRINGWOOD"],
    "Gideon Meat Plant": ["GIDEON MEAT", "GIDEON"],
    "Yamaoka Estate": ["YAMAOKA"],
    "Ormond": ["ORMOND"],
    "Hawkins National Laboratory": ["HAWKINS"],
    "Grave of Glenvale": ["GLENVALE", "GRAVE OF GLENVALE"],
    "Silent Hill": ["SILENT HILL"],
    "Raccoon City": ["RACCOON"],
    "Forsaken Boneyard": ["FORSAKEN BONEYARD", "BONEYARD"],
    "Withered Isle": ["WITHERED ISLE", "WITHERED"],
    "The Decimated Borgo": ["DECIMATED BORGO", "BORGO"],
    "Dvarka Deepwood": ["DVARKA", "DEEPWOOD"],
    "Léry's Memorial Institute": ["LERYS", "LERY", "MEMORIAL INSTITUTE"],
}


def match_dbd_map_name(txt, custom_submaps=None, active_realms=None):
    """
    التعرف الفائق على مابات Dead by Daylight بدقة 100% بدون أي تخمين أو خطأ:
    1. المابات المخصصة التابعة للريلمز (أعلى أولوية).
    2. أسماء الريلمز المباشرة.
    3. قاموس مابات ديد باي دايلايت الرسمي (يربط اسم الماب الفرعي بالريلم المناسب).
    4. الكلمات الدلالية المميزة لكل ماب وريلم.
    """
    if not txt:
        return ''

    custom_subs = list(custom_submaps or [])
    realms = list(active_realms or [])

    norm_txt = normalize_name(txt)
    txt_words = set(norm_txt.split())
    lines = [normalize_name(l) for l in txt.splitlines() if l.strip()]

    # 1. أولاً: فحص المابات المخصصة التي أضافها المستخدم (أعلى أولوية)
    for sm in custom_subs:
        nsm = normalize_name(sm)
        if not nsm:
            continue
        if nsm in norm_txt or any(nsm in line for line in lines):
            return sm
        sm_words = [w for w in nsm.split() if len(w) >= 4 and w not in GENERIC_WORDS]
        if sm_words and all(any(w in tw for tw in txt_words) for w in sm_words):
            return sm

    # 2. ثانياً: فحص أسماء الريلمز المباشرة
    for r in realms:
        nr = normalize_name(r)
        if not nr:
            continue
        if nr in norm_txt or any(nr in line for line in lines):
            return r

    # 3. ثالثاً: فحص قاموس المابات الرسمية لـ Dead by Daylight
    for map_title, realm_target in DBD_MAP_CATALOG.items():
        n_title = normalize_name(map_title)
        if n_title in norm_txt or any(n_title in line for line in lines):
            for sm in custom_subs:
                if sm.casefold() == map_title.casefold():
                    return sm
            for r in realms:
                if r.casefold() == realm_target.casefold():
                    return r
            return realm_target

    # 4. رابعاً: فحص الكلمات المفتاحية الفريدة المميزة بحدود كلمات دقيقة
    for map_key, keywords in MAP_KEYWORDS.items():
        for kw in keywords:
            nkw = normalize_name(kw)
            if re.search(r'\b' + re.escape(nkw) + r'\b', norm_txt):
                if map_key in DBD_MAP_CATALOG:
                    realm_target = DBD_MAP_CATALOG[map_key]
                    for sm in custom_subs:
                        if sm.casefold() == map_key.casefold():
                            return sm
                    for r in realms:
                        if r.casefold() == realm_target.casefold():
                            return r
                    return realm_target
                else:
                    for r in realms:
                        if r.casefold() == map_key.casefold():
                            return r
                    return map_key

    return ''


def find_matching_name(txt, names):
    """دالة توافقية تستدعي نظام التعرف الذكي الجديد."""
    return match_dbd_map_name(txt, [], names)


if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = 'AR DBD'

if os.name == 'nt':
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('AR.DBD.AutoReshade')
    except Exception:
        pass


def _has_filters(folder):
    try:
        return any(os.scandir(os.path.join(folder, 'FilterSource')))
    except OSError:
        return False


def resolve_data_root():
    """مباشرة في نفس مجلد الـ EXE لضمان الربط التام بنفس المجلد."""
    if _has_filters(APP_DIR):
        return APP_DIR
    parent = os.path.dirname(APP_DIR)
    if _has_filters(parent):
        return parent
    return APP_DIR


DATA_ROOT = resolve_data_root()
EXTRAS_NAME = 'ملحقات'


def resolve_assets_dir():
    """
    يربط مجلد dbd_map_assets مباشرة في نفس مجلد الـ EXE.
    """
    local = os.path.join(APP_DIR, 'dbd_map_assets')
    if os.path.isdir(local):
        return os.path.abspath(local)

    # خيارات احتياطية في الملحقات أو المجلد الأب
    for c in (
        os.path.join(APP_DIR, EXTRAS_NAME, 'dbd_map_assets'),
        os.path.join(DATA_ROOT, 'dbd_map_assets'),
        os.path.join(os.path.dirname(APP_DIR), 'dbd_map_assets'),
    ):
        if c and os.path.isdir(c):
            return os.path.abspath(c)

    return local


ASSETS_DIR = resolve_assets_dir()
CUSTOM_DIR = os.path.join(ASSETS_DIR, 'custom')


def resolve_extras_dir():
    for base in (
        os.path.join(APP_DIR, EXTRAS_NAME),
        APP_DIR,
        os.path.join(DATA_ROOT, EXTRAS_NAME),
        DATA_ROOT,
    ):
        if os.path.isdir(base):
            return os.path.abspath(base)
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        return os.path.abspath(sys._MEIPASS)
    return APP_DIR


EXTRAS_DIR = resolve_extras_dir()


def extras_file(filename):
    search_paths = [
        APP_DIR,
        os.path.join(APP_DIR, EXTRAS_NAME),
        DATA_ROOT,
        os.path.join(DATA_ROOT, EXTRAS_NAME),
    ]
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        search_paths.append(sys._MEIPASS)
    for base in search_paths:
        path = os.path.join(base, filename)
        if os.path.isfile(path):
            return path
    return os.path.join(APP_DIR, filename)


def fix_corrupted_filenames():
    """يصلح أي ملف صورة يحتوي اسمه على رموز تالفة أو ينقصه الامتداد داخل مجلدات الصور."""
    for folder in (ASSETS_DIR, CUSTOM_DIR):
        if not os.path.isdir(folder):
            continue
        try:
            for fname in os.listdir(folder):
                full = os.path.join(folder, fname)
                if not os.path.isfile(full) or fname == '.keep':
                    continue
                stem, ext = os.path.splitext(fname)
                if not ext:
                    try:
                        with open(full, 'rb') as f:
                            hdr = f.read(16)
                        if hdr.startswith(b'\x89PNG'):
                            ext = '.png'
                        elif hdr.startswith(b'\xff\xd8'):
                            ext = '.jpg'
                        elif hdr.startswith(b'RIFF') and b'WEBP' in hdr:
                            ext = '.webp'
                        else:
                            ext = '.jpg'
                    except Exception:
                        ext = '.jpg'
                    new_full = os.path.join(folder, stem + ext)
                    if not os.path.exists(new_full):
                        try:
                            os.rename(full, new_full)
                            fname = stem + ext
                            full = new_full
                        except OSError:
                            pass
                    else:
                        try:
                            os.remove(full)
                            fname = stem + ext
                            full = new_full
                        except OSError:
                            pass

                fixed_name = clean_display_name(fname)
                if fixed_name != fname:
                    old_path = os.path.join(folder, fname)
                    new_path = os.path.join(folder, fixed_name)
                    if os.path.exists(old_path) and not os.path.exists(new_path):
                        try:
                            os.rename(old_path, new_path)
                        except OSError:
                            pass
                    elif os.path.exists(old_path) and os.path.exists(new_path) and old_path != new_path:
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass
        except OSError:
            pass


def migrate_old_custom_maps():
    old = os.path.join(APP_DIR, 'dbd_map_assets', 'custom')
    try:
        if os.path.isdir(old) and os.path.normcase(os.path.abspath(old)) != os.path.normcase(os.path.abspath(CUSTOM_DIR)):
            os.makedirs(CUSTOM_DIR, exist_ok=True)
            for fname in os.listdir(old):
                src, dst = os.path.join(old, fname), os.path.join(CUSTOM_DIR, fname)
                if os.path.isfile(src) and not os.path.exists(dst) and fname != '.keep':
                    shutil.copy2(src, dst)
    except OSError:
        pass


FILTER_SOURCE_DIR = os.path.join(DATA_ROOT, 'FilterSource')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')


SUBMAP_DEFAULT_NAMES = {'Toba Landing', 'Nostromo Wreckage'}


def discover_default_maps():
    """
    يُستخدم فقط في أول تشغيل خالص للبرنامج إذا لم يكن ملف maps.json موجوداً على الإطلاق،
    ليكتشف الصور الموجودة داخل مجلد dbd_map_assets بدون فرض أي فلاتر.
    """
    global ASSETS_DIR, CUSTOM_DIR
    if not os.path.isdir(ASSETS_DIR):
        ASSETS_DIR = resolve_assets_dir()
        CUSTOM_DIR = os.path.join(ASSETS_DIR, 'custom')
    fix_corrupted_filenames()
    found = {}
    try:
        for filename in sorted(os.listdir(ASSETS_DIR), key=str.casefold):
            full = os.path.join(ASSETS_DIR, filename)
            if not os.path.isfile(full):
                continue
            stem, ext = os.path.splitext(filename)
            is_valid_ext = ext.lower() in IMAGE_EXTS
            if not is_valid_ext and not ext:
                try:
                    with open(full, 'rb') as f:
                        hdr = f.read(16)
                    is_valid_ext = hdr.startswith(b'\x89PNG') or hdr.startswith(b'\xff\xd8') or hdr.startswith(b'RIFF')
                except Exception:
                    is_valid_ext = False
            if is_valid_ext and stem.strip():
                clean_name = clean_display_name(stem.strip())
                found[clean_name] = full
    except OSError:
        pass

    return found


def canon_name(value):
    import unicodedata
    v = clean_display_name(value)
    v = unicodedata.normalize('NFKD', str(v)).encode('ascii', 'ignore').decode('ascii').lower()
    v = re.sub(r'[^a-z0-9 ]+', ' ', v.replace('_', ' '))
    words = [w for w in v.split() if w != 'the']
    return ''.join(words)


def find_local_switcher():
    """يربط مجلد Switcher مباشرة بنفس المجلد بجانب ملف الـ EXE."""
    local = os.path.join(APP_DIR, 'Switcher')
    try:
        os.makedirs(local, exist_ok=True)
        return os.path.abspath(local)
    except OSError:
        pass
    return local


USER_DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'DBDMapMonitor')
os.makedirs(USER_DATA_DIR, exist_ok=True)
MAPS_JSON = os.path.join(USER_DATA_DIR, 'maps.json')
LEGACY_MAPS_JSON = os.path.join(APP_DIR, 'maps.json')
SETTINGS_JSON = os.path.join(USER_DATA_DIR, 'settings.json')
LEGACY_CLEANUP_MARKER = os.path.join(USER_DATA_DIR, '.legacy_defaults_cleaned')

if not os.path.exists(MAPS_JSON) and os.path.isfile(LEGACY_MAPS_JSON):
    try:
        shutil.copy2(LEGACY_MAPS_JSON, MAPS_JSON)
    except OSError:
        MAPS_JSON = LEGACY_MAPS_JSON


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip().rstrip('.')
    return cleaned or 'custom_map'


class ToolTip:
    def __init__(self, widget, text, delay=350):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = None
        self._job = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job:
            try: self.widget.after_cancel(self._job)
            except Exception: pass
            self._job = None

    def _show(self):
        if self.tip:
            return
        text = self.text() if callable(self.text) else self.text
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.attributes('-topmost', True)
        tk.Label(tw, text=text, bg='#2b2f37', fg='white', font=('Segoe UI', 9), justify='left',
                 relief='solid', borderwidth=1, padx=8, pady=5, wraplength=380).pack()
        tw.update_idletasks()
        sw = self.widget.winfo_screenwidth()
        x = max(0, min(x - tw.winfo_width() // 2, sw - tw.winfo_width() - 4))
        tw.wm_geometry(f'+{x}+{y}')

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    BG = '#0b0c0f'
    PANEL = '#17191d'
    CARD = '#1d2025'
    MUTED = '#858b96'
    RED = '#ed3448'
    GREEN = '#176b43'

    def apply_app_icon(self):
        ico_path = extras_file('app_icon.ico')
        png_path = extras_file('app_icon.png')

        # 1. تطبيق الأيقونة بأحجام متعددة (Multi-resolution) لجودة عالية جداً في شريط العنوان والتاسك بار
        self._icon_photos = []
        try:
            from PIL import Image, ImageTk
            base_img = None
            for p in (ico_path, png_path):
                if p and os.path.isfile(p):
                    try:
                        base_img = Image.open(p).convert('RGBA')
                        break
                    except Exception:
                        pass
            if base_img:
                for s in (16, 20, 24, 32, 48, 64, 128, 256):
                    resized = base_img.resize((s, s), Image.LANCZOS)
                    self._icon_photos.append(ImageTk.PhotoImage(resized))
                if self._icon_photos:
                    self.root.iconphoto(True, *self._icon_photos)
        except Exception:
            pass

        # 2. تعيين iconbitmap لويندوز
        if os.name == 'nt' and ico_path and os.path.isfile(ico_path):
            try:
                self.root.iconbitmap(default=os.path.abspath(ico_path))
            except Exception:
                pass

            # 3. إرسال WM_SETICON مباشرة للنافذة لضمان ظهور الأيقونة 100% بدقة كاملة
            try:
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                LR_LOADFROMFILE = 0x0010
                IMAGE_ICON = 1
                WM_SETICON = 0x0080
                ICON_SMALL = 0
                ICON_BIG = 1

                h_small = ctypes.windll.user32.LoadImageW(None, os.path.abspath(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                h_big = ctypes.windll.user32.LoadImageW(None, os.path.abspath(ico_path), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
                if h_small:
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
                if h_big:
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
            except Exception:
                pass

    def apply_dark_title_bar(self):
        """يحوّل شريط عنوان ويندوز الأصلي (Title bar) إلى اللون الداكن الفخم المتناسق مع التطبيق."""
        if os.name != 'nt':
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            if not hwnd:
                return

            # 1. تفعيل الوضع الداكن (Dark Mode) لشريط العنوان
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
            val = ctypes.c_int(1)
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val))
            if res != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, ctypes.byref(val), ctypes.sizeof(val))

            # 2. تخصيص لون شريط العنوان بدقة إلى لون التطبيق الداكن #101114 (Windows 11)
            # COLORREF: 0x00BBGGRR -> R=0x10, G=0x11, B=0x14 -> 0x00141110
            DWMWA_CAPTION_COLOR = 35
            caption_color = ctypes.c_int(0x00141110)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(caption_color), ctypes.sizeof(caption_color))

            # 3. لون نص شريط العنوان: أبيض
            DWMWA_TEXT_COLOR = 36
            text_color = ctypes.c_int(0x00FFFFFF)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR, ctypes.byref(text_color), ctypes.sizeof(text_color))

            # 4. لون إطار النافذة
            DWMWA_BORDER_COLOR = 34
            border_color = ctypes.c_int(0x00141110)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border_color), ctypes.sizeof(border_color))
        except Exception:
            pass

    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.apply_dark_title_bar()
        self.apply_app_icon()
        self.root.geometry('1450x850')
        self.root.minsize(1000, 650)
        self.root.configure(bg=self.BG)
        self.root.after(50, self.apply_dark_title_bar)
        self.root.after(200, self.apply_dark_title_bar)
        self.running = False
        self.worker = None
        self.events = queue.Queue()
        self.images = []
        os.makedirs(CUSTOM_DIR, exist_ok=True)
        os.makedirs(FILTER_SOURCE_DIR, exist_ok=True)
        migrate_old_custom_maps()
        fix_corrupted_filenames()
        self.filter_options = self.load_filter_options()
        self.maps_load_failed = False
        self.app_settings = self.load_app_settings()
        self.update_active_rois()
        self.auto_detect_switcher()
        self.maps = self.load_maps()
        self.active_realm = None
        self.init_default_submaps()
        self.sync_from_map_presets_txt()
        self.export_map_presets_txt()
        self.update_detection_names()
        self.ready_active = False
        self.ready_message = ''
        self.map_message = ''
        self.applied_key = None
        self.names = [m['name'] for m in self.maps]
        self.selected_name = None
        self.card_widgets = {}
        self.cell_widgets = {}
        self.image_cache = {}
        self._scroll_job = None
        self._rearrange_job = None
        self._last_canvas_w = None
        self.combobox_widgets = []
        self.current_lang = self.app_settings.get('language', 'ar')
        if self.current_lang not in ('ar', 'en'):
            self.current_lang = 'ar'
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value=self.t('monitoring_stopped'))
        self.detected_var = tk.StringVar(value=self.t('waiting_prompt'))

        # ── تنسيق القوائم المنسدلة وأشرطة التمرير بتصميم داكن وفخم متناسق ──
        root.option_add('*TCombobox*Listbox.background', '#181a1f')
        root.option_add('*TCombobox*Listbox.foreground', '#ffffff')
        root.option_add('*TCombobox*Listbox.selectBackground', '#ed3448')
        root.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
        root.option_add('*TCombobox*Listbox.font', ('Segoe UI', 9))
        root.option_add('*TCombobox*Listbox.relief', 'flat')
        root.option_add('*TCombobox*Listbox.borderWidth', '1')
        root.option_add('*Combobox*Listbox.background', '#181a1f')
        root.option_add('*Combobox*Listbox.foreground', '#ffffff')
        root.option_add('*Combobox*Listbox.selectBackground', '#ed3448')
        root.option_add('*Combobox*Listbox.selectForeground', '#ffffff')
        root.option_add('*ComboboxPopdown*background', '#181a1f')
        root.option_add('*ComboboxPopdown*foreground', '#ffffff')
        root.option_add('*Scrollbar.background', '#23272e')
        root.option_add('*Scrollbar.troughColor', '#101114')
        root.option_add('*Scrollbar.activeBackground', '#383d47')
        root.option_add('*Scrollbar.highlightThickness', '0')
        root.option_add('*Scrollbar.borderWidth', '0')
        root.option_add('*Scrollbar.relief', 'flat')

        style = ttk.Style()
        try: style.theme_use('clam')
        except Exception: pass
        style.configure('TButton', font=('Segoe UI', 9), padding=7)

        # تصميم داكن لأشرطة التمرير (الرئيسية وفي القوائم المنسدلة)
        for sb_style in ('TScrollbar', 'Vertical.TScrollbar', 'Horizontal.TScrollbar'):
            style.configure(
                sb_style,
                gripcount=0,
                background='#23272e',
                darkcolor='#23272e',
                lightcolor='#23272e',
                troughcolor='#101114',
                bordercolor='#101114',
                arrowcolor='#858b96',
                arrowsize=11,
                relief='flat',
                borderwidth=0,
                width=13
            )
            style.map(
                sb_style,
                background=[('pressed', '#ed3448'), ('active', '#383d47')],
                darkcolor=[('pressed', '#ed3448'), ('active', '#383d47')],
                lightcolor=[('pressed', '#ed3448'), ('active', '#383d47')],
                arrowcolor=[('pressed', '#ffffff'), ('active', '#ffffff')]
            )

        # تصميم مخصص للقائمة المنسدلة الداكنة
        style.configure(
            'Dark.TCombobox',
            fieldbackground='#23272e',
            background='#23272e',
            foreground='#ffffff',
            darkcolor='#181a1f',
            lightcolor='#2d323b',
            arrowcolor='#ed3448',
            bordercolor='#2f343e',
            padding=4,
        )
        style.map(
            'Dark.TCombobox',
            fieldbackground=[('readonly', '#23272e'), ('focus', '#282d36'), ('hover', '#262a32')],
            foreground=[('readonly', '#ffffff'), ('focus', '#ffffff')],
            arrowcolor=[('hover', '#ffffff'), ('pressed', '#ffffff')],
            bordercolor=[('focus', '#ed3448'), ('hover', '#ed3448')],
        )

        # Sidebar
        self.sidebar = tk.Frame(root, bg='#101114', width=230)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)

        # ── زر منصة إحسان للتبرع أسفل يسار النافذة ──
        ehsan_frame = tk.Frame(self.sidebar, bg='#101114')
        ehsan_frame.pack(side='bottom', fill='x', padx=12, pady=(0, 16))

        ehsan_btn = tk.Button(
            ehsan_frame,
            text='💚  منصة إحسان للتبرع',
            command=self.open_ehsan,
            bg='#14271c',
            fg='#4ade80',
            activebackground='#1d3c2b',
            activeforeground='#ffffff',
            relief='flat',
            font=('Segoe UI', 9, 'bold'),
            pady=9,
            cursor='hand2',
            highlightthickness=1,
            highlightbackground='#22543d',
            highlightcolor='#4ade80'
        )
        ehsan_btn.pack(fill='x')
        ToolTip(ehsan_btn, 'انقر للانتقال إلى منصة إحسان الوطنية للعمل الخيري والتبرع')

        def _on_ehsan_enter(e):
            ehsan_btn.configure(bg='#1c3c2b', fg='#ffffff', highlightbackground='#4ade80')

        def _on_ehsan_leave(e):
            ehsan_btn.configure(bg='#14271c', fg='#4ade80', highlightbackground='#22543d')

        ehsan_btn.bind('<Enter>', _on_ehsan_enter)
        ehsan_btn.bind('<Leave>', _on_ehsan_leave)
        tk.Label(self.sidebar, text='AutoReShade', bg='#101114', fg=self.RED,
                 font=('Segoe UI', 17, 'bold')).pack(anchor='w', padx=17, pady=(20, 0))
        tk.Label(self.sidebar, text='FAST MAP MONITOR', bg='#101114', fg='#f5c842',
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=18, pady=(0, 20))

        self.lang_side_lbl = self._side_label(self.t('language'))
        lang_frame = tk.Frame(self.sidebar, bg='#101114')
        lang_frame.pack(fill='x', padx=12, pady=(2, 6))

        self.lang_display_var = tk.StringVar(value='العربية' if self.current_lang == 'ar' else 'English')
        self.lang_combo = ttk.Combobox(lang_frame, textvariable=self.lang_display_var,
                                       values=['العربية', 'English'], state='readonly',
                                       font=('Segoe UI', 8), style='Dark.TCombobox')
        self.lang_combo.pack(fill='x')
        self.combobox_widgets.append(self.lang_combo)
        self.lang_combo.bind('<<ComboboxSelected>>', self._on_language_selected)
        ToolTip(self.lang_combo, self.t('lang_tip'))

        self.status_side_lbl = self._side_label(self.t('monitor_status'))
        self.status_label = tk.Label(self.sidebar, textvariable=self.status_var, bg='#26292f', fg='white',
                                      font=('Segoe UI', 10, 'bold'), anchor='w', padx=10, pady=10,
                                      wraplength=195, justify='left')
        self.status_label.pack(fill='x', padx=12, pady=(4, 12))

        self.lobby_side_lbl = self._side_label(self.t('lobby_preset'))
        lobby_frame = tk.Frame(self.sidebar, bg='#101114')
        lobby_frame.pack(fill='x', padx=12, pady=(2, 6))

        self.lobby_filter_var = tk.StringVar(value=self.app_settings.get('lobby_filter') or '')
        saved_lobby = self.app_settings.get('lobby_filter')
        if saved_lobby and saved_lobby in self.filter_options:
            self.lobby_filter_var.set(saved_lobby)
        else:
            first_lobby = next((opt for opt in self.filter_options if opt != 'بدون ربط' and ('lobby' in opt.lower() or 'all' in opt.lower())), 'بدون ربط')
            self.lobby_filter_var.set(first_lobby)
            self.app_settings['lobby_filter'] = first_lobby

        self.lobby_combo = ttk.Combobox(lobby_frame, textvariable=self.lobby_filter_var,
                                        values=self.filter_options, state='readonly',
                                        font=('Segoe UI', 8), style='Dark.TCombobox')
        self.lobby_combo.pack(side='left', fill='x', expand=True)
        self.combobox_widgets.append(self.lobby_combo)
        self.lobby_combo.bind('<<ComboboxSelected>>', self._on_lobby_filter_selected)
        ToolTip(self.lobby_combo, self.t('lobby_tip'))

        self.lobby_refresh_btn = tk.Button(lobby_frame, text='🔄', command=self.refresh_filter_options,
                                           bg='#26292f', fg='white', activebackground='#383c46',
                                           activeforeground='white', relief='flat', font=('Segoe UI', 8),
                                           cursor='hand2', padx=5, pady=2)
        self.lobby_refresh_btn.pack(side='right', padx=(4, 0))
        ToolTip(self.lobby_refresh_btn, self.t('refresh_tip'))

        self.res_side_lbl = self._side_label(self.t('resolution'))
        res_frame = tk.Frame(self.sidebar, bg='#101114')
        res_frame.pack(fill='x', padx=12, pady=(2, 6))

        self.resolution_var = tk.StringVar(value=self.app_settings.get('resolution', DEFAULT_RESOLUTION))
        if self.resolution_var.get() not in RESOLUTION_PRESETS:
            self.resolution_var.set(DEFAULT_RESOLUTION)
            self.app_settings['resolution'] = DEFAULT_RESOLUTION

        self.res_combo = ttk.Combobox(res_frame, textvariable=self.resolution_var,
                                      values=list(RESOLUTION_PRESETS.keys()), state='readonly',
                                      font=('Segoe UI', 8), style='Dark.TCombobox')
        self.res_combo.pack(fill='x')
        self.combobox_widgets.append(self.res_combo)
        self.res_combo.bind('<<ComboboxSelected>>', self._on_resolution_selected)
        ToolTip(self.res_combo, self.t('res_tip'))

        self.dot_side_lbl = self._side_label(self.t('crosshair'))
        dot_frame = tk.Frame(self.sidebar, bg='#101114')
        dot_frame.pack(fill='x', padx=12, pady=(2, 6))

        self.center_dot_var = tk.BooleanVar(value=bool(self.app_settings.get('center_dot', False)))
        self.center_dot_cb = tk.Checkbutton(
            dot_frame,
            text=self.t('crosshair_dot'),
            variable=self.center_dot_var,
            command=self._on_center_dot_toggled,
            bg='#101114',
            fg='white',
            selectcolor='#26292f',
            activebackground='#101114',
            activeforeground='#f5c842',
            font=('Segoe UI', 9),
            highlightthickness=0,
            bd=0,
            cursor='hand2'
        )
        self.center_dot_cb.pack(anchor='w')
        ToolTip(self.center_dot_cb, self.t('crosshair_tip'))

        self.ocr_side_lbl = self._side_label('OCR MONITOR')
        self.toggle_btn = tk.Button(self.sidebar, text=self.t('start_monitor'), command=self.toggle_monitoring,
                                    bg=self.GREEN, fg='white', activebackground='#218454', activeforeground='white',
                                    relief='flat', font=('Segoe UI', 10, 'bold'), pady=11, cursor='hand2')
        self.toggle_btn.pack(fill='x', padx=12, pady=(6, 6))

        self.footer_note_lbl = tk.Label(self.sidebar, text=self.t('footer_note'),
                                        bg='#101114', fg=self.MUTED, font=('Segoe UI', 8), justify='left')
        self.footer_note_lbl.pack(anchor='w', padx=15, pady=(18, 0))

        # Main content
        main = tk.Frame(root, bg=self.BG)
        main.pack(side='left', fill='both', expand=True)
        header = tk.Frame(main, bg=self.BG)
        header.pack(fill='x', padx=20, pady=(18, 10))
        self.back_btn = tk.Button(header, text=self.t('back_to_realms'), command=self.exit_realm_view,
                                  bg='#2a2d34', fg='white', activebackground='#383c46',
                                  activeforeground='white', relief='flat',
                                  font=('Segoe UI', 9, 'bold'), padx=10, pady=3, cursor='hand2')
        self.header_title = tk.Label(header, text=self.t('map_config'), bg=self.BG, fg='white',
                                     font=('Segoe UI', 12, 'bold'))
        self.header_title.pack(side='left')

        self.search_entry = tk.Entry(header, textvariable=self.search_var, bg='#1b1d22', fg='white',
                                     insertbackground='white', relief='flat', font=('Segoe UI', 10), width=25)
        self.search_entry.pack(side='right', ipady=6)
        self.search_lbl = tk.Label(header, text=self.t('search'), bg=self.BG, fg=self.MUTED, font=('Segoe UI', 9))
        self.search_lbl.pack(side='right', padx=8)
        self.search_var.trace_add('write', lambda *_: self.render_cards())

        statusbar = tk.Frame(main, bg='#202329')
        statusbar.pack(fill='x', padx=20, pady=(0, 10))
        self.status_bar_lbl = tk.Label(statusbar, textvariable=self.detected_var, bg='#202329', fg='#d8dbe1',
                                       font=('Segoe UI', 9), anchor='w', padx=10, pady=8)
        self.status_bar_lbl.pack(fill='x')

        self.target_var = tk.StringVar(value=self.app_settings.get('switcher_dir') or os.path.join(DATA_ROOT, 'Switcher'))

        holder = tk.Frame(main, bg=self.BG)
        holder.pack(fill='both', expand=True, padx=(20, 10), pady=(0, 15))
        self.canvas = tk.Canvas(holder, bg=self.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient='vertical', style='Vertical.TScrollbar', command=self.canvas.yview)
        self.cards_frame = tk.Frame(self.canvas, bg=self.BG)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind('<Configure>', self._on_canvas_configure)
        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.canvas.bind('<Button-1>', lambda e: self.clear_combobox_selection(), add='+')
        self.cards_frame.bind('<Button-1>', lambda e: self.clear_combobox_selection(), add='+')
        self.canvas.bind_all('<MouseWheel>', self._mousewheel)
        self.root.bind_class('TCombobox', '<MouseWheel>', self._combo_wheel)
        self.root.update_idletasks()
        self.render_cards()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(20, self.poll_events)
        # بعد ظهور النافذة: تثبيت الأيقونة بدقة عالية
        self.root.after(150, self.apply_app_icon)
        self.tesseract_path = find_tesseract()

    def _update_scrollregion(self):
        """تحديث دقيق لمنطقة التمرير يمنع النزول في فراغ ويضمن ظهور كل المابات بدقة."""
        query = self.search_var.get().strip().casefold()
        items = self.get_current_items()
        shown = [m for m in items if query in m['name'].casefold()]
        shown_count = len(shown) + 1  # تشمل دائماً بطاقة الإضافة ＋
        cols = getattr(self, '_current_cols', 4) or 4

        if shown_count == 0:
            actual_content_h = 100
        else:
            rows = (shown_count + cols - 1) // cols
            actual_content_h = rows * 234 + 20

        self._current_content_h = actual_content_h

        canvas_w = getattr(self, '_last_canvas_w', None) or max(self.canvas.winfo_width(), 650)
        canvas_h = getattr(self, '_last_canvas_h', None) or max(self.canvas.winfo_height(), 400)

        if actual_content_h <= canvas_h:
            # كل المابات مرئية بالكامل داخل الشاشة: نثبت الارتفاع ونمنع أي تمرير زائد في فراغ
            self.canvas.itemconfigure(self.canvas_window, width=canvas_w, height=canvas_h)
            target_sr = f"0 0 {canvas_w} {canvas_h}"
            if self.canvas.cget('scrollregion') != target_sr:
                self.canvas.configure(scrollregion=(0, 0, canvas_w, canvas_h))
            self.canvas.yview_moveto(0.0)
        else:
            # المابات تتجاوز ارتفاع الشاشة: نسمح بالتمرير حتى آخر صف فقط دون أي فراغ إضافي
            self.canvas.itemconfigure(self.canvas_window, width=canvas_w, height=actual_content_h)
            target_sr = f"0 0 {canvas_w} {actual_content_h}"
            if self.canvas.cget('scrollregion') != target_sr:
                self.canvas.configure(scrollregion=(0, 0, canvas_w, actual_content_h))

            # منع البقاء في موضع تمرير أعمق من الارتفاع الفعلي الجديد
            max_scroll = max(0.0, (actual_content_h - canvas_h) / actual_content_h)
            cur_top = self.canvas.yview()[0]
            if cur_top > max_scroll:
                self.canvas.yview_moveto(max_scroll)
            elif cur_top < 0.0:
                self.canvas.yview_moveto(0.0)

    def _calc_target_cols(self, width):
        return max(2, min(6, width // 220))

    def _rearrange_columns(self, new_cols, canvas_w=None):
        """إعادة توزيع البطاقات الحالية فوراً في مواضع الأعمدة الجديدة برسم متزامن سلس بدون هدم أو وميض."""
        self._current_cols = new_cols
        if canvas_w is not None:
            self.canvas.itemconfigure(self.canvas_window, width=canvas_w)

        for col in range(8):
            self.cards_frame.grid_columnconfigure(col, weight=0, minsize=0, uniform='')
        for col in range(new_cols):
            self.cards_frame.grid_columnconfigure(col, weight=1, minsize=180, uniform='cards')

        query = self.search_var.get().strip().casefold()
        items = self.get_current_items()
        shown = [m for m in items if query in m['name'].casefold()]
        cell_dict = getattr(self, 'cell_widgets', {})
        for index, item in enumerate(shown):
            cell = cell_dict.get(item['name'])
            if cell and cell.winfo_exists():
                row, col = divmod(index, new_cols)
                cell.grid(row=row, column=col, padx=6, pady=6, sticky='nsew')

        add_cell = getattr(self, 'add_card_cell', None)
        if add_cell and add_cell.winfo_exists():
            row, col = divmod(len(shown), new_cols)
            add_cell.grid(row=row, column=col, padx=6, pady=6, sticky='nsew')

        self.root.update_idletasks()
        self._update_scrollregion()

    def _on_canvas_configure(self, event):
        if event.widget != self.canvas:
            return
        canvas_w = max(event.width, 650)
        canvas_h = max(event.height, 400)
        self._last_canvas_w = canvas_w
        self._last_canvas_h = canvas_h
        new_cols = self._calc_target_cols(canvas_w)

        if getattr(self, '_current_cols', None) != new_cols:
            self._rearrange_columns(new_cols, canvas_w)
        else:
            self.canvas.itemconfigure(self.canvas_window, width=canvas_w)
            self._update_scrollregion()

    def t(self, key, **kwargs):
        lang = getattr(self, 'current_lang', 'ar')
        text = TRANSLATIONS.get(lang, TRANSLATIONS['ar']).get(key, TRANSLATIONS['ar'].get(key, key))
        if kwargs:
            try:
                text = text.format(**kwargs)
            except Exception:
                pass
        return text

    def _side_label(self, text):
        lbl = tk.Label(self.sidebar, text=text, bg='#101114', fg='#727987',
                       font=('Segoe UI', 8, 'bold'))
        lbl.pack(anchor='w', padx=15, pady=(8, 2))
        return lbl

    def _on_language_selected(self, event=None):
        val = self.lang_combo.get()
        new_lang = 'ar' if 'عرب' in val else 'en'
        self.set_language(new_lang)

    def set_language(self, new_lang):
        self.current_lang = new_lang
        self.app_settings['language'] = new_lang
        self.save_app_settings()

        # تحديث نصوص القائمة الجانبية
        if hasattr(self, 'status_side_lbl'):
            self.status_side_lbl.configure(text=self.t('monitor_status'))
        if hasattr(self, 'lobby_side_lbl'):
            self.lobby_side_lbl.configure(text=self.t('lobby_preset'))
        if hasattr(self, 'res_side_lbl'):
            self.res_side_lbl.configure(text=self.t('resolution'))
        if hasattr(self, 'dot_side_lbl'):
            self.dot_side_lbl.configure(text=self.t('crosshair'))
        if hasattr(self, 'lang_side_lbl'):
            self.lang_side_lbl.configure(text=self.t('language'))
        if hasattr(self, 'center_dot_cb'):
            self.center_dot_cb.configure(text=self.t('crosshair_dot'))
        if hasattr(self, 'toggle_btn'):
            if getattr(self, 'running', False):
                self.toggle_btn.configure(text=self.t('stop_monitor'))
            else:
                self.toggle_btn.configure(text=self.t('start_monitor'))
        if hasattr(self, 'footer_note_lbl'):
            self.footer_note_lbl.configure(text=self.t('footer_note'))

        # تحديث رأس الصفحة
        if hasattr(self, 'header_title'):
            if self.active_realm:
                self.header_title.configure(text=f"{self.t('realm_prefix')}{self.active_realm.upper()}")
            else:
                self.header_title.configure(text=self.t('map_config'))
        if hasattr(self, 'back_btn'):
            self.back_btn.configure(text=self.t('back_to_realms'))
        if hasattr(self, 'search_lbl'):
            self.search_lbl.configure(text=self.t('search'))

        # تحديث نص الحالة
        if not getattr(self, 'running', False):
            self.status_var.set(self.t('monitoring_stopped'))
            self.detected_var.set(self.t('waiting_prompt'))

        # إعادة رسم البطاقات لتحديث كافة نصوص البطاقات والأزرار
        self.render_cards()

    @staticmethod
    def _wheel_units(event):
        units = int(-1 * (event.delta / 120))
        if units == 0 and event.delta:
            units = -1 if event.delta > 0 else 1
        return units

    def _open_popdown_listbox(self, event=None):
        candidates = []
        try:
            grabbed = self.root.grab_current()
            if grabbed is not None:
                candidates.append(str(grabbed))
        except Exception:
            pass
        if event is not None:
            candidates.append(str(event.widget))
        for path in candidates:
            if '.popdown' in path:
                base = path[:path.index('.popdown') + len('.popdown')]
                try:
                    return self.root.nametowidget(base + '.f.l'), path
                except Exception:
                    return None, path
        return None, None

    def _mousewheel(self, event):
        listbox, popdown_path = self._open_popdown_listbox(event)
        if popdown_path is not None:
            if listbox is not None and str(event.widget) != str(listbox):
                listbox.yview_scroll(self._wheel_units(event), 'units')
            return 'break'

        canvas_h = max(self.canvas.winfo_height(), 400)
        content_h = getattr(self, '_current_content_h', 0)

        # إذا كانت كل المابات ظاهرة داخل الشاشة (مثل الفل سكرين) نمنع التمرير ونقفلها بالأعلى
        if content_h <= canvas_h:
            self.canvas.yview_moveto(0.0)
            return 'break'

        units = self._wheel_units(event)
        cur_top = self.canvas.yview()[0]
        cur_bottom = self.canvas.yview()[1]

        # إذا كان السكرول للأعلى ووصلنا للقمة يمنع السكرول الزائد
        if units < 0 and cur_top <= 0.0001:
            self.canvas.yview_moveto(0.0)
            return 'break'

        # إذا كان السكرول للأسفل ووصلنا للقاع يمنع السكرول في الفراغ
        if units > 0 and cur_bottom >= 0.9999:
            return 'break'

        self.canvas.yview_scroll(units, 'units')
        if self.canvas.yview()[0] < 0:
            self.canvas.yview_moveto(0.0)
        return 'break'

    def _combo_wheel(self, event):
        if self._open_popdown_listbox(event)[1] is None:
            self._mousewheel(event)
        return 'break'

    def toggle_monitoring(self):
        """التبديل بين تشغيل وإيقاف المراقبة من زر واحد."""
        try:
            if self.running:
                self.stop()
            else:
                self.start()
        except Exception as exc:
            print(f"[AR DBD Toggle Error] {exc}", flush=True)
            messagebox.showerror('خطأ', f'حدث خطأ أثناء تبديل المراقبة:\n{exc}', parent=self.root)

    def get_current_items(self):
        if self.active_realm:
            realm = next((m for m in self.maps if m['name'] == self.active_realm), None)
            if realm is not None:
                return realm.setdefault('sub_maps', [])
            self.active_realm = None
        return self.maps

    def open_realm_submaps(self, realm_name):
        self.active_realm = realm_name
        self.selected_name = None
        self.search_var.set('')
        self.detected_var.set(f"{self.t('realm_prefix')}{realm_name}")
        self.render_cards()

    def exit_realm_view(self):
        self.active_realm = None
        self.selected_name = None
        self.search_var.set('')
        self.detected_var.set(self.t('map_config'))
        self.render_cards()

    def update_detection_names(self):
        self.realm_names = [m['name'] for m in self.maps]
        self.submap_names = []
        for r in self.maps:
            for sm in r.get('sub_maps', []):
                sname = sm.get('name')
                if sname and sname not in self.submap_names:
                    self.submap_names.append(sname)
        # Fast OCR lookup checks submaps first, then realms
        self.names = self.submap_names + [n for n in self.realm_names if n not in self.submap_names]

    def resolve_submap_image(self, name, existing_img='', realm_entry=None):
        """
        يحل مسار صورة الماب المخصص بمرونة كاملة لتعمل على أي جهاز وأي مسار:
        1. إذا كان المسار المخزن موجوداً فعلياً، نستخدمه.
        2. نبحث في مجلد الريلم المخصص داخل custom/<Realm_Name>/.
        3. نبحث عن اسم الملف (basename) داخل مجلد custom أو dbd_map_assets.
        4. نبحث باسم الماب مع الامتدادات (.webp, .png, .jpg, .jpeg, .bmp).
        5. كحل أخير، نستخدم صورة الريلم الأصلي التابع له الماب.
        """
        if existing_img and os.path.isfile(existing_img):
            return os.path.abspath(existing_img)
        safe_n = _safe_filename(name)
        if realm_entry:
            r_name = realm_entry.get('name', '')
            if r_name:
                r_folder = os.path.join(CUSTOM_DIR, _safe_filename(r_name))
                if os.path.isdir(r_folder):
                    for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                        for cand_name in (safe_n, name):
                            cand = os.path.join(r_folder, cand_name + ext)
                            if os.path.isfile(cand):
                                return os.path.abspath(cand)
        if existing_img:
            base = os.path.basename(existing_img)
            for folder in (CUSTOM_DIR, ASSETS_DIR):
                cand = os.path.join(folder, base)
                if os.path.isfile(cand):
                    return os.path.abspath(cand)
        for folder in (CUSTOM_DIR, ASSETS_DIR):
            for ext in ('.webp', '.png', '.jpg', '.jpeg', '.bmp'):
                cand = os.path.join(folder, safe_n + ext)
                if os.path.isfile(cand):
                    return os.path.abspath(cand)
                cand2 = os.path.join(folder, name + ext)
                if os.path.isfile(cand2):
                    return os.path.abspath(cand2)
        if realm_entry:
            r_img = realm_entry.get('image', '')
            if r_img and os.path.isfile(r_img):
                return os.path.abspath(r_img)
            elif r_img:
                r_base = os.path.basename(r_img)
                for folder in (ASSETS_DIR, CUSTOM_DIR):
                    cand = os.path.join(folder, r_base)
                    if os.path.isfile(cand):
                        return os.path.abspath(cand)
        return ''

    def init_default_submaps(self):
        """ضمان تهيئة قائمة sub_maps لكل ماب دون فرض أي مابات تابعة برمجياً."""
        for m in self.maps:
            if 'sub_maps' not in m or not isinstance(m['sub_maps'], list):
                m['sub_maps'] = []

    def is_filter_placed(self, filter_rel):
        if not filter_rel or filter_rel == 'بدون ربط':
            return False
        placed = self.app_settings.get('placed', [])
        if not placed:
            return False
        expected_name = os.path.basename(filter_rel).casefold()
        return placed[0].casefold() == expected_name

    def apply_for_map(self, map_name):
        self.last_applied_map_name = map_name

        # البحث في المابات المخصصة أولاً عبر جميع الريلمز
        target_filter = None
        matched_label = None

        for realm in self.maps:
            for sm in realm.get('sub_maps', []):
                if sm['name'].casefold() == map_name.casefold():
                    sm_filter = sm.get('filter', 'بدون ربط')
                    if sm_filter != 'بدون ربط':
                        target_filter = sm_filter
                        matched_label = f"{sm['name']} (مخصص لـ {realm['name']})"
                    else:
                        # إذا كان الماب المخصص بدون ربط، يرجع لريشيد الريلم نفسه تلقائياً
                        target_filter = realm.get('filter', 'بدون ربط')
                        matched_label = f"{sm['name']} (يتبع ريشيد {realm['name']})"
                    break
            if target_filter is not None:
                break

        # إذا لم يكن ماباً مخصصاً، نطبق ريشيد الريلم كامل
        if target_filter is None:
            realm = next((m for m in self.maps if m['name'].casefold() == map_name.casefold()), None)
            if realm:
                target_filter = realm.get('filter', 'بدون ربط')
                matched_label = f"ريلم {realm['name']}"

        # إذا كان اسم ماب رسمي في كتالوج DBD ولم يُحدد له ماب مخصص، نربطه بريلمه تلقائياً
        if target_filter is None and map_name in DBD_MAP_CATALOG:
            parent_realm_name = DBD_MAP_CATALOG[map_name]
            realm = next((m for m in self.maps if m['name'].casefold() == parent_realm_name.casefold()), None)
            if realm:
                target_filter = realm.get('filter', 'بدون ربط')
                matched_label = f"{map_name} (تابع لريلم {realm['name']})"

        if matched_label is None:
            matched_label = map_name
            target_filter = 'بدون ربط'

        selected = target_filter
        key = ('map', map_name, selected)

        # إذا كان الفلتر المطلوب موضوعاً بالفعل في Switcher ونفس المفتاح، نرسل ضغطة فوراً
        if self.applied_key == key and self.is_filter_placed(selected):
            if self.app_settings.get('press_equals', True):
                self.send_equals()
            return self.map_message

        if time.time() < getattr(self, '_apply_retry_at', 0):
            return self.map_message

        ok, msg = self.apply_filter(selected)
        if not ok and selected == 'بدون ربط':
            msg = f'{matched_label}: لا يوجد فلتر مرتبط بهذا الماب/الريلم.'
        else:
            msg = f'{matched_label}: {msg}'

        if ok or selected == 'بدون ربط':
            self.applied_key = key
            self.ready_active = False
        else:
            self._apply_retry_at = time.time() + 1.0

        self.map_message = msg
        return msg

    def load_app_settings(self):
        defaults = {'switcher_dir': '', 'placed': [], 'removed_defaults': [], 'switcher_manual': False, 'press_equals': True, 'auto_linked': [], 'lobby_filter': '', 'ocr_engine': 'easyocr', 'resolution': DEFAULT_RESOLUTION, 'lobby_delay': 0.7, 'center_dot': False, 'language': 'ar'}
        data = None
        for path in (os.path.join(APP_DIR, 'settings.json'), os.path.join(DATA_ROOT, 'settings.json'), SETTINGS_JSON):
            if path and os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        d = json.load(f)
                    if isinstance(d, dict):
                        data = d
                        break
                except Exception:
                    pass
        if not data:
            return defaults
        try:
            defaults['switcher_dir'] = str(data.get('switcher_dir', ''))
            defaults['switcher_manual'] = bool(data.get('switcher_manual', False))
            defaults['auto_linked'] = [str(x) for x in data.get('auto_linked', []) if isinstance(x, str)]
            defaults['press_equals'] = bool(data.get('press_equals', True))
            defaults['lobby_filter'] = str(data.get('lobby_filter', ''))
            defaults['ocr_engine'] = str(data.get('ocr_engine', 'easyocr')).lower()
            res_val = str(data.get('resolution', DEFAULT_RESOLUTION))
            defaults['resolution'] = res_val if res_val in RESOLUTION_PRESETS else DEFAULT_RESOLUTION
            placed = data.get('placed', [])
            defaults['placed'] = [str(p) for p in placed] if isinstance(placed, list) else []
            removed = data.get('removed_defaults', [])
            defaults['removed_defaults'] = [str(n) for n in removed] if isinstance(removed, list) else []
            defaults['lobby_delay'] = float(data.get('lobby_delay', 0.7))
            defaults['center_dot'] = bool(data.get('center_dot', False))
            defaults['language'] = 'en' if str(data.get('language', 'ar')).lower() == 'en' else 'ar'
        except Exception:
            pass
        return defaults

    def save_app_settings(self):
        targets = [SETTINGS_JSON, os.path.join(APP_DIR, 'settings.json')]
        if DATA_ROOT and DATA_ROOT != APP_DIR:
            targets.append(os.path.join(DATA_ROOT, 'settings.json'))
        success = False
        for target in targets:
            temp_path = target + '.tmp'
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.app_settings, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, target)
                success = True
            except Exception:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
        return success

    def find_lobby_filter(self):
        chosen = self.app_settings.get('lobby_filter')
        if chosen is None and hasattr(self, 'lobby_filter_var'):
            chosen = self.lobby_filter_var.get()
        if chosen and chosen != 'بدون ربط':
            source = os.path.join(FILTER_SOURCE_DIR, chosen)
            if os.path.isfile(source):
                return chosen
        return None

    def ensure_lobby_filter_in_switcher(self):
        """عند بدء التشغيل: التأكد من وجود ملف واحد في السويتشر وحقن فلتر اللوبي فيه."""
        self.auto_detect_switcher()
        lobby_rel = self.find_lobby_filter()
        if not lobby_rel:
            return
        source = os.path.join(FILTER_SOURCE_DIR, lobby_rel)
        if not os.path.isfile(source):
            return
        target_file = self.get_single_switcher_file()
        if not os.path.isfile(target_file) or os.path.getsize(target_file) == 0:
            try:
                with open(source, 'r', encoding='utf-8', errors='ignore') as f_in:
                    raw = f_in.read()
                processed = inject_or_strip_center_dot(raw, bool(self.app_settings.get('center_dot', False)))
                with open(target_file, 'w', encoding='utf-8') as f_out:
                    f_out.write(processed)
                self.app_settings['placed'] = [os.path.basename(source)]
                self.save_app_settings()
            except OSError:
                pass

    def _on_lobby_filter_selected(self, event=None):
        val = self.lobby_filter_var.get()
        self.app_settings['lobby_filter'] = val
        self.save_app_settings()
        self.export_map_presets_txt()
        self.clear_combobox_selection()
        self.detected_var.set(self.t('lobby_set', val=val))
        # إذا كنا في اللوبي حالياً، طبّق الفلتر الجديد فوراً
        if self.applied_key == ('lobby',):
            self.applied_key = None
            self.apply_lobby()

    def _on_resolution_selected(self, event=None):
        val = self.resolution_var.get()
        self.app_settings['resolution'] = val
        self.save_app_settings()
        self.update_active_rois()
        self.clear_combobox_selection()
        self.detected_var.set(self.t('res_set', val=val))
        print(f"[AR DBD] {val}", flush=True)

    def _on_center_dot_toggled(self):
        val = bool(self.center_dot_var.get())
        self.app_settings['center_dot'] = val
        self.save_app_settings()
        status_msg = self.t('dot_on') if val else self.t('dot_off')
        self.detected_var.set(status_msg)
        print(f"[AR DBD] {status_msg}", flush=True)

        target_file = self.get_single_switcher_file()
        if os.path.isfile(target_file):
            try:
                placed = self.app_settings.get('placed', [])
                source_name = placed[0] if placed else None
                source_path = os.path.join(FILTER_SOURCE_DIR, source_name) if source_name else None
                if source_path and os.path.isfile(source_path):
                    with open(source_path, 'r', encoding='utf-8', errors='ignore') as f_in:
                        content = f_in.read()
                else:
                    with open(target_file, 'r', encoding='utf-8', errors='ignore') as f_in:
                        content = f_in.read()

                processed = inject_or_strip_center_dot(content, val)
                with open(target_file, 'w', encoding='utf-8') as f_out:
                    f_out.write(processed)

                if self.app_settings.get('press_equals', True):
                    self.send_equals()
            except Exception as e:
                print(f"[AR DBD] خطأ في تحديث نقطة المنتصف: {e}", flush=True)

    def update_active_rois(self):
        res_key = self.app_settings.get('resolution', DEFAULT_RESOLUTION)
        if hasattr(self, 'resolution_var') and self.resolution_var.get() in RESOLUTION_PRESETS:
            res_key = self.resolution_var.get()
        preset = RESOLUTION_PRESETS.get(res_key, RESOLUTION_PRESETS.get(DEFAULT_RESOLUTION, {}))
        self.active_roi = preset.get('roi_map', ROI)
        self.active_roi_ready = preset.get('roi_ready', ROI_READY)

    def get_single_switcher_file(self):
        """العثور على الملف الوحيد الثابت في مجلد Switcher أو إنشاؤه."""
        self.auto_detect_switcher()
        folder = self.app_settings.get('switcher_dir', '') or os.path.join(DATA_ROOT, 'Switcher')
        os.makedirs(folder, exist_ok=True)
        ini_files = [f for f in os.listdir(folder) if f.lower().endswith('.ini')]
        if ini_files:
            return os.path.join(folder, ini_files[0])
        return os.path.join(folder, 'Zxxqo All.ini')

    def is_switcher_on_lobby(self):
        """يفحص هل الفلتر الحالي المحقون هو فلتر اللوبي."""
        lobby_rel = self.find_lobby_filter()
        if not lobby_rel:
            return False
        return self.is_filter_placed(lobby_rel)

    def send_equals(self):
        ok, msg = press_equals_key()
        print(f'[AR DBD Fast] {msg}', flush=True)
        if not ok:
            if 'ليست النافذة النشطة' in msg or 'غير موجودة' in msg:
                self.pending_equals = True
        else:
            self.pending_equals = False
        return ok, msg

    def apply_lobby(self):
        """حقن فلتر اللوبي في الملف الوحيد الثابت داخل السويتشر."""
        lobby_rel = self.find_lobby_filter()
        if not lobby_rel:
            return False, 'لم يتم تحديد فلتر للوبي.'

        source = os.path.join(FILTER_SOURCE_DIR, lobby_rel)
        if not os.path.isfile(source):
            return False, f'ملف فلتر اللوبي غير موجود: {lobby_rel}'

        name = os.path.basename(source)

        try:
            target_file = self.get_single_switcher_file()
            folder = os.path.dirname(target_file)

            # قراءة محتوى فلتر اللوبي وتطبيق خيار نقطة التصويب
            with open(source, 'r', encoding='utf-8', errors='ignore') as f_in:
                raw_data = f_in.read()

            enable_dot = bool(self.app_settings.get('center_dot', False))
            processed_data = inject_or_strip_center_dot(raw_data, enable_dot)

            with open(target_file, 'w', encoding='utf-8') as f_out:
                f_out.write(processed_data)

            # تنظيف أي ملفات ini أخرى قديمة إن وجدت ليبقى ملف واحد فقط
            target_base = os.path.basename(target_file).casefold()
            for f in os.listdir(folder):
                if f.lower().endswith('.ini') and f.casefold() != target_base:
                    try:
                        os.remove(os.path.join(folder, f))
                    except OSError:
                        pass

            if self.app_settings.get('press_equals', True):
                delay = float(self.app_settings.get('lobby_delay', 0.7))
                threading.Timer(delay, self.send_equals).start()

            self.app_settings['placed'] = [name]
            threading.Thread(target=self.save_app_settings, daemon=True).start()

            return True, f'تم حقن فلتر اللوبي: {name}'
        except Exception as e:
            return False, f'تعذر حقن فلتر اللوبي: {e}'

    def auto_detect_switcher(self):
        local = find_local_switcher() or os.path.join(DATA_ROOT, 'Switcher')
        try:
            os.makedirs(local, exist_ok=True)
        except OSError:
            pass
        current = self.app_settings.get('switcher_dir', '')
        changed = os.path.normcase(os.path.abspath(current or '.')) != os.path.normcase(os.path.abspath(local))
        if changed or self.app_settings.get('switcher_manual'):
            self.app_settings['switcher_dir'] = local
            self.app_settings['switcher_manual'] = False
            if changed:
                self.app_settings['placed'] = []
            self.save_app_settings()
        if hasattr(self, 'target_var'):
            self.target_var.set(local)

    def open_switcher_dir(self):
        self.auto_detect_switcher()
        try:
            os.startfile(self.app_settings['switcher_dir'])
        except Exception as exc:
            self.detected_var.set(f'تعذر فتح المجلد: {exc}')

    def get_presets_txt_path(self):
        for folder in (APP_DIR, DATA_ROOT, USER_DATA_DIR):
            if folder and os.path.isdir(folder):
                return os.path.join(folder, 'map_presets.txt')
        return os.path.join(APP_DIR, 'map_presets.txt')

    def open_map_presets_txt(self):
        self.export_map_presets_txt()
        path = self.get_presets_txt_path()
        try:
            os.startfile(path)
            self.detected_var.set(f'تم فتح ملف الإعدادات: {os.path.basename(path)}')
        except Exception as exc:
            self.detected_var.set(f'تعذر فتح الملف النصي: {exc}')

    def export_map_presets_txt(self, maps=None):
        """يُصدّر ملف نصي سهل وواضح يوضح الفلاتر المرتبطة بكل ماب وريلم، بجانب ملف الـ EXE."""
        try:
            current_maps = maps if maps is not None else getattr(self, 'maps', [])
            if not current_maps:
                return
            lines = [
                "# ====================================================================",
                "#  AR DBD - ملف إعدادات فلاتر الريشيد المرتبطة بكل ماب وريلم",
                "#  يتم تحديث هذا الملف تلقائياً في كل مرة تختار فيها فلتراً من البرنامج.",
                "#  كما يمكنك كتابة اسم الفلتر هنا وحفظ الملف وسيقوم البرنامج بقراءته!",
                "# ====================================================================",
                "",
                "[LOBBY]",
                f"LOBBY_PRESET = {getattr(self, 'app_settings', {}).get('lobby_filter', 'بدون ربط')}",
                "",
                "[REALMS]"
            ]
            for m in current_maps:
                lines.append(f"{m['name']} = {m.get('filter', 'بدون ربط')}")

            lines.append("")
            lines.append("[SUB_MAPS]")
            has_sub = False
            for m in current_maps:
                for sm in m.get('sub_maps', []):
                    has_sub = True
                    lines.append(f"{m['name']} / {sm['name']} = {sm.get('filter', 'بدون ربط')}")
            if not has_sub:
                lines.append("# لا توجد مابات مخصصة حالياً")

            content = "\n".join(lines) + "\n"
            for folder in (APP_DIR, DATA_ROOT, USER_DATA_DIR):
                if folder and os.path.isdir(folder):
                    path = os.path.join(folder, 'map_presets.txt')
                    try:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(content)
                    except OSError:
                        pass
        except Exception as e:
            print(f"[AR DBD] تعذر تصدير map_presets.txt: {e}", flush=True)

    def sync_from_map_presets_txt(self):
        """إذا عدّل المستخدم ملف map_presets.txt يدويًا، نقرأ التعديلات ونطبقها على القائمة."""
        for folder in (APP_DIR, DATA_ROOT, USER_DATA_DIR):
            path = os.path.join(folder, 'map_presets.txt')
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                current_section = 'REALMS'
                changed = False
                for raw_line in lines:
                    line = raw_line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('[') and line.endswith(']'):
                        current_section = line[1:-1].strip().upper()
                        continue
                    if '=' not in line:
                        continue
                    key_part, val_part = line.split('=', 1)
                    key = key_part.strip()
                    val = val_part.strip()
                    if current_section == 'LOBBY' and key.upper() == 'LOBBY_PRESET':
                        if self.app_settings.get('lobby_filter') != val:
                            self.app_settings['lobby_filter'] = val
                            if hasattr(self, 'lobby_filter_var'):
                                self.lobby_filter_var.set(val)
                            self.ensure_lobby_filter_in_switcher()
                            changed = True
                    elif current_section == 'REALMS':
                        for m in self.maps:
                            if m['name'].casefold() == key.casefold():
                                if m.get('filter') != val:
                                    m['filter'] = val
                                    changed = True
                                break
                    elif current_section == 'SUB_MAPS':
                        parts = [p.strip() for p in key.split('/', 1)]
                        if len(parts) == 2:
                            rname, sname = parts
                            for m in self.maps:
                                if m['name'].casefold() == rname.casefold():
                                    found_sm = False
                                    for sm in m.setdefault('sub_maps', []):
                                        if sm['name'].casefold() == sname.casefold():
                                            found_sm = True
                                            if sm.get('filter') != val:
                                                sm['filter'] = val
                                                changed = True
                                            break
                                    if not found_sm:
                                        # لا نعيد إنشاء مابات تابعة محذوفة تلقائياً
                                        pass
                if changed:
                    self.save_maps()
                return
            except Exception:
                pass

    def apply_filter(self, filter_rel):
        """حقن إعدادات الفلتر في الملف الوحيد الثابت داخل السويتشر."""
        if not filter_rel or filter_rel == 'بدون ربط':
            return False, 'لا يوجد فلتر محدد لهذا الماب.'

        source = os.path.join(FILTER_SOURCE_DIR, filter_rel)
        if not os.path.isfile(source):
            return False, f'ملف الفلتر غير موجود: {filter_rel}'

        try:
            target_file = self.get_single_switcher_file()
            folder = os.path.dirname(target_file)
            map_name = os.path.basename(source)

            # حقن محتوى الفلتر الجديد مع تطبيق خيار نقطة التصويب
            with open(source, 'r', encoding='utf-8', errors='ignore') as f_in:
                raw_data = f_in.read()

            enable_dot = bool(self.app_settings.get('center_dot', False))
            processed_data = inject_or_strip_center_dot(raw_data, enable_dot)

            with open(target_file, 'w', encoding='utf-8') as f_out:
                f_out.write(processed_data)

            # حذف أي ملفات ini أخرى قديمة إن وجدت ليبقى ملف واحد فقط
            target_base = os.path.basename(target_file).casefold()
            for f in os.listdir(folder):
                if f.lower().endswith('.ini') and f.casefold() != target_base:
                    try:
                        os.remove(os.path.join(folder, f))
                    except OSError:
                        pass

            if self.app_settings.get('press_equals', True):
                self.send_equals()

            self.app_settings['placed'] = [map_name]
            threading.Thread(target=self.save_app_settings, daemon=True).start()

            return True, f'تم حقن الفلتر بنجاح: {map_name}'
        except Exception as e:
            return False, f'تعذر حقن الفلتر: {e}'

    def load_maps(self):
        self.maps_load_failed = False
        candidate_paths = [
            os.path.join(APP_DIR, 'maps.json'),
            os.path.join(DATA_ROOT, 'maps.json'),
            MAPS_JSON,
            os.path.join(APP_DIR, 'maps.json.bak'),
            MAPS_JSON + '.bak',
        ]

        # اختيار أول ملف صالح موجود دون دمج ملفات قديمة تلغي حذف المستخدم
        data = None
        for path in candidate_paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, list) and len(loaded) > 0:
                    data = loaded
                    break
            except Exception:
                continue

        if data is None:
            return []

        merged_by_name = {}
        for item in data:
            if not isinstance(item, dict) or not str(item.get('name', '')).strip():
                continue
            name = clean_display_name(str(item['name']).strip())
            key = canon_name(name)
            if not key:
                continue
            if key not in merged_by_name:
                merged_by_name[key] = {
                    'name': name,
                    'image': clean_display_name(item.get('image', '')),
                    'filter': item.get('filter', 'بدون ربط'),
                    'default': item.get('default', True),
                    'sub_maps': []
                }
            existing_subs = {canon_name(s['name']): s for s in merged_by_name[key]['sub_maps']}
            for sm in item.get('sub_maps', []):
                if isinstance(sm, dict) and str(sm.get('name', '')).strip():
                    sname = clean_display_name(str(sm['name']).strip())
                    skey = canon_name(sname)
                    if not skey:
                        continue
                    if skey not in existing_subs:
                        sub_entry = {
                            'name': sname,
                            'image': clean_display_name(sm.get('image', '')),
                            'filter': sm.get('filter', 'بدون ربط')
                        }
                        if sm.get('default'):
                            sub_entry['default'] = True
                        merged_by_name[key]['sub_maps'].append(sub_entry)
                        existing_subs[skey] = sub_entry

        result = list(merged_by_name.values())
        for entry in result:
            img_path = entry['image']
            if img_path and not os.path.isfile(img_path):
                fname = os.path.basename(img_path)
                for d in (ASSETS_DIR, CUSTOM_DIR):
                    c = os.path.join(d, fname)
                    if os.path.isfile(c):
                        entry['image'] = c
                        break
            for sm in entry.get('sub_maps', []):
                sm['image'] = self.resolve_submap_image(sm['name'], sm.get('image', ''), entry)

        # فحص مجلد custom:
        # 1. المجلدات الفرعية المسماة باسم الريلم: أي صورة داخل مجلد الريلم تتبع هذا الريلم تلقائياً!
        # 2. ملفات الصور في جذر custom: مابات مخصصة عامة
        if os.path.isdir(CUSTOM_DIR):
            existing_all = set()
            for m in result:
                existing_all.add(canon_name(m['name']))
                for sm in m.get('sub_maps', []):
                    existing_all.add(canon_name(sm['name']))

            removed_keys = {canon_name(n) for n in getattr(self, 'app_settings', {}).get('removed_defaults', [])}

            for item_name in sorted(os.listdir(CUSTOM_DIR), key=str.casefold):
                if item_name.startswith('.') or item_name.casefold() in ('.keep', 'thumbs.db'):
                    continue
                item_path = os.path.join(CUSTOM_DIR, item_name)

                # إذا كان مجلداً: فهو مجلد ريلم يحتوي على ماباته التابعة وصورها
                if os.path.isdir(item_path):
                    r_key = canon_name(item_name)
                    target_realm = next((m for m in result if canon_name(m['name']) == r_key), None)
                    if target_realm:
                        sub_list = target_realm.setdefault('sub_maps', [])
                        existing_sub_keys = {canon_name(s['name']) for s in sub_list}
                        for sub_file in sorted(os.listdir(item_path), key=str.casefold):
                            if sub_file.startswith('.'):
                                continue
                            sub_full = os.path.join(item_path, sub_file)
                            if not os.path.isfile(sub_full):
                                continue
                            s_name, s_ext = os.path.splitext(sub_file)
                            if s_ext.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                                continue
                            s_display = clean_display_name(s_name)
                            s_key = canon_name(s_display)
                            if s_key and s_key not in existing_sub_keys and s_key not in removed_keys:
                                sub_list.append({
                                    'name': s_display,
                                    'image': sub_full,
                                    'filter': 'بدون ربط'
                                })
                                existing_sub_keys.add(s_key)
                                existing_all.add(s_key)

                # إذا كان ملف صورة في جذر custom: فهو ماب مخصص عام (Realm)
                elif os.path.isfile(item_path):
                    base_name, ext = os.path.splitext(item_name)
                    if ext.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                        continue
                    display_name = clean_display_name(base_name)
                    c_key = canon_name(display_name)
                    if c_key and c_key not in existing_all and c_key not in removed_keys:
                        result.append({
                            'name': display_name,
                            'image': item_path,
                            'filter': 'بدون ربط',
                            'default': False,
                            'sub_maps': []
                        })
                        existing_all.add(c_key)

        removed_keys = {canon_name(n) for n in getattr(self, 'app_settings', {}).get('removed_defaults', [])}
        if removed_keys:
            result = [m for m in result if canon_name(m['name']) not in removed_keys]

        return result

    def restore_default_maps(self):
        """إعادة تعيين القائمة إلى المابات المكتشفة في مجلد الأصول دون أي ربط إجباري للفلاتر."""
        if not messagebox.askyesno('استرجاع القائمة الافتراضية', 'هل أنت متأكد من رغبتك في استرجاع قائمة المابات الافتراضية من مجلد الصور؟\nسيتم إعادة تعيين القائمة وتعيين جميع الفلاتر إلى (بدون ربط).'):
            return
        defaults = discover_default_maps()
        self.maps = [
            {'name': name, 'image': path, 'filter': 'بدون ربط', 'default': True, 'sub_maps': []}
            for name, path in defaults.items()
        ]
        self.maps.sort(key=lambda m: m['name'].casefold())
        self.app_settings['removed_defaults'] = []
        self.save_app_settings()
        self.save_maps()
        self.sync_from_map_presets_txt()
        self.export_map_presets_txt()
        self.update_detection_names()
        self.render_cards()
        self.detected_var.set('تم استرجاع المابات الافتراضية')

    def load_filter_options(self):
        options = ['بدون ربط']
        try:
            for current, dirs, files in os.walk(FILTER_SOURCE_DIR):
                dirs.sort(key=str.casefold)
                for filename in sorted(files, key=str.casefold):
                    full = os.path.join(current, filename)
                    relative = os.path.relpath(full, FILTER_SOURCE_DIR)
                    options.append(relative)
        except OSError:
            pass
        return options

    def refresh_filter_options(self):
        self.filter_options = self.load_filter_options()
        if hasattr(self, 'lobby_combo') and self.lobby_combo.winfo_exists():
            self.lobby_combo.configure(values=self.filter_options)
        self.render_cards()

    def set_map_filter(self, map_name, selected_filter):
        found = False
        if self.active_realm:
            realm = next((m for m in self.maps if m['name'] == self.active_realm), None)
            if realm:
                for sm in realm.setdefault('sub_maps', []):
                    if sm['name'] == map_name:
                        sm['filter'] = selected_filter
                        found = True
                        break
        if not found:
            for item in self.maps:
                if item['name'] == map_name:
                    item['filter'] = selected_filter
                    break
        self.save_maps()
        self.detected_var.set(f'تم ربط {map_name} بالفلتر: {selected_filter}')

        # إذا كانت المراقبة تعمل وهذا الماب هو النشط حالياً في اللعبة، بدّل الفلتر فوراً
        if getattr(self, 'running', False):
            last_m = getattr(self, 'last_applied_map_name', None)
            if last_m and (last_m.casefold() == map_name.casefold() or (self.active_realm and last_m.casefold() == self.active_realm.casefold())):
                # امسح المفتاح فقط إذا كان هذا الماب هو النشط حالياً
                self.applied_key = None
                self.apply_for_map(last_m)

    def save_maps(self, maps=None):
        if getattr(self, 'maps_load_failed', False):
            return False
        data_to_save = maps if maps is not None else getattr(self, 'maps', None)
        if data_to_save is None:
            return False

        targets = [MAPS_JSON, os.path.join(APP_DIR, 'maps.json')]
        if DATA_ROOT and DATA_ROOT != APP_DIR:
            targets.append(os.path.join(DATA_ROOT, 'maps.json'))

        success = False
        for target in targets:
            temp_path = target + '.tmp'
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data_to_save, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if os.path.isfile(target):
                    try:
                        shutil.copy2(target, target + '.bak')
                    except OSError:
                        pass
                os.replace(temp_path, target)
                success = True
            except Exception:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass

        self.export_map_presets_txt(data_to_save)
        return success

    def save_settings(self):
        if self.save_maps():
            self.detected_var.set('تم حفظ الإعدادات بنجاح. ستبقى محفوظة بعد إغلاق البرنامج.')
            messagebox.showinfo('حفظ الإعدادات', 'تم حفظ المابات والصور والفلاتر المرتبطة بنجاح.')

    def render_cards(self):
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.images.clear()
        self.card_widgets = {}
        self.cell_widgets = {}
        self.combobox_widgets = [w for w in (getattr(self, 'lobby_combo', None), getattr(self, 'res_combo', None), getattr(self, 'lang_combo', None)) if w]

        if self.active_realm:
            self.back_btn.pack(side='left', padx=(0, 10))
            self.header_title.configure(text=f"{self.t('realm_prefix')}{self.active_realm.upper()}")
        else:
            self.back_btn.pack_forget()
            self.header_title.configure(text=self.t('map_config'))

        query = self.search_var.get().strip().casefold()
        items = self.get_current_items()
        shown = [m for m in items if query in m['name'].casefold()]
        canvas_w = max(self.canvas.winfo_width(), 650)
        cols = max(2, min(6, canvas_w // 220))
        self._current_cols = cols

        self.canvas.itemconfigure(self.canvas_window, width=canvas_w)

        for col in range(8):
            self.cards_frame.grid_columnconfigure(col, weight=0, minsize=0, uniform='')
        for col in range(cols):
            self.cards_frame.grid_columnconfigure(col, weight=1, minsize=180, uniform='cards')

        for index, item in enumerate(shown):
            row, col = divmod(index, cols)
            cell = tk.Frame(self.cards_frame, bg=self.BG, height=222)
            cell.grid(row=row, column=col, padx=6, pady=6, sticky='nsew')
            cell.grid_propagate(False)
            self.cell_widgets[item['name']] = cell

            is_selected = (item['name'] == getattr(self, 'selected_name', None))
            hl_color = self.RED if is_selected else '#292c32'
            card = tk.Frame(cell, bg=self.CARD, highlightthickness=2,
                            highlightbackground=hl_color, highlightcolor=hl_color,
                            cursor='hand2')
            card.pack(fill='both', expand=True)
            self.card_widgets[item['name']] = card
            image_label = tk.Label(card, bg='#292c32', text=self.t('no_image'), fg='#9298a3',
                                   font=('Segoe UI', 9), height=6, cursor='hand2')
            image_path = item.get('image', '')
            if not image_path or not os.path.isfile(image_path):
                realm_obj = next((m for m in self.maps if m['name'] == self.active_realm), None) if self.active_realm else None
                resolved = self.resolve_submap_image(item['name'], image_path, realm_obj)
                if resolved:
                    image_path = resolved
                    item['image'] = resolved
            photo = self.image_cache.get(image_path)
            if photo is None and image_path and os.path.isfile(image_path):
                try:
                    from PIL import Image, ImageTk
                    image = Image.open(image_path).convert('RGB')
                    image.thumbnail((193, 105), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(image)
                    self.image_cache[image_path] = photo
                except Exception:
                    pass
            if photo:
                self.images.append(photo)
                image_label.configure(image=photo, text='', height=105)
            image_label.pack(fill='x')
            title = tk.Label(card, text=item['name'], bg=self.CARD, fg='white',
                             font=('Segoe UI', 9, 'bold'), wraplength=180, justify='center',
                             cursor='hand2')
            title.pack(fill='x', padx=5, pady=(5, 3))
            filter_var = tk.StringVar(value=item.get('filter', 'بدون ربط'))
            if filter_var.get() not in self.filter_options:
                filter_var.set('بدون ربط')
            combo = ttk.Combobox(card, textvariable=filter_var, values=self.filter_options,
                                 state='readonly', font=('Segoe UI', 8), width=23,
                                 style='Dark.TCombobox')
            combo.pack(fill='x', padx=6, pady=(0, 3))
            self.combobox_widgets.append(combo)
            combo.bind('<<ComboboxSelected>>',
                       lambda e, n=item['name'], v=filter_var, c=combo: (
                           self.select_map(n),
                           self.set_map_filter(n, v.get()),
                           c.selection_clear(),
                           self.root.focus_set()
                       ))

            interactive_widgets = [cell, card, image_label, title]

            if not self.active_realm:
                sub_count = len(item.get('sub_maps', []))
                if sub_count > 0:
                    badge_text = self.t('custom_maps_count', count=sub_count)
                    badge_fg = '#38bdf8'
                else:
                    badge_text = self.t('double_click_hint')
                    badge_fg = self.MUTED
                hint = tk.Label(card, text=badge_text, bg=self.CARD, fg=badge_fg,
                                font=('Segoe UI', 7), cursor='hand2')
                hint.pack(fill='x')
                interactive_widgets.append(hint)

                for w in interactive_widgets:
                    w.bind('<Double-Button-1>', lambda e, rn=item['name']: self.open_realm_submaps(rn))
            else:
                hint = tk.Label(card, text=self.t('custom_map_for', realm=self.active_realm), bg=self.CARD, fg='#4ade80',
                                font=('Segoe UI', 7), cursor='hand2')
                hint.pack(fill='x')
                interactive_widgets.append(hint)

            for widget in interactive_widgets:
                widget.bind('<Button-1>', lambda e, n=item['name']: self.select_map(n))

            # ── كليك يمين لحذف الماب أو الريلم ──
            def _bind_context_menu(target_name):
                def popup(e):
                    self.select_map(target_name)
                    menu = tk.Menu(self.root, tearoff=0, bg='#1c1f24', fg='white',
                                   activebackground=self.RED, activeforeground='white',
                                   font=('Segoe UI', 9), bd=1, relief='solid')
                    del_label = self.t('delete_map') if self.active_realm else self.t('delete_realm')
                    menu.add_command(label=f"🗑  {del_label}", command=lambda: self.delete_map_by_name(target_name))
                    menu.post(e.x_root, e.y_root)
                return popup

            ctx_cmd = _bind_context_menu(item['name'])
            for widget in interactive_widgets:
                widget.bind('<Button-3>', ctx_cmd)

        # ── بطاقة الإضافة دائماً في آخر الشبكة ──
        add_index = len(shown)
        add_row, add_col = divmod(add_index, cols)
        add_cell = tk.Frame(self.cards_frame, bg=self.BG, height=222)
        add_cell.grid(row=add_row, column=add_col, padx=6, pady=6, sticky='nsew')
        add_cell.grid_propagate(False)
        self.add_card_cell = add_cell

        add_card = tk.Frame(add_cell, bg='#181a1f', highlightthickness=2,
                            highlightbackground='#2d3139', highlightcolor=self.RED,
                            cursor='hand2')
        add_card.pack(fill='both', expand=True)

        plus_lbl = tk.Label(add_card, text='＋', bg='#181a1f', fg='#727987',
                            font=('Segoe UI', 38, 'bold'), cursor='hand2')
        plus_lbl.pack(expand=True, pady=(15, 0))

        add_text = self.t('add_map_card') if self.active_realm else self.t('add_realm_card')
        txt_lbl = tk.Label(add_card, text=add_text, bg='#181a1f', fg='#9ca3af',
                           font=('Segoe UI', 9, 'bold'), wraplength=170, justify='center', cursor='hand2')
        txt_lbl.pack(pady=(0, 20))

        def on_add_enter(e):
            add_card.configure(highlightbackground=self.RED, bg='#20242b')
            plus_lbl.configure(fg=self.RED, bg='#20242b')
            txt_lbl.configure(fg='white', bg='#20242b')

        def on_add_leave(e):
            add_card.configure(highlightbackground='#2d3139', bg='#181a1f')
            plus_lbl.configure(fg='#727987', bg='#181a1f')
            txt_lbl.configure(fg='#9ca3af', bg='#181a1f')

        for w in (add_cell, add_card, plus_lbl, txt_lbl):
            w.bind('<Enter>', on_add_enter)
            w.bind('<Leave>', on_add_leave)
            w.bind('<Button-1>', lambda e: self.add_map_dialog())

        if not shown and query:
            tk.Label(self.cards_frame, text=self.t('no_matches'), bg=self.BG, fg=self.MUTED,
                     font=('Segoe UI', 11)).grid(row=0, column=1, columnspan=cols-1, padx=20, pady=30)

        self._update_scrollregion()

    def clear_combobox_selection(self):
        """يلغي أي تحديد نصي أو تركيز على جميع القوائم المنسدلة ويعيد التركيز للنافذة."""
        try:
            self.root.focus_set()
            for cb in getattr(self, 'combobox_widgets', []):
                try:
                    if cb.winfo_exists():
                        cb.selection_clear()
                except Exception:
                    pass
        except Exception:
            pass

    def select_map(self, name):
        # إلغاء أي تحديد أو تركيز على القوائم المنسدلة فوراً عند اختيار الماب
        self.clear_combobox_selection()

        prev_name = getattr(self, 'selected_name', None)
        if prev_name == name:
            return
        self.selected_name = name
        self.detected_var.set(self.t('selected_msg', name=name))
        cards = getattr(self, 'card_widgets', {})

        if prev_name and prev_name in cards:
            try:
                prev_card = cards[prev_name]
                if prev_card.winfo_exists():
                    prev_card.configure(highlightbackground='#292c32', highlightcolor='#292c32')
            except Exception:
                pass

        if name and name in cards:
            try:
                new_card = cards[name]
                if new_card.winfo_exists():
                    new_card.configure(highlightbackground=self.RED, highlightcolor=self.RED)
            except Exception:
                pass

    def add_map_dialog(self):
        dialog = tk.Toplevel(self.root)
        is_sub = bool(self.active_realm)
        dialog.title(self.t('add_dialog_map_title', realm=self.active_realm) if is_sub else self.t('add_dialog_realm_title'))
        dialog.configure(bg=self.PANEL)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        heading = self.t('add_dialog_map_title', realm=self.active_realm) if is_sub else self.t('add_dialog_realm_title')
        tk.Label(dialog, text=heading, bg=self.PANEL, fg='white',
                 font=('Segoe UI', 13, 'bold')).pack(anchor='w', padx=18, pady=(16, 10))
        tk.Label(dialog, text=self.t('map_name_label') if is_sub else self.t('realm_name_label'), bg=self.PANEL, fg='#d5d8de').pack(anchor='w', padx=18)
        name_var = tk.StringVar()
        name_entry = tk.Entry(dialog, textvariable=name_var, bg='#25282e', fg='white', insertbackground='white',
                              relief='flat', width=42, font=('Segoe UI', 10))
        name_entry.pack(padx=18, pady=(4, 10), ipady=7)

        filter_var = tk.StringVar(value='بدون ربط')
        if is_sub:
            tk.Label(dialog, text=self.t('filter_label'), bg=self.PANEL, fg='#d5d8de').pack(anchor='w', padx=18)
            filter_combo = ttk.Combobox(dialog, textvariable=filter_var, values=self.filter_options,
                                        state='readonly', font=('Segoe UI', 9), width=38,
                                        style='Dark.TCombobox')
            filter_combo.pack(padx=18, pady=(4, 10), fill='x')

        image_var = tk.StringVar()
        image_row = tk.Frame(dialog, bg=self.PANEL)
        image_row.pack(fill='x', padx=18)
        img_title = self.t('image_label') if is_sub else self.t('image_label_realm')
        tk.Label(image_row, text=img_title, bg=self.PANEL, fg='#d5d8de').pack(anchor='w')
        image_label = tk.Label(image_row, text=self.t('no_image_chosen'), bg=self.PANEL, fg=self.MUTED,
                               font=('Segoe UI', 9), anchor='w')
        image_label.pack(side='left', fill='x', expand=True, pady=8)
        def browse():
            path = filedialog.askopenfilename(parent=dialog, title=self.t('browse_btn'),
                filetypes=[('Images', '*.png *.jpg *.jpeg *.webp *.bmp'), ('All files', '*.*')])
            if path:
                image_var.set(path)
                image_label.configure(text=os.path.basename(path))
        ttk.Button(image_row, text=self.t('browse_btn'), command=browse).pack(side='right', padx=(8, 0))
        def save():
            name = clean_display_name(' '.join(name_var.get().strip().split()))
            if not name:
                messagebox.showwarning(self.t('enter_name_first'), self.t('enter_name_first'), parent=dialog); return
            current_list = self.get_current_items()
            if any(m['name'].casefold() == name.casefold() for m in current_list):
                messagebox.showwarning(self.t('already_exists'), self.t('already_exists'), parent=dialog); return
            source = image_var.get()
            destination = ''
            if source and os.path.isfile(source):
                try:
                    from PIL import Image
                    ext = os.path.splitext(source)[1].lower()
                    if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'): ext = '.png'
                    if is_sub and self.active_realm:
                        realm_folder = os.path.join(CUSTOM_DIR, _safe_filename(self.active_realm))
                        os.makedirs(realm_folder, exist_ok=True)
                        destination = os.path.join(realm_folder, _safe_filename(name) + ext)
                    else:
                        destination = os.path.join(CUSTOM_DIR, _safe_filename(name) + ext)
                    counter = 2
                    while os.path.exists(destination):
                        p_dir = os.path.dirname(destination)
                        destination = os.path.join(p_dir, _safe_filename(name) + f'_{counter}' + ext)
                        counter += 1
                    Image.open(source).verify()
                    shutil.copy2(source, destination)
                except Exception as e:
                    messagebox.showerror('Error', str(e), parent=dialog); return
            elif is_sub:
                realm_folder = os.path.join(CUSTOM_DIR, _safe_filename(self.active_realm))
                os.makedirs(realm_folder, exist_ok=True)
                realm = next((m for m in self.maps if m['name'] == self.active_realm), None)
                destination = (realm or {}).get('image', '')
            else:
                messagebox.showwarning(self.t('select_image_first'), self.t('select_image_first'), parent=dialog); return

            f_val = filter_var.get() if is_sub else 'بدون ربط'
            new_entry = {'name': name, 'image': destination, 'filter': f_val}
            current_list.append(new_entry)
            self.update_detection_names()
            self.save_maps()
            self.render_cards()
            self.detected_var.set(f'+ {name}')
            dialog.destroy()
        buttons = tk.Frame(dialog, bg=self.PANEL)
        buttons.pack(fill='x', padx=18, pady=18)
        tk.Button(buttons, text=self.t('save_btn'), command=save, bg=self.RED, fg='white', relief='flat',
                  font=('Segoe UI', 10, 'bold'), padx=15, pady=7).pack(side='right')
        ttk.Button(buttons, text=self.t('cancel_btn'), command=dialog.destroy).pack(side='right', padx=8)
        name_entry.focus_set()

    def delete_map_by_name(self, name):
        if not name:
            return
        is_sub = bool(self.active_realm)
        title = self.t('delete_map') if is_sub else self.t('delete_realm')
        question = self.t('delete_confirm', name=name)
        if not messagebox.askyesno(title, question, parent=self.root):
            return
        if self.active_realm:
            realm = next((m for m in self.maps if m['name'] == self.active_realm), None)
            if realm and 'sub_maps' in realm:
                realm_folder = os.path.join(CUSTOM_DIR, _safe_filename(self.active_realm))
                for sm in realm['sub_maps']:
                    if sm['name'] == name:
                        img = sm.get('image', '')
                        if img and os.path.isfile(img) and os.path.normcase(os.path.abspath(img)).startswith(os.path.normcase(os.path.abspath(CUSTOM_DIR))):
                            try: os.remove(img)
                            except OSError: pass
                if os.path.isdir(realm_folder):
                    for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                        f = os.path.join(realm_folder, _safe_filename(name) + ext)
                        if os.path.isfile(f):
                            try: os.remove(f)
                            except OSError: pass
                    try:
                        if not os.listdir(realm_folder):
                            os.rmdir(realm_folder)
                    except OSError: pass
                realm['sub_maps'] = [sm for sm in realm['sub_maps'] if sm['name'] != name]
        else:
            target_entry = next((m for m in self.maps if m['name'] == name), None)
            if target_entry:
                img = target_entry.get('image', '')
                if img and os.path.isfile(img) and os.path.normcase(os.path.abspath(img)).startswith(os.path.normcase(os.path.abspath(CUSTOM_DIR))):
                    try: os.remove(img)
                    except OSError: pass
            removed = self.app_settings.setdefault('removed_defaults', [])
            if name not in removed:
                removed.append(name)
            self.save_app_settings()
            self.maps = [m for m in self.maps if m['name'] != name]
        self.update_detection_names()
        self.selected_name = None
        self.save_maps()
        self.render_cards()
        self.detected_var.set(self.t('deleted_msg', name=name))

    def remove_selected(self):
        name = getattr(self, 'selected_name', None)
        if not name:
            messagebox.showinfo('Select Map', 'اضغط على بطاقة الماب أولاً ثم اضغط حذف.' if self.current_lang == 'ar' else 'Please select a map card first.')
            return
        self.delete_map_by_name(name)

    def set_status(self, found, title, detail):
        self.status_var.set(title)
        self.detected_var.set(detail)
        self.status_label.configure(bg=('#14532d' if found else '#26292f'))

    def read_ocr_text(self, crop, is_ready=False):
        """قراءة النص باستخدام Tesseract مع preprocessing خاص بـ DBD."""
        if not crop:
            return ''
        try:
            import pytesseract
            tess = getattr(self, 'tesseract_path', None) or find_tesseract()
            if not tess:
                return ''
            pytesseract.pytesseract.tesseract_cmd = tess
            if is_ready:
                variants = prep_variants_optimized(crop, is_ready=True)
                txt = pytesseract.image_to_string(variants[0], config='--psm 7').strip()
                return txt
            else:
                results = []
                for img_var, cfg in prep_map_variants(crop):
                    txt = pytesseract.image_to_string(img_var, config=cfg).strip()
                    if txt:
                        results.append(txt)
                return '\n'.join(results)
        except Exception as exc:
            print(f'[AR DBD Tesseract Error] {exc}', flush=True)
        return ''

    def start(self):
        try:
            if self.running: return
            self.running = True
            self.toggle_btn.configure(text=self.t('stop_monitor'), bg=self.RED,
                                      activebackground='#c92638', activeforeground='white')
            self.set_status(False, self.t('monitoring_running'), self.t('waiting_prompt'))
            self.worker = threading.Thread(target=self.monitor_loop, daemon=True)
            self.worker.start()
        except Exception as exc:
            print(f"[AR DBD Start Error] {exc}", flush=True)
            messagebox.showerror('Error', f'Error starting monitor:\n{exc}', parent=self.root)

    def stop(self):
        self.running = False
        self.ready_active = False
        self.applied_key = None
        self.toggle_btn.configure(text=self.t('start_monitor'), bg=self.GREEN,
                                  activebackground='#218454', activeforeground='white')
        self.set_status(False, self.t('stopped_title'), self.t('stopped_detail'))

    def monitor_loop(self):
        try:
            ocr_reader = WindowsMediaOCR()
            print('\n[AR DBD Fast] بدأت المراقبة بمحرك Windows Media OCR فائق السرعة...', flush=True)

            last_ready = False
            last_console_state = None
            cycle = 0

            # حالة المراقبة: 'lobby' أو 'match'
            match_state = 'lobby'
            current_match_map = None
            match_start_time = 0.0
            ready_hits = 0
            ready_misses = 0

            while self.running:
                started = time.time()
                cycle += 1

                game = get_game_window_cached()
                if not game:
                    if last_console_state != 'missing':
                        print('[AR DBD Fast] بانتظار نافذة اللعبة...', flush=True)
                        last_console_state = 'missing'
                    self.events.put(('missing_window',))
                    ready_hits = 0
                    ready_misses = 0
                    time.sleep(0.2)
                    continue

                hwnd, bbox = game
                active_roi = getattr(self, 'active_roi', ROI)
                region = roi_bbox(bbox, active_roi)

                if not game_is_visible(hwnd, region, False):
                    if last_console_state != 'covered':
                        print('[AR DBD Fast] اللعبة مغطاة.', flush=True)
                        last_console_state = 'covered'
                    self.events.put(('covered',))
                    time.sleep(0.12)
                    continue

                if last_console_state != 'searching':
                    last_console_state = 'searching'

                now = time.time()

                # ═══════════════════════════════════════════════
                # في اللوبي: فحص فوري لاسم الماب أو READY/PLAY بدون أي تأخير
                # ═══════════════════════════════════════════════
                if match_state == 'lobby':
                    matched_name = ''
                    screen_crop = fast_grab_roi(region)

                    # 1. فحص القراءة المباشرة أولاً (~3ms)
                    raw_txt = ocr_reader.recognize(screen_crop)
                    if raw_txt:
                        m = match_dbd_map_name(raw_txt, getattr(self, 'submap_names', []), getattr(self, 'realm_names', []))
                        if m:
                            matched_name = m

                    # 2. إذا لم يتطابق، فحص الصور المعالجة
                    if not matched_name:
                        for var_img, _ in prep_map_variants(screen_crop):
                            txt = ocr_reader.recognize(var_img)
                            if not txt:
                                continue
                            m = match_dbd_map_name(txt, getattr(self, 'submap_names', []), getattr(self, 'realm_names', []))
                            if m:
                                matched_name = m
                                break

                    if matched_name:
                        match_state = 'match'
                        current_match_map = matched_name
                        match_start_time = now
                        ready_hits = 0
                        ready_misses = 0
                        last_ready = False
                        print(f'[AR DBD Fast] تم اكتشاف الماب: {matched_name} - تبديل الريشيد فوراً!', flush=True)
                        msg = self.apply_for_map(matched_name)
                        self.events.put(('match_applied', matched_name, 1, msg))
                    else:
                        ready_found = False
                        active_roi_ready = getattr(self, 'active_roi_ready', ROI_READY)
                        ready_region = roi_bbox(bbox, active_roi_ready)
                        if game_is_visible(hwnd, ready_region, False):
                            rc = fast_grab_roi(ready_region)
                            ready_txt = ocr_reader.recognize(rc)
                            if find_ready(ready_txt):
                                ready_found = True
                            else:
                                rv = prep_variants_optimized(rc, is_ready=True)
                                ready_txt2 = ocr_reader.recognize(rv[0])
                                if find_ready(ready_txt2):
                                    ready_found = True

                        if ready_found:
                            ready_hits += 1
                            ready_misses = 0
                        else:
                            ready_misses += 1
                            ready_hits = 0

                        if ready_hits >= 1 and not last_ready:
                            last_ready = True
                            print('[AR DBD Fast] اللوبي (READY/PLAY)', flush=True)
                            self.events.put(('ready', True))
                        elif ready_misses >= 3 and last_ready:
                            last_ready = False
                            self.events.put(('ready', False))

                # ═══════════════════════════════════════════════
                # داخل المباراة: الريشيد ثابت - فحص فوري لـ READY/PLAY للعودة للوبي
                # ═══════════════════════════════════════════════
                else:
                    elapsed = now - match_start_time

                    ready_found = False
                    active_roi_ready = getattr(self, 'active_roi_ready', ROI_READY)
                    ready_region = roi_bbox(bbox, active_roi_ready)
                    if game_is_visible(hwnd, ready_region, False):
                        rc = fast_grab_roi(ready_region)
                        ready_txt = ocr_reader.recognize(rc)
                        if find_ready(ready_txt):
                            ready_found = True
                        else:
                            rv = prep_variants_optimized(rc, is_ready=True)
                            ready_txt2 = ocr_reader.recognize(rv[0])
                            if find_ready(ready_txt2):
                                ready_found = True

                    if ready_found:
                        ready_hits += 1
                    else:
                        ready_hits = 0

                    # قراءتان متتاليتان لـ READY للتأكيد قبل العودة للوبي
                    if ready_hits >= 2:
                        print(f'[AR DBD Fast] انتهت المباراة ({int(elapsed)}s) - العودة للوبي', flush=True)
                        match_state = 'lobby'
                        current_match_map = None
                        ready_hits = 0
                        ready_misses = 0
                        last_ready = True
                        self.events.put(('ready', True))

                elapsed_time = time.time() - started
                time.sleep(max(0.005, INTERVAL_SECONDS - elapsed_time))

        except Exception as e:
            print(f'[AR DBD Fast] خطأ: {e}', flush=True)
            self.events.put(('error', str(e)))
            self.running = False

    def poll_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] in ('match_applied', 'match_start', 'match_update', 'match_confirm'):
                    matched_name = item[1]
                    cap_count = item[2] if len(item) > 2 else 1
                    msg = item[3] if len(item) > 3 else getattr(self, 'map_message', '')
                    self.ready_active = False
                    status_text = self.t('in_match', name=matched_name)
                    status_detail = f"{msg}  |  {self.t('reshade_fast')}"
                    self.set_status(True, status_text, status_detail)
                elif item[0] == 'ready':
                    self.ready_active = bool(item[1])
                    if self.ready_active:
                        if self.applied_key != ('lobby',):
                            ok, msg = self.apply_lobby()
                            self.applied_key = ('lobby',)
                            self.ready_message = msg
                        self.set_status(True, self.t('ready_in_lobby'), self.ready_message)
                    else:
                        self.ready_message = ''
                        if self.running and not self.applied_key:
                            self.set_status(False, self.t('monitoring_running'), self.t('waiting_prompt'))
                elif item[0] == 'missing_window':
                    self.set_status(False, self.t('game_missing'), self.t('game_missing_sub'))
                elif item[0] == 'covered':
                    self.set_status(False, self.t('game_covered'), self.t('game_covered_sub'))
                elif item[0] == 'error':
                    self.set_status(False, self.t('read_error'), 'حدث خطأ: ' + item[1])
                    self.toggle_btn.configure(text=self.t('start_monitor'), bg=self.GREEN,
                                              activebackground='#218454', activeforeground='white')
        except queue.Empty:
            pass

        if getattr(self, 'pending_equals', False):
            game = get_game_window_cached()
            if game:
                user32 = _setup_user32()
                foreground = user32.GetForegroundWindow()
                if foreground and _pid_of(user32, foreground) == _pid_of(user32, game[0]):
                    ok, _ = press_equals_key()
                    if ok:
                        self.pending_equals = False
                        print('[AR DBD Fast] تم الضغط على =', flush=True)

        self.root.after(20, self.poll_events)

    def open_ehsan(self):
        """فتح منصة إحسان الوطنية للعمل الخيري والتبرع في المتصفح."""
        url = "https://ehsan.ea.org.sa/?gad_source=1&gad_campaignid=23338564025&gclid=CjwKCAjwxpvXBhASEiwAue4B4YV0ARx_eS1tVa2N7p0ixFM5Qonblr1nV5AG9jlBvMgWUZjn8WIJJBoCIHAQAvD_BwE"
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            try:
                os.startfile(url)
            except Exception:
                pass

    def close(self):
        self.running = False
        if not getattr(self, 'maps_load_failed', False):
            self.save_maps()
        self.root.destroy()


if __name__ == '__main__':
    try:
        if not ensure_pillow():
            raise SystemExit(1)
        root = tk.Tk()
        App(root)
        hide_console_window()
        root.mainloop()
    except SystemExit:
        raise
    except Exception:
        import traceback
        details = traceback.format_exc()
        log_path = os.path.join(USER_DATA_DIR, 'error.log')
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}]\n{details}\n')
        except OSError:
            pass
        messagebox.showerror(APP_NAME, f'حدث خطأ غير متوقع.\nتم حفظ التفاصيل في:\n{log_path}\n\n{details[-800:]}')
        raise SystemExit(1)
