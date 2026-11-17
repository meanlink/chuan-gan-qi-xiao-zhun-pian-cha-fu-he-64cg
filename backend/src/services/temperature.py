"""温标识别、绝对零度校验与摄氏归一化。

设计口径（与迁移、分析共用，禁止各处自行换算）：
- 旧接入：记录不含 scale 字段，按摄氏直接写入，原值即摄氏值，标记 scale_mode='legacy_celsius'。
- 新接入：每条记录显式声明温标（Celsius/Fahrenheit/Kelvin，支持 °C/°F/K 与大小写变体）。
- 未知温标、非有限数值、低于绝对零度的读数一律拒绝（ValueError），不产生任何换算结果。
- 仅做一次换算并把换算依据写进记录；重试复用已写入的摄氏值，绝不重复换算。
本模块只依赖标准库，便于在无第三方依赖的环境下直接单测。
"""
import math

# 绝对零度（摄氏）。允许极小浮点容差，避免 0 K 等精确边界被舍入误差误伤。
ABSOLUTE_ZERO_C = -273.15
ZERO_TOLERANCE_C = 1e-9

# 温标代码 -> 规范信息。存储统一使用大写代码。
SCALES = {
    'CELSIUS': {'code': 'CELSIUS', 'label': '摄氏 °C', 'unit': '°C'},
    'FAHRENHEIT': {'code': 'FAHRENHEIT', 'label': '华氏 °F', 'unit': '°F'},
    'KELVIN': {'code': 'KELVIN', 'label': '开尔文 K', 'unit': 'K'},
}

# 接入侧允许的温标写法（归一为大写代码）。
_ALIASES = {
    'C': 'CELSIUS', 'CELSIUS': 'CELSIUS', '℃': 'CELSIUS', '°C': 'CELSIUS', '摄氏': 'CELSIUS', '摄氏度': 'CELSIUS',
    'F': 'FAHRENHEIT', 'FAHRENHEIT': 'FAHRENHEIT', '℉': 'FAHRENHEIT', '°F': 'FAHRENHEIT', '华氏': 'FAHRENHEIT', '华氏度': 'FAHRENHEIT',
    'K': 'KELVIN', 'KELVIN': 'KELVIN', '开尔文': 'KELVIN', '开氏': 'KELVIN',
}

TEMP_FIELDS = ('reference', 'measured')


class TemperatureError(ValueError):
    """温标或读数不合法（未知温标、非有限数、低于绝对零度）。"""


def normalize_scale(scale):
    """把各种温标写法归一为规范代码；未知温标抛 TemperatureError。"""
    if scale is None:
        raise TemperatureError('新接入必须逐条声明温标（Celsius/Fahrenheit/Kelvin）')
    key = str(scale).strip().upper()
    code = _ALIASES.get(key)
    if not code:
        raise TemperatureError(f'未知温标 {scale!r}，仅支持 Celsius、Fahrenheit、Kelvin')
    return code


def to_celsius(value, scale):
    """单值换算为摄氏。先在原温标下做绝对零度校验，再换算；换算后再次校验。"""
    code = normalize_scale(scale)
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise TemperatureError(f'读数 {value!r} 不是有限数值')
    if code == 'KELVIN' and number < -ZERO_TOLERANCE_C:
        raise TemperatureError(f'开尔文读数 {number} 低于绝对零度 0 K')
    if code == 'CELSIUS' and number < ABSOLUTE_ZERO_C - ZERO_TOLERANCE_C:
        raise TemperatureError(f'摄氏读数 {number} 低于绝对零度 -273.15 °C')
    if code == 'FAHRENHEIT' and number < -459.67 - ZERO_TOLERANCE_C:
        raise TemperatureError(f'华氏读数 {number} 低于绝对零度 -459.67 °F')
    if code == 'CELSIUS':
        celsius = number
    elif code == 'FAHRENHEIT':
        celsius = (number - 32.0) * 5.0 / 9.0
    else:
        celsius = number - 273.15
    if celsius < ABSOLUTE_ZERO_C - ZERO_TOLERANCE_C:
        raise TemperatureError(f'换算结果 {celsius} °C 低于绝对零度')
    return celsius


