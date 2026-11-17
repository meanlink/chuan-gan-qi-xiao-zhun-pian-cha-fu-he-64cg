"""旧摄氏数据在线迁移到「原值 + 温标 + 摄氏分析值」统一存储。

关键保证：
- 在线迁移：迁移期间接入与分析照常；分析读取任务快照，不与迁移写冲突。
- 可按批暂停/恢复；中断后只继续未完成批次，已完成批次不重复换算。
- 每个迁移批次按输入指纹幂等：重试先核对指纹并跳过已完成行。
- 领导者租约 + 状态版本乐观锁：并发接管后旧执行者的过期写回被拒绝，不覆盖新进度。
- 完成前全量核对历史与迁移期间新批次；存在漏迁则不能报完成。
"""
import time
import logging
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from src.config.database import Session
from src.models import Dataset, MigrationState, MigrationBatch
from src.services.temperature import normalize_record, TemperatureError, is_legacy
from src.services.audit import record
from src.utils.serialization import checksum

logger = logging.getLogger(__name__)

LEADER_LEASE_SECONDS = 30
STATE_ID = 1
TERMINAL_BATCH = ('done', 'skipped')
# 毒批次（如无法修复的低于绝对零度读数）最多自动重试次数，超出后停放，避免每 tick 空转；
# 仍计入未完成批次，核对阶段继续阻止「报完成」，人工修数后可手动重置恢复。
MAX_BATCH_ATTEMPTS = 5


# ---------- 状态读取 ----------

def get_state(db):
    state = db.get(MigrationState, STATE_ID)
    if state is None:
        state = MigrationState(id=STATE_ID, status='idle', version=0)
        db.add(state)
        db.flush()
    return state


def status_payload(db):
    state = get_state(db)
    batches = list(db.scalars(select(MigrationBatch).order_by(MigrationBatch.id)))
    by_status = {}
    for b in batches:
        by_status[b.status] = by_status.get(b.status, 0) + 1
    per_dataset = {}
    for b in batches:
        item = per_dataset.setdefault(b.dataset_id, {
            'dataset_id': b.dataset_id, 'dataset_name': b.dataset_name,
            'total': 0, 'migrated_rows': 0, 'batches': 0, 'done_batches': 0,
            'failed_batches': 0,
        })
        item['total'] += (b.end_index - b.start_index)
        item['migrated_rows'] += b.migrated_rows
        item['batches'] += 1
        item['done_batches'] += 1 if b.status == 'done' else 0
        item['failed_batches'] += 1 if b.status == 'failed' else 0
    open_batches = [{
        'id': b.id, 'dataset_id': b.dataset_id, 'dataset_name': b.dataset_name,
        'status': b.status, 'attempts': b.attempts, 'migrated_rows': b.migrated_rows,
        'error': b.error,
    } for b in batches if b.status in ('pending', 'failed', 'running')]
    return {
        'status': state.status,
        'batch_size': state.batch_size,
        'total_datasets': state.total_datasets,
        'total_records': state.total_records,
        'migrated_datasets': state.migrated_datasets,
        'migrated_records': state.migrated_records,
        'failed_records': state.failed_records,
        'leader_id': state.leader_id,
        'leader_heartbeat': state.leader_heartbeat,
        'version': state.version,
        'last_error': state.last_error,
        'verification': state.verification or {},
        'started_at': state.started_at,
        'updated_at': state.updated_at,
        'completed_at': state.completed_at,
        'batch_counts': by_status,
        'open_batches': open_batches,
        'max_batch_attempts': MAX_BATCH_ATTEMPTS,
        'datasets': list(per_dataset.values()),
    }


# ---------- 启动 / 暂停 / 恢复 ----------

