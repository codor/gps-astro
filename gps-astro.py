import socket
import time
import threading
from collections import deque
import tkinter as tk
from tkinter import font
import os
import sys
from datetime import datetime, timezone, timedelta
import math

# Skyfield 相关导入
from skyfield.api import Loader, wgs84, utc
from skyfield.almanac import find_discrete, risings_and_settings, meridian_transits
from skyfield import almanac

# ===== 新增导入 lunardate =====
from lunardate import LunarDate  # 农历库

# ===== 农历中文函数 =====
CN_NUM = {0:'〇',1:'一',2:'二',3:'三',4:'四',5:'五',
          6:'六',7:'七',8:'八',9:'九',10:'十'}

TIANGAN = ['甲','乙','丙','丁','戊','己','庚','辛','壬','癸']
DIZHI = ['子','丑','寅','卯','辰','巳','午','未','申','酉','戌','亥']
SHENGXIAO = ['鼠','牛','虎','兔','龙','蛇','马','羊','猴','鸡','狗','猪']

TRADITIONAL_MONTH = ['正','二','三','四','五','六','七','八','九','十','十一','腊']

# 农历节日字典
FESTIVALS = {
    (1,1): "春节",
    (1,15): "元宵节",
    (5,5): "端午节",
    (7,7): "七夕节",
    (8,15): "中秋节",
    (9,9): "重阳节",
    (12,8): "腊八节",
    (12,23): "小年"
}

SERVER_IP = '192.168.37.141'
SERVER_PORT = 20175
SMOOTH_WINDOW = 20

speed_history = deque()
course_history = deque()

# 全局 Skyfield 对象
ephemeris = None
ts = None
earth = None
moon = None
sun = None

# 二十四节气名称
SOLAR_TERMS = [
    "小寒","大寒","立春","雨水","惊蛰","春分","清明","谷雨",
    "立夏","小满","芒种","夏至","小暑","大暑",
    "立秋","处暑","白露","秋分","寒露","霜降",
    "立冬","小雪","大雪","冬至"
]

# ===== 节气缓存 =====
current_solar_terms = []  # 当年的节气列表
last_solar_term_calc_year = None  # 上次计算节气的年份

def init_skyfield():
    """初始化 Skyfield 和 DE421 星历"""
    global ephemeris, ts, earth, moon, sun
    try:
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

        de421_path = os.path.join(base_path, 'de421.bsp')

        if not os.path.exists(de421_path):
            print(f"错误: 找不到 de421.bsp 文件")
            print(f"请确保 de421.bsp 与程序在同一目录: {base_path}")
            return False

        loader = Loader(base_path)
        ephemeris = loader('de421.bsp')
        ts = loader.timescale()
        earth = ephemeris['earth']
        moon = ephemeris['moon']
        sun = ephemeris['sun']

        print(f"DE421 星历加载成功: {de421_path}")
        return True
    except Exception as e:
        print(f"加载 DE421 星历失败: {e}")
        import traceback
        traceback.print_exc()
        return False

# ===== 农历函数 =====
def num_to_chinese(n):
    if 1 <= n <= 10:
        return '初' + CN_NUM[n]
    elif n < 20:
        return '十' + (CN_NUM[n-10] if n-10 != 0 else '')
    elif n < 30:
        return '二十' + (CN_NUM[n-20] if n-20 != 0 else '')
    elif n == 30:
        return '三十'
    return str(n)

def get_ganzhi_year(year):
    tg = TIANGAN[(year - 4) % 10]
    dz = DIZHI[(year - 4) % 12]
    sx = SHENGXIAO[(year - 4) % 12]
    return f"{tg}{dz}年({sx}年)"

def today_lunar_info():
    today = datetime.today().date()
    try:
        l = LunarDate.fromSolarDate(today.year, today.month, today.day)
        
        lunar_year = get_ganzhi_year(l.year)
        month_name = TRADITIONAL_MONTH[l.month - 1]
        if getattr(l, 'isLeapMonth', getattr(l, 'leap', False)):
            month_name = '闰' + month_name
        day_name = num_to_chinese(l.day)
        
        # 大年初一
        if l.month == 1 and l.day == 1:
            day_name = '大年初一'
        # 除夕
        if l.month == 12 and l.day in (29,30):
            day_name += ' 除夕'
        # 节日
        festival = FESTIVALS.get((l.month, l.day))
        if festival:
            day_name += f" {festival}"
        
        return f"农历:{lunar_year}{month_name}月{day_name}"
    except Exception as e:
        print(f"农历计算错误: {e}")
        return "农历:不可用"

# ==========================
# 二十四节气功能
# ==========================
def find_solar_term_time(year, month, day, target_lon):
    """
    使用二分法精确查找某个节气发生的时刻
    target_lon: 目标太阳黄经（度）
    返回: datetime对象（UTC时间）
    """
    if ts is None or ephemeris is None:
        return None
    
    try:
        # 搜索范围：从当天00:00开始，跨2天
        try:
            t_start = ts.utc(year, month, day, 0, 0, 0)
        except:
            return None
        
        # 结束时间：2天后
        end_dt = datetime(year, month, day) + timedelta(days=2)
        try:
            t_end = ts.utc(end_dt.year, end_dt.month, end_dt.day, 0, 0, 0)
        except:
            return None
        
        # 二分法查找
        max_iterations = 50  # 最多迭代50次，精度可达秒级
        
        for _ in range(max_iterations):
            t_mid = ts.tt_jd((t_start.tt + t_end.tt) / 2)
            
            e = earth.at(t_mid)
            s = e.observe(sun)
            lat, lon, distance = s.apparent().ecliptic_latlon()
            sun_lon = lon.degrees % 360
            
            # 处理跨越0度的情况
            if target_lon == 0:
                if sun_lon > 180:
                    sun_lon = sun_lon - 360
                target_lon_check = 0
            else:
                target_lon_check = target_lon
            
            # 计算差值
            diff = sun_lon - target_lon_check
            
            # 处理周期性边界
            if diff > 180:
                diff -= 360
            elif diff < -180:
                diff += 360
            
            # 如果足够接近（0.0001度 约等于 0.36秒），返回结果
            if abs(diff) < 0.0001:
                return t_mid.utc_datetime()
            
            # 调整搜索范围
            if diff < 0:
                t_start = t_mid
            else:
                t_end = t_mid
        
        # 如果没找到精确值，返回最接近的时刻
        return t_mid.utc_datetime()
        
    except Exception as e:
        print(f"精确查找节气时间错误: {e}")
        return None


