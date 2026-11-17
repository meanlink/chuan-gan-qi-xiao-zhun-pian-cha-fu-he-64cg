from fastapi import HTTPException
from sqlalchemy import select
from src.models import Dataset
from src.services.business import metadata
from src.services.temperature import normalize_records, TemperatureError
from src.utils.serialization import checksum
import time


def _validate_shape(records):
    """业务字段与有限性校验；温标语义交给 temperature 模块统一处理。"""
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            raise HTTPException(422, f'第 {index + 1} 行必须是对象')
        for c in metadata()['columns']:
            value = row.get(c['key'])
            if value is None or value == '':
                raise HTTPException(422, f'第 {index + 1} 行缺少字段 {c["label"]}')
            if c['type'] == 'number':
                try:
                    if isinstance(value, bool):
                        raise ValueError()
                    float(value)
                except (TypeError, ValueError, OverflowError):
                    raise HTTPException(422, f'第 {index + 1} 行 {c["label"]} 必须为有限数值')
        for key in ('sample', 'band'):
            if not str(row.get(key, '')).strip():
                raise HTTPException(422, f'第 {index + 1} 行 {key} 不能为空')


def create(db, data):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, '批次名称去除空格后至少需要两个字符')
    if db.scalar(select(Dataset).where(Dataset.name == name)):
        raise HTTPException(409, '该数据批次名已存在，请修改名称')
    _validate_shape(data.records)
    try:
        # 旧接入（无温标字段）按摄氏写入；新接入逐条声明温标，未知温标/低于绝对零度在此拒绝。
        records = normalize_records(data.records)
    except TemperatureError as exc:
        raise HTTPException(422, str(exc))
    d = Dataset(name=name, source=data.source, records=records,
                checksum=checksum(records), created_at=time.time())
    db.add(d)
    db.flush()
    return d