def start(db, batch_size, actor):
    state = get_state(db)
    if state.status in ('running', 'verifying'):
        return state
    if not (50 <= int(batch_size) <= 2000):
        raise ValueError('批大小需在 50–2000 条之间')
    now = time.time()
    state.batch_size = int(batch_size)
    state.status = 'running'
    state.last_error = ''
    state.verification = {}
    state.completed_at = None
    if state.started_at is None:
        state.started_at = now
    state.updated_at = now
    state.version += 1
    # 即使上次已 completed 也重新规划：_plan 对已有批次的数据集幂等跳过，
    # 只会把完成后新接入且仍是旧形态的数据集补入计划（漏迁可重新发起）。
    _plan(db, state)
    record(db, actor, 'migration.start', {'batch_size': state.batch_size, 'version': state.version})
    return state


def pause(db, actor):
    state = get_state(db)
    if state.status not in ('running', 'verifying'):
        return state
    state.status = 'paused'
    state.updated_at = time.time()
    state.version += 1
    # 不强制剥夺领导者：租约自然过期；正在处理的批次走完后不会再领新批次。
    record(db, actor, 'migration.pause', {'version': state.version})
    return state


def resume(db, actor):
    state = get_state(db)
    if state.status != 'paused':
        return state
    state.status = 'running'
    state.last_error = ''
    state.updated_at = time.time()
    state.version += 1
    record(db, actor, 'migration.resume', {'version': state.version})
    return state


def _plan(db, state):
    """为所有仍为旧存储（legacy_celsius）的数据集建立可恢复批次；新数据已是统一形态则跳过。

    幂等：已存在批次的数据集不重复规划；迁移期间接入的新数据若恰好为旧形态，会在
    核对阶段补入计划，保证漏迁无法报完成。
    """
    size = state.batch_size
    total_datasets = 0
    total_records = 0
    for d in db.scalars(select(Dataset).order_by(Dataset.id)):
        legacy_indexes = [i for i, r in enumerate(d.records) if is_legacy(r)]
        if not legacy_indexes:
            continue
        exists = db.scalar(select(MigrationBatch.id).where(MigrationBatch.dataset_id == d.id).limit(1))
        if exists:
            continue
        total_datasets += 1
        total_records += len(legacy_indexes)
        # 按连续区间切块（旧记录通常连续；新接入数据不会进入该计划）。
        for start in range(0, len(d.records), size):
            end = min(start + size, len(d.records))
            window = d.records[start:end]
            if not any(is_legacy(r) for r in window):
                continue
            db.add(MigrationBatch(
                dataset_id=d.id, dataset_name=d.name, start_index=start, end_index=end,
                status='pending', input_checksum=checksum(window), updated_at=time.time(),
            ))
    state.total_datasets += total_datasets
    state.total_records += total_records
    db.flush()


# ---------- 领导者租约与并发接管 ----------

def _acquire_leadership(db, state, worker_id):
    now = time.time()
    expired = state.leader_heartbeat is None or now - state.leader_heartbeat > LEADER_LEASE_SECONDS
    # 同一 worker 的新会话可直接续约自己的租约；他人租约仅在过期后接管。
    if state.leader_id == worker_id:
        state.leader_heartbeat = now
        db.flush()
        return True, False
    if state.leader_id is None or expired:
        takeover = state.leader_id is not None
        state.leader_id = worker_id
        state.leader_heartbeat = now
        state.version += 1
        db.flush()
        return True, takeover
    return False, False


def _renew_or_lose(db, state, worker_id, version):
    """续约并校验乐观锁。版本已变说明被暂停/接管，旧执行者必须放弃，不能覆盖新进度。"""
    db.refresh(state)
    if state.version != version or state.leader_id != worker_id or state.status != 'running':
        return None
    state.leader_heartbeat = time.time()
    db.flush()
    return state


def recover_lease(db, worker_id):
    """进程启动时回收自己崩溃前可能持有的租约。"""
    state = db.get(MigrationState, STATE_ID)
    if state and state.leader_id == worker_id:
        state.leader_id = None
        state.leader_heartbeat = None
        state.version += 1