def calculate_solar_terms(year):
    """
    返回全年节气列表，格式为 [(节气名, datetime对象)]
    使用精确的二分法计算节气时刻
    """
    if ts is None or ephemeris is None:
        return []
    
    try:
        # 定义24节气对应的太阳黄经
        SOLAR_TERM_LONGITUDES = {
            315: "立春", 330: "雨水", 345: "惊蛰",
            0: "春分", 15: "清明", 30: "谷雨",
            45: "立夏", 60: "小满", 75: "芒种",
            90: "夏至", 105: "小暑", 120: "大暑",
            135: "立秋", 150: "处暑", 165: "白露",
            180: "秋分", 195: "寒露", 210: "霜降",
            225: "立冬", 240: "小雪", 255: "大雪",
            270: "冬至", 285: "小寒", 300: "大寒"
        }
        
        solar_terms_dates = []
        
        # 从前一年12月开始扫描到次年2月
        start_date = datetime(year - 1, 12, 1)
        end_date = datetime(year + 1, 2, 1)
        
        current = start_date
        prev_lon = None
        
        while current < end_date:
            try:
                t = ts.utc(current.year, current.month, current.day, 12, 0, 0)
                e = earth.at(t)
                s = e.observe(sun)
                lat, lon, distance = s.apparent().ecliptic_latlon()
                
                sun_lon = lon.degrees % 360
                
                # 检查是否跨过了某个节气点
                if prev_lon is not None:
                    for target_lon, term_name in SOLAR_TERM_LONGITUDES.items():
                        crossed = False
                        
                        # 处理跨越0度的情况
                        if prev_lon > 350 and sun_lon < 10:
                            if target_lon == 0 or target_lon >= 345:
                                crossed = True
                        # 正常情况
                        elif prev_lon < sun_lon:
                            if prev_lon <= target_lon <= sun_lon:
                                crossed = True
                        # 处理其他跨越情况
                        else:
                            if target_lon >= prev_lon or target_lon <= sun_lon:
                                crossed = True
                        
                        if crossed:
                            # 使用二分法精确查找节气时刻
                            # 在前一天到当天之间查找
                            search_date = current - timedelta(days=1)
                            precise_time = find_solar_term_time(
                                search_date.year,
                                search_date.month,
                                search_date.day,
                                target_lon
                            )
                            
                            if precise_time and precise_time.year == year:
                                # 检查是否已经添加过这个节气
                                if not any(name == term_name for name, _ in solar_terms_dates):
                                    solar_terms_dates.append((term_name, precise_time))
                
                prev_lon = sun_lon
                current += timedelta(days=1)
                
            except ValueError:
                current += timedelta(days=1)
                continue
        
        # 按日期排序
        solar_terms_dates.sort(key=lambda x: x[1])
        
        return solar_terms_dates
        
    except Exception as e:
        print(f"计算节气错误: {e}")
        import traceback
        traceback.print_exc()
        return []

def solar_term_reminder(today, solar_terms):
    """
    根据今天日期返回节气提醒信息
    """
    if not solar_terms:
        return ""
    
    for term_name, term_datetime in solar_terms:
        term_date = term_datetime.date() if hasattr(term_datetime, 'date') else term_datetime
        delta_days = (term_date - today).days
        
        # 转换为本地时间显示
        if hasattr(term_datetime, 'tzinfo') and term_datetime.tzinfo is not None:
            # UTC时间转本地时间
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
            local_dt = term_datetime + timedelta(hours=offset_hours)
        else:
            local_dt = term_datetime
        
        if delta_days == 0:
            time_str = local_dt.strftime("%H:%M:%S") if hasattr(local_dt, 'strftime') else ""
            return f"今日节气:{term_name} {time_str}"
        elif delta_days == 1:
            time_str = local_dt.strftime("%H:%M:%S") if hasattr(local_dt, 'strftime') else ""
            return f"明日节气:{term_name} {time_str}"
        elif 1 < delta_days <= 30:
            time_str = local_dt.strftime("%m-%d %H:%M") if hasattr(local_dt, 'strftime') else ""
            return f"距下个节气{delta_days}天:{term_name} {time_str}"
    
    return ""

def get_solar_term_info():
    """获取当前节气信息，带缓存机制"""
    global current_solar_terms, last_solar_term_calc_year
    
    try:
        today = datetime.today().date()
        
        # 如果是新的一年或还没计算过，重新计算节气
        if last_solar_term_calc_year != today.year:
            current_solar_terms = calculate_solar_terms(today.year)
            last_solar_term_calc_year = today.year
            print(f"已计算 {today.year} 年节气，共 {len(current_solar_terms)} 个")
        
        if current_solar_terms:
            return solar_term_reminder(today, current_solar_terms)
        else:
            return ""
    except Exception as e:
        print(f"获取节气信息错误: {e}")
        return ""

# --------------------------- 常用工具 ---------------------------

def get_desktop_path():
    """跨平台获取桌面路径"""
    if sys.platform == 'win32':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders')
            desktop_path = winreg.QueryValueEx(key, 'Desktop')[0]
            winreg.CloseKey(key)
            return desktop_path
        except:
            return os.path.join(os.path.expanduser('~'), 'Desktop')
    elif sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Desktop')
    else:
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        if os.path.exists(desktop):
            return desktop
        desktop_cn = os.path.join(os.path.expanduser('~'), '桌面')
        if os.path.exists(desktop_cn):
            return desktop_cn
        return os.path.expanduser('~')

def ensure_log_directory():
    """确保GPS LOG目录存在"""
    desktop = get_desktop_path()
    log_dir = os.path.join(desktop, 'GPS LOG')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir

def get_utc_time():
    """获取UTC时间"""
    return datetime.now(timezone.utc)

def format_time_info():
    """格式化时间信息(本地时间和UTC)"""
    local_time = datetime.now()
    utc_time = get_utc_time()

    # 计算时差
    if local_time.tzinfo is None:
        offset_seconds = time.timezone if time.daylight == 0 else time.altzone
        offset_hours = -offset_seconds / 3600
    else:
        offset_hours = local_time.utcoffset().total_seconds() / 3600

    # 格式化时差为 +HH:MM 或 -HH:MM
    offset_sign = '+' if offset_hours >= 0 else '-'
    offset_hours_abs = abs(offset_hours)
    offset_h = int(offset_hours_abs)
    offset_m = int((offset_hours_abs - offset_h) * 60)
    utc_offset_str = f"{offset_sign}{offset_h:02d}:{offset_m:02d}"

    week_list = ['一','二','三','四','五','六','日']
    weekday = week_list[local_time.weekday()]
    week_num = local_time.strftime("%W")

    local_str = local_time.strftime('%Y-%m-%d %H:%M:%S')
    utc_str = utc_time.strftime('%Y-%m-%d %H:%M:%S')

    # ===== 获取农历和节气信息 =====
    try:
        lunar_info = today_lunar_info()
    except Exception as e:
        print(f"获取农历信息错误: {e}")
        lunar_info = ""
    
    try:
        solar_term_info = get_solar_term_info()
    except Exception as e:
        print(f"获取节气信息错误: {e}")
        solar_term_info = ""

    return {
        'local': local_str,
        'utc': utc_str,
        'utc_offset': utc_offset_str,
        'weekday': weekday,
        'week_num': week_num,
        'date': local_time.strftime('%Y-%m-%d'),
        'lunar': lunar_info,
        'solar_term': solar_term_info
    }

# 新增小工具
def _format_az_deg(deg):
    """格式化方位为三位整数度数，如 002°"""
    if deg is None:
        return "—"
    return f"{int(round(deg))%360:03d}°"

def _today_local(dt=None):
    """返回本地日期的 00:00:00 与 23:59:59"""
    if dt is None:
        dt = datetime.now()
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end

def _to_local(dt_utc_like, ref_local=None):
    """将一个'有tz的UTC时间'按当前本地时区转为'本地naive时间'"""
    if ref_local is None:
        ref_local = datetime.now()
    if ref_local.tzinfo is None or ref_local.utcoffset() is None:
        # 计算系统本地偏移
        offset_seconds = time.timezone if time.daylight == 0 else time.altzone
        offset_hours = -offset_seconds / 3600
    else:
        offset_hours = ref_local.utcoffset().total_seconds() / 3600
    return dt_utc_like.replace(tzinfo=None) + timedelta(hours=offset_hours)

# --------------------------- 月相 (使用 DE421) ---------------------------

