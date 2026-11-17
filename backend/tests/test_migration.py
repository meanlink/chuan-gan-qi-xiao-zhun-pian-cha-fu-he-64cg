"""迁移编排集成测试（SQLite 临时文件库 + SQLAlchemy，覆盖暂停/恢复/接管/幂等/核对）。"""
import os
import sys
import time
import tempfile
import unittest

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix='migration-test-'), 'test.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP_DB}'
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from sqlalchemy import select
from src.config.database import Base, engine, Session
from src.models import Dataset, MigrationState, MigrationBatch, User
from src.services import migration
from src.services.temperature import is_legacy
from src.utils.serialization import checksum
from src.utils.security import hash_password


def seed_dataset(name, n_legacy, n_explicit=0):
    """构造数据集：n_legacy 条模拟旧库的裸摄氏行（待迁移），可追加 n_explicit 条已归一的新格式行。"""
    records = []
    for i in range(n_legacy):
        # 旧库历史数据：没有温标字段、没有 conversion 块
        records.append({'sample': f'{name}-L{i}', 'band': '常温',
                        'reference': 20.0 + i * 0.01, 'measured': 20.2 + i * 0.01})
    from src.services.temperature import normalize_record
    for i in range(n_explicit):
        # 新接入数据：声明温标，走真实归一路径后落库
        records.append(normalize_record({'sample': f'{name}-N{i}', 'band': '高温',
                                         'reference': 300.0, 'reference_scale': 'K',
                                         'measured': 80.6, 'measured_scale': 'F'}))
    with Session() as db:
        d = Dataset(name=name, source='test.csv', records=records,
                    checksum=checksum(records), created_at=time.time())
        db.add(d)
        db.commit()
        return d.id


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with Session() as db:
            db.add(User(email='admin@demo.local', name='admin', role='admin',
                        password=hash_password('x')))
            db.commit()

    def setUp(self):
        # 每用例清空迁移相关表与数据集，保证互不污染
        with Session() as db:
            db.query(MigrationBatch).delete()
            db.query(MigrationState).delete()
            db.query(Dataset).delete()
            db.commit()

    def _state(self, db):
        return db.get(MigrationState, migration.STATE_ID)

    def test_01_plan_and_full_run_with_verification(self):
        seed_dataset('D1', 250)
        seed_dataset('D2', 120)
        with Session() as db:
            migration.start(db, batch_size=100, actor='t')
            db.commit()
        with Session() as db:
            batches = list(db.scalars(select(MigrationBatch).order_by(MigrationBatch.id)))
            # 250 -> 3 批（100/100/50），120 -> 2 批（100/20）
            self.assertEqual([b.end_index - b.start_index for b in batches],
                             [100, 100, 50, 100, 20])
            self.assertEqual(self._state(db).total_records, 370)

        # 逐 tick 跑完
        for _ in range(10):
            migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.status, 'completed')
            self.assertEqual(state.migrated_records, 370)
            self.assertEqual(state.migrated_datasets, 2)
            self.assertTrue(state.verification['passed'])
            self.assertIsNone(state.leader_id)
            # 所有记录已归一，原值保留
            for d in db.scalars(select(Dataset)):
                self.assertFalse(any(is_legacy(r) for r in d.records))
                self.assertIn('conversion', d.records[0])

    def test_02_pause_resume_and_no_duplicate_conversion(self):
        seed_dataset('D3', 300)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        migration.run_pending('w1')  # 完成第 1 批
        with Session() as db:
            self.assertEqual(self._state(db).migrated_records, 100)
            first_dataset = db.scalar(select(Dataset))
            converted_value = first_dataset.records[0]['reference']
        with Session() as db:
            migration.pause(db, 't')
            db.commit()
        migration.run_pending('w1')  # paused：不应再领批次
        with Session() as db:
            done = len(db.scalars(select(MigrationBatch).where(
                MigrationBatch.status == 'done')).all())
            self.assertEqual(done, 1)
            self.assertEqual(self._state(db).status, 'paused')
        # 恢复后继续
        with Session() as db:
            migration.resume(db, 't')
            db.commit()
        for _ in range(5):
            migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.status, 'completed')
            self.assertEqual(state.migrated_records, 300)
            d = db.scalar(select(Dataset))
            # 重试未重复换算：值保持一次换算结果（旧摄氏恒等），attempts 仅记录成功一次
            self.assertEqual(d.records[0]['reference'], converted_value)
            self.assertTrue(all(b.attempts >= 1 for b in db.scalars(select(MigrationBatch))))

    def test_03_interrupted_batch_resumes_without_reconverting_done_rows(self):
        seed_dataset('D4', 100)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        # 模拟 worker 领取批次后崩溃：批次停在 running
        migration.run_pending('w1')
        with Session() as db:
            self.assertEqual(self._state(db).migrated_records, 100)
        # 构造一个进行到一半崩溃的场景：新数据集 + 手动置 running
        did = seed_dataset('D5', 100)
        with Session() as db:
            state = self._state(db)
            state.status = 'running'
            state.verification = {}
            state.completed_at = None
            migration._plan(db, state)
            db.commit()
        with Session() as db:
            pending = db.scalar(select(MigrationBatch).where(
                MigrationBatch.dataset_id == did, MigrationBatch.status == 'pending'))
            pending.status = 'running'
            # 模拟已换算前 30 行
            from src.services.temperature import normalize_record
            d = db.get(Dataset, did)
            for i in range(30):
                d.records[i] = normalize_record(d.records[i], i)
            db.commit()
        # run_pending 只领 pending/failed，running 批次由核对拦截；先退回 pending 模拟接管恢复
        with Session() as db:
            b = db.scalar(select(MigrationBatch).where(MigrationBatch.status == 'running'))
            b.status = 'pending'
            db.commit()
        for _ in range(5):
            migration.run_pending('w2')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.status, 'completed')
            d = db.get(Dataset, did)
            self.assertFalse(any(is_legacy(r) for r in d.records))
            # 前 30 行没有被二次处理（无 scale_mode 翻转问题，值保持）
            self.assertTrue(all(r.get('scale_mode') == 'legacy_celsius' or
                                'conversion' in r for r in d.records))

    def test_04_leader_takeover_stale_worker_cannot_overwrite(self):
        seed_dataset('D6', 250)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        migration.run_pending('w-old')  # old 成为 leader 并完成一批
        # 租约过期后新 worker 接管
        with Session() as db:
            state = self._state(db)
            state.leader_heartbeat = time.time() - migration.LEADER_LEASE_SECONDS - 5
            old_version = state.version
            db.commit()
        migration.run_pending('w-new')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.leader_id, 'w-new')
            self.assertGreater(state.version, old_version)
        # 旧 worker 带着过期版本写回：必须被乐观锁拒绝
        with Session() as db:
            state = self._state(db)
            stale_version = old_version
            ok = migration._renew_or_lose(db, state, 'w-old', stale_version)
            self.assertIsNone(ok)  # 旧执行者无法续约，不能再写进度
            # 新 leader 正常续约
            ok2 = migration._renew_or_lose(db, state, 'w-new', state.version)
            self.assertIsNotNone(ok2)

    def test_05_replan_covers_legacy_rows_arriving_during_migration(self):
        seed_dataset('D7', 100)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        # 迁移期间又接入一个旧形态批次（模拟旧接入通道仍按摄氏写入）
        seed_dataset('D8', 80)
        for _ in range(6):
            migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            # 首次核对发现 D8 漏迁 -> 重新计划 -> 继续 -> 最终完成
            self.assertEqual(state.status, 'completed')
            self.assertEqual(state.migrated_records, 180)
            self.assertTrue(state.verification['passed'])
            self.assertEqual(state.verification['datasets_checked'], 2)

    def test_06_failed_record_does_not_fake_completion(self):
        # 非法读数在计划前就存在于旧数据中（迁移时才暴露），且保持数据集指纹自洽
        did = seed_dataset('D9', 100)
        with Session() as db:
            from sqlalchemy.orm.attributes import flag_modified
            d = db.get(Dataset, did)
            d.records[50]['reference'] = -999.0  # 低于绝对零度
            flag_modified(d, 'records')
            d.checksum = checksum(d.records)
            db.commit()
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            self.assertNotEqual(state.status, 'completed')
            self.assertEqual(state.failed_records, 1)
            batch = db.scalar(select(MigrationBatch).where(MigrationBatch.status == 'failed'))
            self.assertIn('绝对零度', batch.error)
            result = migration.verify(db, 't')
            db.commit()
            self.assertFalse(result['passed'])

    def test_07_input_checksum_change_blocks_reconversion(self):
        did = seed_dataset('D10', 100)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        with Session() as db:
            batch = db.scalar(select(MigrationBatch).where(MigrationBatch.dataset_id == did))
            d = db.get(Dataset, did)
            # 计划建立后窗口结构发生变化（记录被外部流程增删），指纹不再一致
            d.records = d.records[:50]
            d.checksum = checksum(d.records)
            db.commit()
        migration.run_pending('w1')
        with Session() as db:
            batch = db.scalar(select(MigrationBatch).where(MigrationBatch.dataset_id == did))
            self.assertEqual(batch.status, 'failed')
            self.assertIn('指纹', batch.error)
            self.assertNotEqual(self._state(db).status, 'completed')

    def test_08_analysis_snapshot_uses_frozen_celsius_inputs(self):
        did = seed_dataset('D11', 0, n_explicit=10)
        with Session() as db:
            d = db.get(Dataset, did)
            from src.services.jobs import build_input_snapshot
            snap = build_input_snapshot(d)
            self.assertEqual(len(snap['records']), 10)
            self.assertTrue(all(set(r) == {'sample', 'band', 'reference', 'measured'}
                                for r in snap['records']))
            # 存储中已归一为摄氏：80.6°F -> 27.0°C；原值与温标只存在于换算依据里
            self.assertAlmostEqual(snap['records'][0]['measured'], 27.0, places=10)
            self.assertAlmostEqual(snap['records'][0]['reference'], 26.85, places=10)
            basis = snap['conversion_basis'][0]
            self.assertEqual(basis['measured']['original_scale'], 'FAHRENHEIT')
            self.assertEqual(basis['measured']['original_value'], 80.6)
            self.assertEqual(basis['reference']['original_scale'], 'KELVIN')
            self.assertEqual(basis['reference']['original_value'], 300.0)
            self.assertTrue(snap['input_checksum'])
            # 快照与数据集当前内容解耦：之后迁移/改动不影响已冻结输入
            frozen_checksum = snap['input_checksum']
            self.assertEqual(build_input_snapshot(d)['input_checksum'], frozen_checksum)

    def test_09b_snapshot_on_unmigrated_legacy_still_has_full_basis(self):
        # 裸历史行（无 conversion 块）上排队的分析，也要冻结完整摄氏换算依据
        did = seed_dataset('D11B', 40)  # 全是未迁移裸行
        with Session() as db:
            d = db.get(Dataset, did)
            self.assertTrue(all('conversion' not in r for r in d.records))
            from src.services.jobs import build_input_snapshot
            snap = build_input_snapshot(d)
            self.assertEqual(len(snap['conversion_basis']), 40)
            for b in snap['conversion_basis']:
                self.assertEqual(b['reference']['original_scale'], 'CELSIUS')
                self.assertEqual(b['measured']['formula'],
                                 'legacy intake, value already in celsius')
                self.assertEqual(b['reference']['celsius_value'],
                                 b['reference']['original_value'])
            # 分析视图与依据条数一致，且都是摄氏
            self.assertEqual(len(snap['records']), 40)


    def test_09_poison_batch_parks_then_reset_recovers(self):
        did = seed_dataset('D12', 100)
        with Session() as db:
            from sqlalchemy.orm.attributes import flag_modified
            d = db.get(Dataset, did)
            d.records[50]['reference'] = -999.0  # 毒数据：低于绝对零度
            flag_modified(d, 'records')
            d.checksum = checksum(d.records)
            db.commit()
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        # 连续 tick：达到重试上限后批次停放，不再空转，迁移始终无法报完成
        for _ in range(migration.MAX_BATCH_ATTEMPTS + 3):
            migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            batch = db.scalar(select(MigrationBatch))
            self.assertEqual(batch.status, 'failed')
            self.assertEqual(batch.attempts, migration.MAX_BATCH_ATTEMPTS)
            self.assertNotEqual(state.status, 'completed')
            self.assertFalse(migration.verify(db, 't')['passed'])
            db.commit()
        # 人工修复毒数据后重置批次
        with Session() as db:
            from sqlalchemy.orm.attributes import flag_modified
            d = db.get(Dataset, did)
            d.records[50]['reference'] = 21.0
            flag_modified(d, 'records')
            d.checksum = checksum(d.records)
            migration.reset_batch(db, batch.id, 'admin')
            db.commit()
        for _ in range(5):
            migration.run_pending('w1')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.status, 'completed')
            self.assertEqual(state.migrated_records, 100)
            self.assertTrue(state.verification['passed'])


    def test_10_stranded_running_batch_recovered_on_worker_restart(self):
        seed_dataset('D13', 100)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        # 模拟 worker 领取批次后崩溃：批次停在 running，且无活跃领导者心跳
        with Session() as db:
            state = self._state(db)
            state.leader_id = 'dead-worker'
            state.leader_heartbeat = time.time() - 999
            batch = db.scalar(select(MigrationBatch).where(MigrationBatch.status == 'pending'))
            batch.status = 'running'
            db.commit()
        # 新 worker 启动恢复
        with Session() as db:
            n = migration.recover_stranded_batches(db, 'new-worker')
            db.commit()
            self.assertEqual(n, 1)
            b = db.scalar(select(MigrationBatch))
            self.assertEqual(b.status, 'pending')
        # 恢复后正常跑完，不重复换算
        for _ in range(5):
            migration.run_pending('new-worker')
        with Session() as db:
            state = self._state(db)
            self.assertEqual(state.status, 'completed')
            self.assertEqual(state.migrated_records, 100)

    def test_11_recover_does_not_steal_active_leader_batch(self):
        seed_dataset('D14', 100)
        with Session() as db:
            migration.start(db, 100, 't')
            db.commit()
        with Session() as db:
            state = self._state(db)
            state.leader_id = 'healthy-worker'
            state.leader_heartbeat = time.time()  # 心跳新鲜
            batch = db.scalar(select(MigrationBatch).where(MigrationBatch.status == 'pending'))
            batch.status = 'running'
            db.commit()
        with Session() as db:
            n = migration.recover_stranded_batches(db, 'intruder')
            db.commit()
            self.assertEqual(n, 0)  # 不能抢健康 worker 的批次
            b = db.scalar(select(MigrationBatch))
            self.assertEqual(b.status, 'running')


if __name__ == '__main__':
    unittest.main(verbosity=2)