def conversion_basis(value, scale, celsius):
    """换算依据快照：记录原值、原温标、公式与结果，随分析输入一起冻结。"""
    code = normalize_scale(scale)
    if code == 'CELSIUS':
        formula = 'celsius = value'
    elif code == 'FAHRENHEIT':
        formula = 'celsius = (value - 32) * 5 / 9'
    else:
        formula = 'celsius = value - 273.15'
    return {
        'original_value': value if isinstance(value, (int, float)) and not isinstance(value, bool) else float(value),
        'original_scale': code,
        'scale_label': SCALES[code]['label'],
        'formula': formula,
        'absolute_zero_check': 'passed',
        'celsius_value': celsius,
    }


def _as_legacy(row):
    """旧记录（无温标语义）：按摄氏直取，校验有限性与绝对零度，保留原始行。"""
    normalized = dict(row)
    basis = {}
    for field in TEMP_FIELDS:
        value = row.get(field)
        try:
            if value is None or value == '' or isinstance(value, bool):
                raise ValueError()
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise TemperatureError(f'{field} 必须为有限数值')
        if number < ABSOLUTE_ZERO_C - ZERO_TOLERANCE_C:
            raise TemperatureError(f'{field} 摄氏读数 {number} 低于绝对零度 -273.15 °C')
        normalized[field] = number
        basis[field] = {
            'original_value': number,
            'original_scale': 'CELSIUS',
            'scale_label': SCALES['CELSIUS']['label'],
            'formula': 'legacy intake, value already in celsius',
            'absolute_zero_check': 'passed',
            'celsius_value': number,
        }
    normalized['scale_mode'] = 'legacy_celsius'
    normalized['conversion'] = basis
    return normalized


def normalize_record(row, index=None):
    """把一条接入记录归一为统一存储形态。

    统一形态：业务字段不变，reference/measured 始终是摄氏分析值；
    另存原始值（reference_original/measured_original）、温标（*_scale）、
    scale_mode（legacy_celsius/explicit_scale）与 conversion 换算依据。
    index 仅用于错误信息中的行号。
    """
    if not isinstance(row, dict):
        raise TemperatureError(f'第 {index + 1 if index is not None else "?"} 行必须是对象')
    prefix = f'第 {index + 1} 行 ' if index is not None else ''
    # 旧接入：整行没有任何温标声明，按摄氏写入。
    has_scale = any(f'{f}_scale' in row or f in row and False for f in TEMP_FIELDS) or \
        ('reference_scale' in row) or ('measured_scale' in row) or ('scale' in row)
    if not has_scale:
        try:
            return _as_legacy(row)
        except TemperatureError as exc:
            raise TemperatureError(prefix + str(exc))
    # 新接入：逐条声明温标。允许每字段独立温标，或用行级 scale 统一声明。
    row_scale = row.get('scale')
    normalized = dict(row)
    basis = {}
    for field in TEMP_FIELDS:
        raw_scale = row.get(f'{field}_scale', row_scale)
        if raw_scale is None:
            raise TemperatureError(prefix + f'{field} 缺少温标声明（新接入须逐条声明温标）')
        value = row.get(field)
        if value is None or value == '':
            raise TemperatureError(prefix + f'{field} 缺少读数')
        try:
            celsius = to_celsius(value, raw_scale)
        except TemperatureError as exc:
            raise TemperatureError(prefix + str(exc))
        original = value if isinstance(value, (int, float)) and not isinstance(value, bool) else float(value)
        normalized[f'{field}_original'] = original
        normalized[f'{field}_scale'] = normalize_scale(raw_scale)
        normalized[field] = celsius
        basis[field] = conversion_basis(original, raw_scale, celsius)
    normalized.pop('scale', None)
    normalized['scale_mode'] = 'explicit_scale'
    normalized['conversion'] = basis
    return normalized


def normalize_records(records):
    """批量归一；任何一行不合法整体拒绝（接入不写入半成品）。"""
    return [normalize_record(row, i) for i, row in enumerate(records)]


def analysis_view(record):
    """分析侧视图：只取 sample/band/reference/measured（均为摄氏），算法不接触原值。"""
    return {
        'sample': record.get('sample'),
        'band': record.get('band'),
        'reference': float(record['reference']),
        'measured': float(record['measured']),
    }


def analysis_views(records):
    return [analysis_view(r) for r in records]


def is_legacy(record):
    """记录是否仍是未迁移的旧存储（裸摄氏行，无换算依据块）。

    注意：迁移后的旧数据保留 scale_mode='legacy_celsius' 作为来源标记，
    但已带 conversion 归一结构；二者必须区分，否则已迁行会被反复判定为漏迁。
    """
    return not isinstance(record, dict) or 'conversion' not in record


def is_migrated(record):
    return isinstance(record, dict) and 'conversion' in record