def recover_stranded_batches(db, worker_id):
    """把崩溃后停在 running 的批次退回 pending，使中断可恢复、不永久卡住。

    仅在没有活跃领导者（无心跳或租约已过期）时执行，避免抢走另一个健康 worker 手上的批次。
    """
    state = db.get(MigrationState, STATE_ID)
    if state is None or state.status not in ('running', 'verifying'):
        return 0
    now = time.time()
    active = (state.leader_id is not None and state.leader_id != worker_id
              and state.leader_heartbeat is not None
              and now - state.leader_heartbeat <= LEADER_LEASE_SECONDS)
    if active:
        return 0
    stranded = list(db.scalars(select(MigrationBatch).where(MigrationBatch.status == 'running')))
    for b in stranded:
        b.status = 'pending'
        b.updated_at = now
        record(db, worker_id, 'migration.batch.recovered', {'batch': b.id})
    if stranded:
        state.leader_id = None
        state.leader_heartbeat = None
        state.version += 1
    return len(stranded)


# ---------- 批次执行（幂等，不重复换算） ----------

def _process_batch(db, batch, state, worker_id, version):
    dataset = db.get(Dataset, batch.dataset_id)
    if dataset is None:
        batch.status = 'skipped'
        batch.error = '数据集已删除'
        batch.updated_at = time.time()
        return 0
    window = dataset.records[batch.start_index:batch.end_index]
    current_checksum = checksum(window)
    if current_checksum != batch.input_checksum:
        # 输入在迁移期间被改动过：不猜测、不换算，标记失败等待人工核对（在线接入本就追加新批次，不改旧批次）。
        batch.status = 'failed'
        batch.error = '批次输入指纹与计划时不一致，已停止自动换算'
        batch.updated_at = time.time()
        state.failed_records += (batch.end_index - batch.start_index)
        state.last_error = f'数据集 {batch.dataset_name} 批次 {batch.id} 输入变化'
        return 0
    migrated = 0
    for offset, row in enumerate(window):
        if not is_legacy(row):
            continue  # 重试时已迁行直接跳过，绝不二次换算
        index = batch.start_index + offset
        try:
            dataset.records[index] = normalize_record(row, index)
        except TemperatureError as exc:
            batch.status = 'failed'
            batch.error = str(exc)[:900]
            batch.updated_at = time.time()
            state.failed_records += 1
            state.last_error = str(exc)[:900]
            return migrated
        migrated += 1
    batch.migrated_rows += migrated
    batch.status = 'done'
    batch.updated_at = time.time()
    state.migrated_records += migrated
    if migrated:
        # JSON 列的原地改写不会被 SQLAlchemy 自动跟踪，必须显式标记后才会持久化。
        flag_modified(dataset, 'records')
        dataset.checksum = checksum(dataset.records)
    return migrated


def run_pending(worker_id):
    """worker 每个 tick 调用：在 running 状态下领取并处理一个未完成批次。"""
    with Session() as db:
        state = get_state(db)
        if state.status != 'running':
            return
        acquired, takeover = _acquire_leadership(db, state, worker_id)
        if not acquired:
            return
        version = state.version
        if takeover:
            record(db, worker_id, 'migration.takeover', {'version': version})
        # pending 优先；failed 仅在未超重试上限时再次领取，保证中断可恢复又不会毒批次空转。
        batch = db.scalar(
            select(MigrationBatch)
            .where(MigrationBatch.status == 'pending')
            .order_by(MigrationBatch.id).limit(1)
        )
        if batch is None:
            batch = db.scalar(
                select(MigrationBatch)
                .where(MigrationBatch.status == 'failed',
                       MigrationBatch.attempts < MAX_BATCH_ATTEMPTS)
                .order_by(MigrationBatch.id).limit(1)
            )
        if batch is None:
            _verify_and_finish(db, state, worker_id)
            db.commit()
            return
        batch.status = 'running'
        batch.attempts += 1
        batch.updated_at = time.time()
        db.commit()

    # 重开事务处理，避免长事务；状态版本在写回前再次校验。
    with Session() as db:
        state = db.get(MigrationState, STATE_ID)
        batch = db.get(MigrationBatch, batch.id)
        if state is None or batch is None or state.status != 'running':
            return
        if state.version != version or state.leader_id != worker_id:
            # 已被暂停或接管：把批次退回未完成，供恢复后的执行者接手；本次结果不写回。
            if batch.status == 'running':
                batch.status = 'pending'
                batch.updated_at = time.time()
                db.commit()
            return
        migrated = _process_batch(db, batch, state, worker_id, version)
        state.leader_heartbeat = time.time()
        state.updated_at = time.time()
        if batch.status == 'done':
            record(db, worker_id, 'migration.batch.done',
                   {'batch': batch.id, 'dataset': batch.dataset_id, 'rows': migrated})
        else:
            record(db, worker_id, 'migration.batch.failed',
                   {'batch': batch.id, 'error': batch.error})
        db.commit()
        _maybe_verify(worker_id)


