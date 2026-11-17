import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    records = d.records or []
    # 已归一 = 带 conversion 块（迁移后的旧数据仍保留 legacy_celsius 来源标记）；
    # 未迁移 = 上线前的裸摄氏行，无 conversion 块。
    normalized = sum(1 for r in records if isinstance(r, dict) and 'conversion' in r)
    raw_legacy = len(records) - normalized
    return {
        'id': d.id, 'name': d.name, 'source': d.source, 'rows': len(records),
        'checksum': d.checksum, 'created_at': d.created_at,
        'explicit_scale_rows': sum(1 for r in records
                                   if isinstance(r, dict) and r.get('scale_mode') == 'explicit_scale'),
        'legacy_celsius_rows': raw_legacy,
        'normalized_rows': normalized,
        'storage_generation': 'mixed-during-migration' if raw_legacy and normalized else (
            'celsius-normalized' if normalized else 'legacy-celsius'),
    }
def job_dict(j):
    return {k: getattr(j, k) for k in ['id', 'name', 'dataset_id', 'params', 'status', 'result',
                                       'error', 'approved', 'created_at', 'finished_at', 'input_snapshot']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
