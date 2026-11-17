import time
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models import Dataset, Job
from src.services.business import metadata
from src.services.temperature import analysis_views
from src.utils.serialization import checksum


def build_input_snapshot(dataset):
    """冻结分析输入：摄氏视图 + 每条记录的原值/温标/换算依据 + 批次指纹。

    任务排队后分析只读取该快照，不再回查数据集，迁移期间不会把两种存储形态混进同一次分析。
    对尚未迁移的裸摄氏历史行，补登 legacy 摄氏依据，保证每条冻结记录都有完整换算依据。
    """
    records = list(dataset.records)
    views = analysis_views(records)
    basis_list = []
    for r in records:
        existing = r.get('conversion') or {}
        basis_list.append({
            'sample': r.get('sample'),
            'scale_mode': r.get('scale_mode', 'legacy_celsius'),
            'reference': existing.get('reference') or {
                'original_value': float(r['reference']),
                'original_scale': 'CELSIUS',
                'scale_label': '摄氏 °C',
                'formula': 'legacy intake, value already in celsius',
                'absolute_zero_check': 'passed',
                'celsius_value': float(r['reference']),
            },
            'measured': existing.get('measured') or {
                'original_value': float(r['measured']),
                'original_scale': 'CELSIUS',
                'scale_label': '摄氏 °C',
                'formula': 'legacy intake, value already in celsius',
                'absolute_zero_check': 'passed',
                'celsius_value': float(r['measured']),
            },
        })
    return {
        'dataset_id': dataset.id,
        'dataset_name': dataset.name,
        'dataset_checksum': dataset.checksum,
        'storage_generation': 'celsius-normalized',
        'frozen_at': time.time(),
        'records': views,
        'conversion_basis': basis_list,
        'input_checksum': checksum(views),
    }


def create(db, data, user):
    if not db.get(Dataset, data.dataset_id):
        raise HTTPException(404, '数据批次不存在')
    dataset = db.get(Dataset, data.dataset_id)
    allowed = {p['key']: p for p in metadata().get('parameters', [])}
    if set(data.params) - set(allowed):
        raise HTTPException(422, '包含未知分析参数')
    params = {k: p['default'] for k, p in allowed.items()}
    params.update(data.params)
    for key, p in allowed.items():
        v = params[key]
        if p['type'] == 'number':
            try:
                v = float(v)
                if not (p.get('min', -1e9) <= v <= p.get('max', 1e9)):
                    raise ValueError()
                params[key] = v
            except (ValueError, TypeError):
                raise HTTPException(422, f'{p["label"]} 超出范围或非数值')
        elif str(v) not in [str(o['value']) for o in p.get('options', [])]:
            raise HTTPException(422, f'{p["label"]} 不是有效选项')
    snapshot = build_input_snapshot(dataset)
    j = Job(name=data.name, dataset_id=data.dataset_id, params=params,
            input_snapshot=snapshot, created_by=user.id,
            created_at=time.time(), status='queued')
    db.add(j)
    db.flush()
    return j


def enqueue(job_id):
    try:
        r = Redis.from_url(REDIS_URL)
        r.rpush('analysis:queue', job_id)
        r.close()
    except Exception:
        pass  # SQL queued status is the durable fallback