def calculate_moon_phase_de421(dt_local=None):
    """
    使用 DE421 计算月相
    返回: (月相名称, emoji图标, 月龄天数, 月相进度百分比, 左右亮度提示, 亮度百分比, 趋势箭头)
    """
    if dt_local is None:
        dt_local = datetime.now()

    if ephemeris is None:
        return "新月", "🌑", 0.0, 0.0, "未加载", 0.0, "—"

    try:
        # 转换为 UTC 时间，并添加 timezone 信息
        if dt_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
            dt_utc = dt_local - timedelta(hours=offset_hours)
            dt_utc = dt_utc.replace(tzinfo=utc)
        else:
            dt_utc = dt_local.astimezone(utc)

        t = ts.from_datetime(dt_utc)

        # 计算太阳-地球-月球的相对位置
        e = earth.at(t)
        s = e.observe(sun).apparent()
        m = e.observe(moon).apparent()

        # 计算月相角（太阳-地球-月球夹角）
        sun_pos = s.position.au
        moon_pos = m.position.au

        # 计算相位角
        dot = sun_pos[0]*moon_pos[0] + sun_pos[1]*moon_pos[1] + sun_pos[2]*moon_pos[2]
        sun_norm = math.sqrt(sun_pos[0]**2 + sun_pos[1]**2 + sun_pos[2]**2)
        moon_norm = math.sqrt(moon_pos[0]**2 + moon_pos[1]**2 + moon_pos[2]**2)
        cos_phase = dot / (sun_norm * moon_norm + 1e-15)
        cos_phase = max(-1.0, min(1.0, cos_phase))
        phase_angle = math.degrees(math.acos(cos_phase))

        # 计算月龄（简化）
        synodic_month = 29.530588

        # 黄经差判断盈亏进度
        s_elat, s_elon, _ = s.ecliptic_latlon()
        m_elat, m_elon, _ = m.ecliptic_latlon()
        sun_lon = s_elon.degrees
        moon_lon = m_elon.degrees
        lon_diff = (moon_lon - sun_lon) % 360

        phase_ratio = lon_diff / 360.0
        moon_age = phase_ratio * synodic_month
        phase_percentage = phase_ratio * 100

        # 亮度百分比（基于相位角）
        illumination = 50 * (1 - math.cos(math.radians(phase_angle)))

        # 趋势箭头
        if phase_ratio < 0.5:
            trend = "↑"
        elif phase_ratio > 0.5:
            trend = "↓"
        else:
            trend = "—"

        # 月相名称与亮面侧
        if phase_ratio < 0.0625:
            phase_name, phase_emoji, brightness_side = "新月", "🌑", "不可见"
        elif phase_ratio < 0.1875:
            phase_name, phase_emoji, brightness_side = "娥眉月", "🌒", "右边亮"
        elif phase_ratio < 0.3125:
            phase_name, phase_emoji, brightness_side = "上弦月", "🌓", "右边亮"
        elif phase_ratio < 0.4375:
            phase_name, phase_emoji, brightness_side = "盈凸月", "🌔", "右边亮"
        elif phase_ratio < 0.5625:
            phase_name, phase_emoji, brightness_side = "满月", "🌕", "全亮"
        elif phase_ratio < 0.6875:
            phase_name, phase_emoji, brightness_side = "亏凸月", "🌖", "左边亮"
        elif phase_ratio < 0.8125:
            phase_name, phase_emoji, brightness_side = "下弦月", "🌗", "左边亮"
        elif phase_ratio < 0.9375:
            phase_name, phase_emoji, brightness_side = "残月", "🌘", "左边亮"
        else:
            phase_name, phase_emoji, brightness_side = "新月", "🌑", "不可见"

        return phase_name, phase_emoji, moon_age, phase_percentage, brightness_side, illumination, trend

    except Exception as e:
        print(f"计算月相错误: {e}")
        return "新月", "🌑", 0.0, 0.0, "计算错误", 0.0, "—"

# --------------------------- 月出/月落/中天 (使用 DE421) ---------------------------

def calculate_moon_transit_de421(lat, lon, dt_local=None):
    """
    使用 DE421 计算月球中天时间和高度（简单逐步搜索）
    """
    if dt_local is None:
        dt_local = datetime.now()

    if ephemeris is None:
        return "未加载", None

    try:
        location = earth + wgs84.latlon(lat, lon)

        # 转换为 UTC 并添加时区信息
        if dt_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
            # 创建新的 UTC datetime 对象
            dt_naive = dt_local - timedelta(hours=offset_hours)
            dt_utc = datetime(dt_naive.year, dt_naive.month, dt_naive.day,
                              dt_naive.hour, dt_naive.minute, dt_naive.second, tzinfo=utc)
        else:
            dt_utc = dt_local.astimezone(utc)
            offset_hours = dt_local.utcoffset().total_seconds() / 3600

        # 搜索当天每2分钟的月球高度
        best_time = None
        best_alt = -90.0

        for hour in range(24):
            for minute in range(0, 60, 2):
                check_time = datetime(dt_utc.year, dt_utc.month, dt_utc.day, hour, minute, 0, tzinfo=utc)
                t = ts.from_datetime(check_time)
                astrometric = (location.at(t)).observe(moon)
                alt, az, distance = astrometric.apparent().altaz()
                if alt.degrees > best_alt:
                    best_alt = alt.degrees
                    best_time = check_time

        if best_time:
            local_time = best_time.replace(tzinfo=None) + timedelta(hours=offset_hours)
            return local_time.strftime("%H:%M"), best_alt
        else:
            return "—", None

    except Exception as e:
        print(f"计算月球中天错误: {e}")
        import traceback
        traceback.print_exc()
        return "计算错误", None

def calculate_moon_position_de421(lat, lon, dt_local=None):
    """
    使用 DE421 计算月球当前位置（高度和方位）
    """
    if dt_local is None:
        dt_local = datetime.now()

    if ephemeris is None:
        return None, None

    try:
        location = earth + wgs84.latlon(lat, lon)

        # 转换为 UTC 并添加时区信息
        if dt_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
            dt_utc = dt_local - timedelta(hours=offset_hours)
            dt_utc = dt_utc.replace(tzinfo=utc)
        else:
            dt_utc = dt_local.astimezone(utc)

        t = ts.from_datetime(dt_utc)
        astrometric = (location.at(t)).observe(moon)
        alt, az, distance = astrometric.apparent().altaz()

        return alt.degrees, az.degrees

    except Exception as e:
        print(f"计算月球位置错误: {e}")
        return None, None