def _maybe_verify(worker_id):
    with Session() as db:
        state = db.get(MigrationState, STATE_ID)
        if state and state.status == 'running' and not db.scalar(
            select(MigrationBatch.id).where(MigrationBatch.status.in_(['pending', 'failed', 'running'])).limit(1)
        ):
            _verify_and_finish(db, state, worker_id)
            db.commit()


# ---------- 完成前核对 ----------

def reset_batch(db, batch_id, actor):
    """人工修复数据后，把停放/失败的批次重置为 pending，恢复自动迁移；已完成批次不可重置。"""
    batch = db.get(MigrationBatch, batch_id)
    if batch is None:
        raise KeyError(batch_id)
    if batch.status in TERMINAL_BATCH:
        raise ValueError('已完成批次不能重置')
    batch.status = 'pending'
    batch.error = ''
    batch.attempts = 0
    batch.updated_at = time.time()
    # 重新指纹化当前窗口，使修复后的输入成为新的幂等基线。
    dataset = db.get(Dataset, batch.dataset_id)
    if dataset is not None:
        batch.input_checksum = checksum(dataset.records[batch.start_index:batch.end_index])
    state = get_state(db)
    state.status = 'running'
    state.last_error = ''
    state.updated_at = time.time()
    state.version += 1
    record(db, actor, 'migration.batch.reset', {'batch': batch_id})
    return batch


def verify(db, actor='manual'):
    state = get_state(db)
    missing = []
    datasets_checked = 0
    records_checked = 0
    for d in db.scalars(select(Dataset).order_by(Dataset.id)):
        datasets_checked += 1
        legacy = [i for i, r in enumerate(d.records) if is_legacy(r)]
        records_checked += len(d.records)
        if legacy:
            missing.append({'dataset_id': d.id, 'dataset_name': d.name, 'legacy_rows': len(legacy)})
    open_batches = list(db.scalars(
        select(MigrationBatch).where(MigrationBatch.status.in_(['pending', 'failed', 'running']))
    ))
    result = {
        'datasets_checked': datasets_checked,
        'records_checked': records_checked,
        'missing': missing,
        'open_batches': [
            {'batch': b.id, 'dataset': b.dataset_name, 'status': b.status, 'error': b.error}
            for b in open_batches
        ],
        'checked_at': time.time(),
        'passed': not missing and not open_batches,
    }
    state.verification = result
    state.updated_at = time.time()
    state.version += 1
    record(db, actor, 'migration.verify', {'passed': result['passed'], 'missing': len(missing)})
    return result


def _verify_and_finish(db, state, worker_id):
    state.status = 'verifying'
    state.version += 1
    db.flush()
    result = verify(db, worker_id)
    if result['passed']:
        # 复核迁移数据集数：所有历史及同期新批次均已是统一形态。
        state.migrated_datasets = result['datasets_checked']
        state.status = 'completed'
        state.completed_at = time.time()
        state.leader_id = None
        state.leader_heartbeat = None
        state.version += 1
        record(db, worker_id, 'migration.completed',
               {'datasets': result['datasets_checked'], 'records': result['records_checked']})
    else:
        # 漏迁不能报完成：迁移期间出现的旧形态新数据补入计划，回到 running 继续。
        state.status = 'running'
        state.last_error = f"核对未通过：{len(result['missing'])} 个数据集仍有旧存储行"
        state.version += 1
        _plan(db, state)
        record(db, worker_id, 'migration.replanned', {'missing': len(result['missing'])})