def calculate_moon_events_de421(lat, lon, dt_local=None):
    """
    使用 DE421 计算当日月出、月落、中天时间和高度
    """
    if dt_local is None:
        dt_local = datetime.now()
    if ephemeris is None:
        return "未加载", "未加载", "未加载", None, None, None, None, None

    try:
        topos = wgs84.latlon(lat, lon)

        # 确定当天本地起止
        t0_local, t1_local = _today_local(dt_local)
        # 计算本地与UTC的差值（naive）
        if dt_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
        else:
            offset_hours = dt_local.utcoffset().total_seconds() / 3600

        # 将本地范围转换成对应的 UTC Skyfield Time
        t0_utc = (t0_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t1_utc = (t1_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t0 = ts.from_datetime(t0_utc)
        t1 = ts.from_datetime(t1_utc)

        # 升落事件
        f = risings_and_settings(ephemeris, moon, topos)
        times, events = find_discrete(t0, t1, f)

        moonrise_str, moonset_str = None, None
        next_rise_dt, next_set_dt = None, None
        next_rise_az, next_set_az = None, None

        for t, ev in zip(times, events):
            observer = (earth + topos).at(t)
            apparent = observer.observe(moon).apparent()
            alt, az, _ = apparent.altaz()
            az_deg = az.degrees
            ev_local = _to_local(t.utc_datetime(), dt_local)

            if ev == 1:  # 升起
                if moonrise_str is None:
                    moonrise_str = f"{ev_local.strftime('%H:%M:%S')} 方位{_format_az_deg(az_deg)}"
                    next_rise_dt = ev_local
                    next_rise_az = az_deg
            else:  # 落下
                if moonset_str is None:
                    moonset_str = f"{ev_local.strftime('%H:%M:%S')} 方位{_format_az_deg(az_deg)}"
                    next_set_dt = ev_local
                    next_set_az = az_deg

        if moonrise_str is None:
            moonrise_str = "不出"
        if moonset_str is None:
            moonset_str = "不落"

        # 中天（近似：逐2分钟搜索）
        transit_time, transit_alt = calculate_moon_transit_de421(lat, lon, dt_local)
        return moonrise_str, moonset_str, transit_time, transit_alt, next_rise_dt, next_rise_az, next_set_dt, next_set_az

    except Exception as e:
        print(f"计算月出月落错误: {e}")
        import traceback; traceback.print_exc()
        return "计算错误", "计算错误", "计算错误", None, None, None, None, None

# --------------------------- 太阳相关（全部改为 Skyfield） ---------------------------

def sun_alt_az_skyfield(lat, lon, dt_local):
    """
    使用 Skyfield 计算太阳当前位置（高度、方位）
    """
    if ephemeris is None:
        return None, None
    try:
        topos = wgs84.latlon(lat, lon)
        if dt_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
            dt_utc = dt_local - timedelta(hours=offset_hours)
            dt_utc = dt_utc.replace(tzinfo=utc)
        else:
            dt_utc = dt_local.astimezone(utc)

        t = ts.from_datetime(dt_utc)
        app = (earth + topos).at(t).observe(sun).apparent()
        alt, az, _ = app.altaz()
        return alt.degrees, az.degrees
    except Exception as e:
        print(f"太阳高度方位计算错误: {e}")
        return None, None

def calculate_sun_events_skyfield(lat, lon, date_local=None):
    """
    使用 Skyfield 计算日出日落
    """
    if ephemeris is None:
        return "未加载", "未加载", None, None, None, None

    if date_local is None:
        date_local = datetime.now()

    try:
        topos = wgs84.latlon(lat, lon)

        t0_local, t1_local = _today_local(date_local)
        if date_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
        else:
            offset_hours = date_local.utcoffset().total_seconds() / 3600

        t0_utc = (t0_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t1_utc = (t1_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t0 = ts.from_datetime(t0_utc)
        t1 = ts.from_datetime(t1_utc)

        f = risings_and_settings(ephemeris, sun, topos)
        times, events = find_discrete(t0, t1, f)

        sunrise_str, sunset_str = None, None
        next_rise_dt, next_set_dt = None, None
        next_rise_az, next_set_az = None, None

        for t, ev in zip(times, events):
            obs = (earth + topos).at(t).observe(sun).apparent()
            alt, az, _ = obs.altaz()
            az_deg = az.degrees
            ev_local = _to_local(t.utc_datetime(), date_local)
            if ev == 1:  # 日出
                if sunrise_str is None:
                    sunrise_str = f"{ev_local.strftime('%H:%M:%S')} 方位{_format_az_deg(az_deg)}"
                    next_rise_dt = ev_local
                    next_rise_az = az_deg
            else:  # 日落
                if sunset_str is None:
                    sunset_str = f"{ev_local.strftime('%H:%M:%S')} 方位{_format_az_deg(az_deg)}"
                    next_set_dt = ev_local
                    next_set_az = az_deg

        if sunrise_str is None and sunset_str is None:
            noon = date_local.replace(hour=12, minute=0, second=0, microsecond=0)
            midn = date_local.replace(hour=0, minute=0, second=0, microsecond=0)
            alt_noon, _ = sun_alt_az_skyfield(lat, lon, noon)
            alt_midn, _ = sun_alt_az_skyfield(lat, lon, midn)
            if alt_noon is None or alt_midn is None:
                return "计算错误", "计算错误", None, None, None, None
            if alt_noon > 0 and alt_midn > 0:
                return "极昼", "极昼", None, None, None, None
            if alt_noon < 0 and alt_midn < 0:
                return "极夜", "极夜", None, None, None, None
            return "计算错误", "计算错误", None, None, None, None

        if sunrise_str is None:
            sunrise_str = "无日出"
        if sunset_str is None:
            sunset_str = "无日落"

        return sunrise_str, sunset_str, next_rise_dt, next_rise_az, next_set_dt, next_set_az

    except Exception as e:
        print(f"日出日落计算错误: {e}")
        import traceback; traceback.print_exc()
        return "计算错误", "计算错误", None, None, None, None

def solar_transit_local_precise_skyfield(lat, lon, date_local=None, step_minutes=2):
    """
    使用 Skyfield 计算太阳中天时间与高度
    """
    if ephemeris is None:
        return "—", None

    if date_local is None:
        date_local = datetime.now()

    try:
        topos = wgs84.latlon(lat, lon)

        t0_local, t1_local = _today_local(date_local)
        if date_local.tzinfo is None:
            offset_seconds = time.timezone if time.daylight == 0 else time.altzone
            offset_hours = -offset_seconds / 3600
        else:
            offset_hours = date_local.utcoffset().total_seconds() / 3600

        t0_utc = (t0_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t1_utc = (t1_local - timedelta(hours=offset_hours)).replace(tzinfo=utc)
        t0 = ts.from_datetime(t0_utc)
        t1 = ts.from_datetime(t1_utc)

        try:
            times, kinds = meridian_transits(ephemeris, sun, topos, t0, t1)
            best_local_dt = None
            best_alt = -90.0
            for t, kind in zip(times, kinds):
                if int(kind) == 1:
                    obs = (earth + topos).at(t).observe(sun).apparent()
                    alt, az, _ = obs.altaz()
                    alt_deg = alt.degrees
                    local_dt = _to_local(t.utc_datetime(), date_local)
                    if alt_deg > best_alt:
                        best_alt = alt_deg
                        best_local_dt = local_dt
            if best_local_dt is not None:
                return best_local_dt.strftime("%H:%M"), best_alt
        except Exception:
            pass

        best_alt = -90.0
        best_local_dt = None
        probe = t0_local
        while probe <= t1_local:
            alt, _ = sun_alt_az_skyfield(lat, lon, probe)
            if alt is not None and alt > best_alt:
                best_alt = alt
                best_local_dt = probe
            probe += timedelta(minutes=max(1, int(step_minutes)))

        if best_local_dt is None:
            return "—", None
        return best_local_dt.strftime("%H:%M"), best_alt

    except Exception as e:
        print(f"太阳中天计算错误: {e}")
        return "—", None

def lighting_stage_from_sun_alt(alt_deg):
    if alt_deg <= -18:
        return ("夜间", "远离城市的漆黑深夜｜ 天空：纯黑夜空，满天星最亮｜ 海面：海天线看不见，只能靠灯光和仪器")
    elif -18 < alt_deg <= -12:
        return ("天文拂晓/暮光", "进入观星的夜色｜ 天空：仅地平线附近极微亮｜  海面：除灯光外几不可见")
    elif -12 < alt_deg <= -6:
        return ("航海拂晓/暮光", "郊外无路灯的将亮未亮｜天空：深蓝快速变暗/变亮，星星增/减｜海面：只见轮廓，看不清细节")
    elif -6 < alt_deg <= 0:
        return ("民用拂晓/暮光", "城市'蓝调时刻'｜天空：蓝紫到浅蓝渐变｜海面：轮廓清楚，不用强光也能走动")
    else:
        return ("白天", "正常白天 ｜ 天空： 明亮蓝天 ｜  海面：颜色饱和，细节清晰")

def dmm_format(dd, is_lat=True):
    deg = int(abs(dd))
    minutes = (abs(dd) - deg) * 60
    if is_lat:
        direction = 'N' if dd >= 0 else 'S'
        deg_fmt = f"{deg:02d}"
    else:
        direction = 'E' if dd >= 0 else 'W'
        deg_fmt = f"{deg:03d}"
    return f"{deg_fmt}°{minutes:06.3f}'{direction}"

def add_sample(history, value):
    now = time.time()
    history.append((now, value))
    while history and (now - history[0][0] > SMOOTH_WINDOW):
        history.popleft()

def average(history):
    if not history:
        return 0.0
    return sum(v for _, v in history) / len(history)

def parse_gprmc(line):
    try:
        parts = line.split(',')
        if parts[0] != '$GPRMC' or parts[2] != 'A':
            return None
        lat_raw = float(parts[3])
        lat_dir = parts[4]
        lon_raw = float(parts[5])
        lon_dir = parts[6]
        lat_deg = int(lat_raw / 100)
        lat_min = lat_raw % 100
        latitude = lat_deg + lat_min / 60.0
        if lat_dir == 'S':
            latitude = -latitude
        lon_deg = int(lon_raw / 100)
        lon_min = lon_raw % 100
        longitude = lon_deg + lon_min / 60.0
        if lon_dir == 'W':
            longitude = -longitude
        speed = float(parts[7])
        course = float(parts[8])
        return {'latitude': latitude, 'longitude': longitude, 'speed': speed, 'course': course}
    except Exception:
        return None

# --------------------------- UI 组件 ---------------------------

class RollDigit(tk.Canvas):
    def __init__(self, master, width=20, height=30, fontset=("Consolas", 16), fg="white", bg="black"):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0)
        self.fontset = fontset
        self.fg = fg
        self.current = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        if self.current is not None:
            self.display(self.current)

    def display(self, val):
        self.current = val
        self.delete('all')
        self.create_text(self.winfo_width()//2, self.winfo_height()//2, text=str(val), fill=self.fg, font=self.fontset)

    def animate(self, new):
        old = self.current
        if old is None or old == new:
            self.display(new)
            self.current = new
            return
        for y in range(self.winfo_height()//2, self.winfo_height()+1, 2):
            self.delete('all')
            self.create_text(self.winfo_width()//2, self.winfo_height()-y, text=str(old), fill=self.fg, font=self.fontset)
            self.create_text(self.winfo_width()//2, self.winfo_height()-y+self.winfo_height()//2, text=str(new), fill=self.fg, font=self.fontset)
            self.update()
            time.sleep(0.02)
        self.display(new)
        self.current = new

# --------------------------- 主程序 ---------------------------

class GPSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GPSGate TCP GPS显示")
        self.configure(bg='black')
        self.resizable(False, False)
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)

        self.main_frame = tk.Frame(self, bg='black')
        self.main_frame.grid(row=0, column=0, sticky='nw')

        self.top_row = tk.Frame(self.main_frame, bg='black')
        self.top_row.grid(row=0, column=0, sticky='nw')

        self.server_var = tk.StringVar()
        self.smooth_var = tk.StringVar()
        self.date_var = tk.StringVar()

        self.server_label = tk.Label(self.top_row, textvariable=self.server_var, font=("Consolas", 10), bg='black', fg='white')
        self.smooth_label = tk.Label(self.top_row, textvariable=self.smooth_var, font=("Consolas", 10), bg='black', fg='white')
        self.date_label = tk.Label(self.top_row, textvariable=self.date_var, font=("Consolas", 11), bg='black', fg='white')

        digit_width = 15
        digit_height = 25
        digit_font = ("Consolas", 16)

        self.hour_tens = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)
        self.hour_ones = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)
        self.colon1 = tk.Label(self.top_row, text=":", font=("Consolas", 16), bg='black', fg='white')
        self.min_tens = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)
        self.min_ones = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)
        self.colon2 = tk.Label(self.top_row, text=":", font=("Consolas", 16), bg='black', fg='white')
        self.sec_tens = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)
        self.sec_ones = RollDigit(self.top_row, width=digit_width, height=digit_height, fontset=digit_font)

        self.server_label.grid(row=0, column=0, sticky='n')
        self.smooth_label.grid(row=0, column=1, sticky='n', padx=(8,0))
        self.date_label.grid(row=0, column=2, sticky='n', padx=(14,0))
        self.hour_tens.grid(row=0, column=3, sticky='n', padx=(8,0))
        self.hour_ones.grid(row=0, column=4, sticky='n')
        self.colon1.grid(row=0, column=5, sticky='n')
        self.min_tens.grid(row=0, column=6, sticky='n')
        self.min_ones.grid(row=0, column=7, sticky='n')
        self.colon2.grid(row=0, column=8, sticky='n')
        self.sec_tens.grid(row=0, column=9, sticky='n')
        self.sec_ones.grid(row=0, column=10, sticky='n')

        self.version_label = tk.Label(self.top_row, text="v1.3农历节气", font=("Consolas", 10), bg='black', fg='lime')
        self.version_label.grid(row=0, column=11, sticky='n', padx=(12,0))

        self.btns_frame = tk.Frame(self.main_frame, bg='black')
        self.btns_frame.grid(row=0, column=12, sticky='ne', padx=(12,0))
        self.topmost = True
        self.top_btn = tk.Button(self.btns_frame, text="取消置顶", command=self.toggle_topmost, font=("Consolas", 10), bg='gray20', fg='white', activebackground='gray40', activeforeground='white', relief='flat')
        self.top_btn.pack(side='left', anchor='ne')
        self.close_btn = tk.Button(self.btns_frame, text="✖", command=self.on_close, bg='gray20', fg='white', activebackground='red', activeforeground='white', relief='flat', font=("Arial", 13, 'bold'))
        self.close_btn.pack(side='left', anchor='ne', padx=(4, 0))

        self.bottom_row = tk.Frame(self.main_frame, bg='black')
        self.bottom_row.grid(row=1, column=0, sticky='nw', pady=(0,4), columnspan=12)
        self.latlon_var = tk.StringVar()
        self.speed_course_var = tk.StringVar()
        self.latlon_label = tk.Label(self.bottom_row, textvariable=self.latlon_var, font=("Consolas", 14), bg='black', fg='white')
        self.speed_course_label = tk.Label(self.bottom_row, textvariable=self.speed_course_var, font=("Consolas", 14), bg='black', fg='white')
        self.latlon_label.pack(side="left", padx=(0,12))
        self.speed_course_label.pack(side="left")

        self.astro_moon_row = tk.Frame(self.main_frame, bg='black')
        self.astro_moon_row.grid(row=2, column=0, sticky='nw', pady=(0,2), columnspan=12)
        self.astro_sun_row = tk.Frame(self.main_frame, bg='black')
        self.astro_sun_row.grid(row=3, column=0, sticky='nw', pady=(0,4), columnspan=12)

        self.moon_var = tk.StringVar()
        self.sun_var = tk.StringVar()
        self.moon_label = tk.Label(self.astro_moon_row, textvariable=self.moon_var, font=("Consolas", 10), bg='black', fg='yellow')
        self.sun_label = tk.Label(self.astro_sun_row, textvariable=self.sun_var, font=("Consolas", 10), bg='black', fg='orange')
        self.moon_label.pack(side="left", padx=(0,0))
        self.sun_label.pack(side="left", padx=(0,0))

        self.bind("<ButtonPress-1>", self.start_move)
        self.bind("<ButtonRelease-1>", self.stop_move)
        self.bind("<B1-Motion>", self.do_move)

        self.speed_history = deque()
        self.course_history = deque()
        self.latest_data = None

        self.cur_hour_tens = None
        self.cur_hour_ones = None
        self.cur_min_tens = None
        self.cur_min_ones = None
        self.cur_sec_tens = None
        self.cur_sec_ones = None

        self.log_dir = ensure_log_directory()
        self.last_log_date = None
        self.last_log_minute = None
        self.last_log_hour = None
        self.log_file_handle = None

        self.is_connected = False
        self.connection_lost_logged = False

        self.cached_sunrise = "计算中..."
        self.cached_sunset = "计算中..."
        self.cached_moonrise = "计算中..."
        self.cached_moonset = "计算中..."
        self.sun_alt = None
        self.sun_az = None
        self.moon_alt = None
        self.moon_az = None
        self.sun_transit = "—"
        self.sun_transit_alt = None
        self.moon_transit = "—"
        self.moon_transit_alt = None
        self.last_astro_calc_minute = None

        self.running = True

        self.next_moonrise_dt = None
        self.next_moonrise_az = None
        self.next_moonset_dt = None
        self.next_moonset_az = None
        self.next_sunrise_dt = None
        self.next_sunrise_az = None
        self.next_sunset_dt = None
        self.next_sunset_az = None
        self._fired_event_keys = set()

        init_success = init_skyfield()
        if not init_success:
            print("警告: DE421 星历加载失败，天文计算将不可用")

        threading.Thread(target=self.tcp_recv_thread, daemon=True).start()
        threading.Thread(target=self.log_thread, daemon=True).start()
        self.update_display()

    def start_move(self, event):
        self._offsetx = event.x
        self._offsety = event.y

    def stop_move(self, event):
        self._offsetx = None
        self._offsety = None

    def do_move(self, event):
        x = self.winfo_pointerx() - self._offsetx
        y = self.winfo_pointery() - self._offsety
        self.geometry(f'+{x}+{y}')

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.wm_attributes("-topmost", self.topmost)
        self.top_btn.config(text="取消置顶" if self.topmost else "置顶")

    def log_connection_event(self, event_type):
        try:
            now = datetime.now()
            current_date = now.strftime('%Y-%m-%d')

            if self.last_log_date != current_date or not self.log_file_handle:
                if self.log_file_handle:
                    self.log_file_handle.close()

                filename = f"G ATLANTIC-{current_date}.TXT"
                filepath = os.path.join(self.log_dir, filename)

                file_exists = os.path.exists(filepath)
                self.log_file_handle = open(filepath, 'a', encoding='utf-8')

                if not file_exists:
                    time_info = format_time_info()
                    self.write_file_header(self.log_file_handle, time_info)

                self.last_log_date = current_date

            utc_time_val = get_utc_time()
            utc_str = utc_time_val.strftime('%H:%M:%S')
            local_str = now.strftime('%H:%M:%S')

            if now.tzinfo is None:
                offset_seconds = time.timezone if time.daylight == 0 else time.altzone
                offset_hours = -offset_seconds / 3600
            else:
                offset_hours = now.utcoffset().total_seconds() / 3600
            offset_sign = '+' if offset_hours >= 0 else '-'
            offset_hours_abs = abs(offset_hours)
            offset_h = int(offset_hours_abs)
            offset_m = int((offset_hours_abs - offset_h) * 60)
            utc_offset_str = f"{offset_sign}{offset_h:02d}:{offset_m:02d}"

            if event_type == 'disconnect':
                log_line = f"{current_date} [{local_str}LT | {utc_str}UTC | UTC{utc_offset_str}]   ***X 与服务器断开连接 ***\n"
            else:
                log_line = f"{current_date} [{local_str}LT | {utc_str}UTC | UTC{utc_offset_str}]   ***V 与服务器建立连接 ***\n"

            self.log_file_handle.write(log_line)
            self.log_file_handle.flush()
        except Exception as e:
            print(f"记录连接事件错误: {e}")

    def tcp_recv_thread(self):
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((SERVER_IP, SERVER_PORT))

                if not self.is_connected:
                    self.is_connected = True
                    self.connection_lost_logged = False
                    self.log_connection_event('connect')
                    print(f"已连接到服务器 {SERVER_IP}:{SERVER_PORT}")

                sock.settimeout(None)
                buffer = ''

                while self.running:
                    data = sock.recv(4096)
                    if not data:
                        break

                    buffer += data.decode(errors='ignore')
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line.startswith('$GPRMC'):
                            parsed = parse_gprmc(line)
                            if parsed:
                                self.latest_data = parsed
                                add_sample(self.speed_history, parsed['speed'])
                                add_sample(self.course_history, parsed['course'])

            except Exception as e:
                print(f"连接错误: {e}")

            finally:
                if sock:
                    try:
                        sock.close()
                    except:
                        pass

                if self.is_connected:
                    self.is_connected = False
                    self.latest_data = None
                    if not self.connection_lost_logged:
                        self.log_connection_event('disconnect')
                        self.connection_lost_logged = True
                    print("与服务器断开连接")

                if self.running:
                    print("1秒后尝试重新连接...")
                    time.sleep(1)

    def write_file_header(self, f, time_info):
        f.write("=" * 70 + "\n")
        f.write("G ATLANTIC GPS 记录 (使用 DE421 星历)\n")
        f.write("=" * 70 + "\n")
        f.write(f"服务器: {SERVER_IP}:{SERVER_PORT}\n")
        f.write(f"平滑设置: {SMOOTH_WINDOW}秒\n")
        f.write(f"当地时间: {time_info['local']}\n")
        f.write(f"UTC时间: {time_info['utc']}\n")
        f.write(f"时差: UTC{time_info['utc_offset']}\n")
        f.write(f"星期{time_info['weekday']} 第{time_info['week_num']}周   {time_info['lunar']}\n")
        if time_info.get('solar_term'):
            f.write(f"{time_info['solar_term']}\n")
        f.write("=" * 70 + "\n\n")

    def log_thread(self):
        while self.running:
            try:
                time.sleep(1)
                if not self.latest_data or not self.is_connected:
                    continue

                now = datetime.now()
                current_date = now.strftime('%Y-%m-%d')
                current_minute = now.strftime('%Y-%m-%d %H:%M')
                current_hour = now.hour

                if self.last_log_date != current_date:
                    if self.log_file_handle:
                        self.log_file_handle.close()

                    filename = f"G ATLANTIC-{current_date}.TXT"
                    filepath = os.path.join(self.log_dir, filename)

                    file_exists = os.path.exists(filepath)
                    self.log_file_handle = open(filepath, 'a', encoding='utf-8')

                    if not file_exists:
                        time_info = format_time_info()
                        self.write_file_header(self.log_file_handle, time_info)

                    self.last_log_date = current_date
                    self.last_log_minute = None
                    self.last_log_hour = None

                if self.last_log_hour is not None and self.last_log_hour != current_hour:
                    self.log_file_handle.write("\n")
                    self.log_file_handle.flush()
                self.last_log_hour = current_hour

                if self.last_log_minute != current_minute:
                    speed_avg = average(self.speed_history)
                    course_avg = average(self.course_history)

                    lat_str = dmm_format(self.latest_data['latitude'], True)
                    lon_str = dmm_format(self.latest_data['longitude'], False)
                    course_int = f"{int(round(course_avg)):03d}"
                    speed_str = f"{speed_avg:.1f}"

                    utc_time_val = get_utc_time()
                    utc_str = utc_time_val.strftime('%H:%M:%S')
                    local_str = now.strftime('%H:%M:%S')

                    if now.tzinfo is None:
                        offset_seconds = time.timezone if time.daylight == 0 else time.altzone
                        offset_hours = -offset_seconds / 3600
                    else:
                        offset_hours = now.utcoffset().total_seconds() / 3600
                    offset_sign = '+' if offset_hours >= 0 else '-'
                    offset_hours_abs = abs(offset_hours)
                    offset_h = int(offset_hours_abs)
                    offset_m = int((offset_hours_abs - offset_h) * 60)
                    utc_offset_str = f"{offset_sign}{offset_h:02d}:{offset_m:02d}"

                    log_line = f"{current_date} [{local_str}LT | {utc_str}UTC | UTC{utc_offset_str}]  | 纬度: {lat_str}  , 经度: {lon_str} |  航向:{course_int}° | 航速:{speed_str}节\n"

                    self.log_file_handle.write(log_line)
                    self.log_file_handle.flush()
                    self.last_log_minute = current_minute

            except Exception as e:
                print(f"日志记录错误: {e}")

    def update_minutely_astro(self, lat, lon):
        try:
            now_local = datetime.now()
            min_key = now_local.strftime('%Y-%m-%d %H:%M')
            if self.last_astro_calc_minute == min_key:
                return
            self.last_astro_calc_minute = min_key

            s_alt, s_az = sun_alt_az_skyfield(lat, lon, now_local)
            self.sun_alt, self.sun_az = s_alt, s_az

            m_alt, m_az = calculate_moon_position_de421(lat, lon, now_local)
            self.moon_alt, self.moon_az = m_alt, m_az

            sunrise_str, sunset_str, sr_dt, sr_az, ss_dt, ss_az = calculate_sun_events_skyfield(lat, lon, now_local)
            self.cached_sunrise = sunrise_str
            self.cached_sunset = sunset_str
            self.next_sunrise_dt, self.next_sunrise_az = sr_dt, sr_az
            self.next_sunset_dt, self.next_sunset_az = ss_dt, ss_az

            (moonrise_str, moonset_str, self.moon_transit,
             self.moon_transit_alt, mr_dt, mr_az, ms_dt, ms_az) = calculate_moon_events_de421(lat, lon, now_local)
            self.cached_moonrise = moonrise_str
            self.cached_moonset = moonset_str
            self.next_moonrise_dt, self.next_moonrise_az = mr_dt, mr_az
            self.next_moonset_dt, self.next_moonset_az = ms_dt, ms_az

            self.sun_transit, self.sun_transit_alt = solar_transit_local_precise_skyfield(lat, lon, now_local, step_minutes=2)

            print(f"[Minutely Astro] {now_local.strftime('%H:%M')} @ ({lat:.4f}, {lon:.4f})")

        except Exception as e:
            print(f"计算天文数据错误: {e}")
            self.cached_sunrise = "计算错误"
            self.cached_sunset = "计算错误"
            self.cached_moonrise = "计算错误"
            self.cached_moonset = "计算错误"
            self.sun_transit = "—"
            self.sun_transit_alt = None
            self.moon_transit = "—"
            self.moon_transit_alt = None
            self.sun_alt = None
            self.sun_az = None
            self.moon_alt = None
            self.moon_az = None

    def _write_event_log(self, event_name, az_deg):
        """写入一条与每分钟轨迹相同格式的记录，末尾追加事件名与方位"""
        try:
            now = datetime.now()
            current_date = now.strftime('%Y-%m-%d')

            if self.last_log_date != current_date or not self.log_file_handle:
                if self.log_file_handle:
                    self.log_file_handle.close()
                filename = f"G ATLANTIC-{current_date}.TXT"
                filepath = os.path.join(self.log_dir, filename)
                file_exists = os.path.exists(filepath)
                self.log_file_handle = open(filepath, 'a', encoding='utf-8')
                if not file_exists:
                    time_info = format_time_info()
                    self.write_file_header(self.log_file_handle, time_info)
                self.last_log_date = current_date

            if not (self.latest_data and self.is_connected):
                lat_str = "—"
                lon_str = "—"
                course_int = "---"
                speed_str = "--.-"
            else:
                lat_str = dmm_format(self.latest_data['latitude'], True)
                lon_str = dmm_format(self.latest_data['longitude'], False)
                course_avg = average(self.course_history)
                speed_avg = average(self.speed_history)
                course_int = f"{int(round(course_avg)):03d}"
                speed_str = f"{speed_avg:.1f}"

            utc_time_val = get_utc_time()
            utc_str = utc_time_val.strftime('%H:%M:%S')
            local_str = now.strftime('%H:%M:%S')

            if now.tzinfo is None:
                offset_seconds = time.timezone if time.daylight == 0 else time.altzone
                offset_hours = -offset_seconds / 3600
            else:
                offset_hours = now.utcoffset().total_seconds() / 3600
            offset_sign = '+' if offset_hours >= 0 else '-'
            offset_hours_abs = abs(offset_hours)
            offset_h = int(offset_hours_abs)
            offset_m = int((offset_hours_abs - offset_h) * 60)
            utc_offset_str = f"{offset_sign}{offset_h:02d}:{offset_m:02d}"

            az_text = _format_az_deg(az_deg)
            log_line = (
                f"{current_date} [{local_str}LT | {utc_str}UTC | UTC{utc_offset_str}]  | "
                f"纬度: {lat_str}  , 经度: {lon_str} |  航向:{course_int}° | 航速:{speed_str}节 | "
                f"事件: {event_name} 方位{az_text}\n"
            )
            self.log_file_handle.write(log_line)
            self.log_file_handle.flush()
        except Exception as e:
            print(f"事件日志记录错误: {e}")

    def _maybe_fire_event(self, now_local):
        """在'出/没'瞬间立即刷新 GUI 并追加一条事件日志"""
        def fire_once(key, event_name, az_deg, refresh_func):
            if key in self._fired_event_keys:
                return
            self._fired_event_keys.add(key)
            try:
                refresh_func()
            finally:
                pass
            self._write_event_log(event_name, az_deg)

        def should_fire(ev_dt):
            return ev_dt is not None and abs((now_local - ev_dt).total_seconds()) <= 1.5

        if should_fire(self.next_moonrise_dt):
            key = f"moonrise-{self.next_moonrise_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            def refresh_moon():
                if self.latest_data:
                    self.update_minutely_astro(self.latest_data['latitude'], self.latest_data['longitude'])
                self.moon_label.update_idletasks()
            fire_once(key, "月出", self.next_moonrise_az, refresh_moon)

        if should_fire(self.next_moonset_dt):
            key = f"moonset-{self.next_moonset_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            def refresh_moon():
                if self.latest_data:
                    self.update_minutely_astro(self.latest_data['latitude'], self.latest_data['longitude'])
                self.moon_label.update_idletasks()
            fire_once(key, "月落", self.next_moonset_az, refresh_moon)

        if should_fire(self.next_sunrise_dt):
            key = f"sunrise-{self.next_sunrise_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            def refresh_sun():
                if self.latest_data:
                    self.update_minutely_astro(self.latest_data['latitude'], self.latest_data['longitude'])
                self.sun_label.update_idletasks()
            fire_once(key, "日出", self.next_sunrise_az, refresh_sun)

        if should_fire(self.next_sunset_dt):
            key = f"sunset-{self.next_sunset_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            def refresh_sun():
                if self.latest_data:
                    self.update_minutely_astro(self.latest_data['latitude'], self.latest_data['longitude'])
                self.sun_label.update_idletasks()
            fire_once(key, "日落", self.next_sunset_az, refresh_sun)

    def update_display(self):
        now_time = time.localtime()

        if self.latest_data and self.is_connected:
            speed_avg = average(self.speed_history)
            course_avg = average(self.course_history)
            lat_str = dmm_format(self.latest_data['latitude'], True)
            lon_str = dmm_format(self.latest_data['longitude'], False)
            course_int = f"{int(round(course_avg)):03d}"
            speed_str = f"{speed_avg:.1f}"
            self.server_var.set(f"服务器: {SERVER_IP}:{SERVER_PORT}")
            self.smooth_var.set(f"平滑: {SMOOTH_WINDOW}秒")
            week_list = ['一','二','三','四','五','六','日']
            weekday = week_list[now_time.tm_wday]
            weekno = time.strftime("%W", now_time)
            date_str = time.strftime('%Y-%m-%d', now_time)
            
            time_info = format_time_info()
            solar_term_text = f" {time_info['solar_term']}" if time_info['solar_term'] else ""
            self.date_var.set(f" {time_info['lunar']} | 星期{weekday} | {solar_term_text} | 第{weekno}周 | {date_str} | UTC{time_info['utc_offset']}")

            lat = self.latest_data['latitude']
            lon = self.latest_data['longitude']
            self.update_minutely_astro(lat, lon)

            phase_name, phase_emoji, moon_age, phase_percentage, brightness_side, illumination, trend = calculate_moon_phase_de421()

            if self.sun_alt is not None:
                stage, stage_desc = lighting_stage_from_sun_alt(self.sun_alt)
            else:
                stage, stage_desc = ("未知", "等待计算")

            if self.moon_alt is not None and self.moon_alt >= 0:
                moon_pos_text = f"高度:{self.moon_alt:.1f}° 方位:{self.moon_az:.1f}°"
            elif self.moon_alt is not None:
                moon_pos_text = "地平线下不可见"
            else:
                moon_pos_text = "等待GPS"

            moon_transit_alt_text = f"{self.moon_transit_alt:.1f}°" if (self.moon_transit_alt is not None) else "—"
            self.moon_var.set(
                f"{phase_emoji} {phase_name}({brightness_side}) "
                f"月龄:{moon_age:.1f}天(已过{phase_percentage:.0f}%) "
                f"亮面比:{illumination:.0f}%{trend} ｜ 月出:{self.cached_moonrise} 月落:{self.cached_moonset} ｜ "
                f"中天时间:{self.moon_transit} 高度:{moon_transit_alt_text} ｜ 当前{moon_pos_text}"
            )

            if self.sun_alt is not None and self.sun_alt >= 0:
                sun_pos_text = f"高度:{self.sun_alt:.1f}° 方位:{self.sun_az:.1f}°"
            elif self.sun_alt is not None:
                sun_pos_text = "地平线下不可见"
            else:
                sun_pos_text = "等待GPS"

            sun_transit_alt_text = f"{self.sun_transit_alt:.1f}°" if (self.sun_transit_alt is not None) else "—"
            self.sun_var.set(
                f"☀ {stage}｜{stage_desc} ｜ 日出:{self.cached_sunrise} 日落:{self.cached_sunset} ｜ "
                f"中天时间:{self.sun_transit} 高度:{sun_transit_alt_text} ｜ 当前{sun_pos_text}"
            )

            hour = now_time.tm_hour
            minute = now_time.tm_min
            second = now_time.tm_sec

            hour_tens = hour // 10
            hour_ones = hour % 10
            min_tens = minute // 10
            min_ones = minute % 10
            sec_tens = second // 10
            sec_ones = second % 10

            if self.cur_hour_tens is None:
                self.hour_tens.display(hour_tens); self.cur_hour_tens = hour_tens
            elif self.cur_hour_tens != hour_tens:
                self.hour_tens.animate(hour_tens); self.cur_hour_tens = hour_tens

            if self.cur_hour_ones is None:
                self.hour_ones.display(hour_ones); self.cur_hour_ones = hour_ones
            elif self.cur_hour_ones != hour_ones:
                self.hour_ones.animate(hour_ones); self.cur_hour_ones = hour_ones

            if self.cur_min_tens is None:
                self.min_tens.display(min_tens); self.cur_min_tens = min_tens
            elif self.cur_min_tens != min_tens:
                self.min_tens.animate(min_tens); self.cur_min_tens = min_tens

            if self.cur_min_ones is None:
                self.min_ones.display(min_ones); self.cur_min_ones = min_ones
            elif self.cur_min_ones != min_ones:
                self.min_ones.animate(min_ones); self.cur_min_ones = min_ones

            if self.cur_sec_tens is None:
                self.sec_tens.display(sec_tens); self.cur_sec_tens = sec_tens
            elif self.cur_sec_tens != sec_tens:
                self.sec_tens.animate(sec_tens); self.cur_sec_tens = sec_tens

            if self.cur_sec_ones is None:
                self.sec_ones.display(sec_ones); self.cur_sec_ones = sec_ones
            elif self.cur_sec_ones != sec_ones:
                self.sec_ones.animate(sec_ones); self.cur_sec_ones = sec_ones

            self.latlon_label.config(fg='white')
            self.speed_course_label.config(fg='white')

            self.latlon_var.set(f"纬度: {lat_str} 经度: {lon_str} |")
            self.speed_course_var.set(f"航速: {speed_str} 节    航向: {course_int}°")

        else:
            self.server_var.set(f"服务器: {SERVER_IP}:{SERVER_PORT}")
            self.smooth_var.set(f"平滑: {SMOOTH_WINDOW}秒")
            week_list = ['一','二','三','四','五','六','日']
            weekday = week_list[now_time.tm_wday]
            weekno = time.strftime("%W", now_time)
            date_str = time.strftime('%Y-%m-%d', now_time)
            
            time_info = format_time_info()
            solar_term_text = f" {time_info['solar_term']}" if time_info['solar_term'] else ""
            self.date_var.set(f"{date_str} {time_info['lunar']} 星期{weekday} 第{weekno}周{solar_term_text}")

            phase_name, phase_emoji, moon_age, phase_percentage, brightness_side, illumination, trend = calculate_moon_phase_de421()
            self.moon_var.set(
                f"{phase_emoji} {phase_name}({brightness_side}) 月龄:{moon_age:.1f}天({phase_percentage:.0f}%) "
                f"亮度:{illumination:.0f}%{trend} ｜ 月出/月落:等待GPS ｜ 中天:— 高度:— ｜ 等待GPS"
            )
            self.sun_var.set("☀️ 光照/太阳:等待GPS ｜ 日出/日落:等待GPS ｜ 中天:— 高度:— ｜ 等待GPS")

            hour = now_time.tm_hour
            minute = now_time.tm_min
            second = now_time.tm_sec

            hour_tens = hour // 10
            hour_ones = hour % 10
            min_tens = minute // 10
            min_ones = minute % 10
            sec_tens = second // 10
            sec_ones = second % 10

            if self.cur_hour_tens is None:
                self.hour_tens.display(hour_tens); self.cur_hour_tens = hour_tens
            elif self.cur_hour_tens != hour_tens:
                self.hour_tens.animate(hour_tens); self.cur_hour_tens = hour_tens

            if self.cur_hour_ones is None:
                self.hour_ones.display(hour_ones); self.cur_hour_ones = hour_ones
            elif self.cur_hour_ones != hour_ones:
                self.hour_ones.animate(hour_ones); self.cur_hour_ones = hour_ones

            if self.cur_min_tens is None:
                self.min_tens.display(min_tens); self.cur_min_tens = min_tens
            elif self.cur_min_tens != min_tens:
                self.min_tens.animate(min_tens); self.cur_min_tens = min_tens

            if self.cur_min_ones is None:
                self.min_ones.display(min_ones); self.cur_min_ones = min_ones
            elif self.cur_min_ones != min_ones:
                self.min_ones.animate(min_ones); self.cur_min_ones = min_ones

            if self.cur_sec_tens is None:
                self.sec_tens.display(sec_tens); self.cur_sec_tens = sec_tens
            elif self.cur_sec_tens != sec_tens:
                self.sec_tens.animate(sec_tens); self.cur_sec_tens = sec_tens

            if self.cur_sec_ones is None:
                self.sec_ones.display(sec_ones); self.cur_sec_ones = sec_ones
            elif self.cur_sec_ones != sec_ones:
                self.sec_ones.animate(sec_ones); self.cur_sec_ones = sec_ones

            self.latlon_label.config(fg='red')
            self.speed_course_label.config(fg='red')
            self.latlon_var.set("")
            self.speed_course_var.set("~~未与服务器建立连接~~")

        self.update_idletasks()
        w = self.main_frame.winfo_reqwidth()
        h = self.main_frame.winfo_reqheight()
        self.geometry(f"{w}x{h}")

        try:
            now_local_dt = datetime.now()
            self._maybe_fire_event(now_local_dt)
        except Exception as _e:
            pass

        self.after(1000, self.update_display)

    def on_close(self):
        self.running = False
        if self.log_file_handle:
            try:
                self.log_file_handle.close()
            except:
                pass
        self.destroy()

if __name__ == "__main__":
    app = GPSApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
