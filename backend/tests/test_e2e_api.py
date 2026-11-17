"""端到端 HTTP 流程测试：登录 → 旧/新接入（含拒绝路径）→ 分析冻结 → 迁移启停/核对。

用 SQLite + 内存队列（redis 不可用时 enqueue 静默降级），直接驱动 FastAPI 路由与 worker 逻辑。
"""
import os
import sys
import tempfile
import unittest

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix='e2e-'), 'e2e.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP_DB}'
os.environ['JWT_SECRET'] = 'test-secret-32bytes-minimum-xxxxx'
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient
from sqlalchemy import select
from src.main import app
from src.config.database import Session
from src.models import User
from src.services import migration
from src.utils.security import hash_password


def rows(n, scale=None):
    out = []
    for i in range(n):
        row = {'sample': f'E2E-{i:03}', 'band': ['低温', '常温', '高温'][i % 3],
               'reference': 10.0 + i, 'measured': 10.3 + i}
        if scale:
            row['reference_scale'] = scale
            row['measured_scale'] = scale
        out.append(row)
    return out


class E2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from migrations.initial import migrate
        migrate()
        with Session() as db:
            if not db.scalar(select(User).where(User.email == 'admin@demo.local')):
                db.add(User(email='admin@demo.local', name='admin', role='admin',
                            password=hash_password('Demo123!')))
                db.add(User(email='viewer@demo.local', name='viewer', role='viewer',
                            password=hash_password('Demo123!')))
                db.commit()
        cls.client = TestClient(app)
        r = cls.client.post('/auth/login', json={'email': 'admin@demo.local', 'password': 'Demo123!'})
        cls.token = r.json()['access_token']
        cls.auth = {'Authorization': f'Bearer {cls.token}'}
        rv = cls.client.post('/auth/login', json={'email': 'viewer@demo.local', 'password': 'Demo123!'})
        cls.viewer_auth = {'Authorization': f'Bearer {rv.json()["access_token"]}'}

    def test_01_meta_advertises_scales(self):
        meta = self.client.get('/meta').json()
        codes = {s['code'] for s in meta['temperature_scales']}
        self.assertEqual(codes, {'CELSIUS', 'FAHRENHEIT', 'KELVIN'})
        self.assertIn('scale_rules', meta)

    def test_02_legacy_intake_is_celsius(self):
        r = self.client.post('/datasets', headers=self.auth,
                             json={'name': '旧接入批次E2E', 'source': 'legacy.csv', 'records': rows(20)})
        self.assertEqual(r.status_code, 201, r.text)
        d = r.json()
        # 旧接入按摄氏写入并即时归一：无显式温标行，全部带摄氏来源的归一结构
        self.assertEqual(d['explicit_scale_rows'], 0)
        self.assertEqual(d['normalized_rows'], 20)
        self.assertEqual(d['legacy_celsius_rows'], 0)
        detail = self.client.get(f'/datasets/{d["id"]}', headers=self.auth).json()
        rec = detail['records'][0]
        self.assertEqual(rec['scale_mode'], 'legacy_celsius')
        self.assertEqual(rec['conversion']['reference']['original_scale'], 'CELSIUS')
        self.assertNotIn('reference_scale', rec)

    def test_03_explicit_intake_converts_and_keeps_originals(self):
        data = rows(20, scale='F')
        r = self.client.post('/datasets', headers=self.auth,
                             json={'name': '华氏接入批次E2E', 'source': 'f.csv', 'records': data})
        self.assertEqual(r.status_code, 201, r.text)
        d = r.json()
        self.assertEqual(d['explicit_scale_rows'], 20)
        detail = self.client.get(f'/datasets/{d["id"]}', headers=self.auth).json()
        rec = detail['records'][0]
        # 原值与温标保留
        self.assertEqual(rec['measured_original'], 10.3)
        self.assertEqual(rec['measured_scale'], 'FAHRENHEIT')
        # 分析字段已换算为摄氏
        self.assertAlmostEqual(rec['measured'], (10.3 - 32) * 5 / 9, places=10)
        self.assertEqual(rec['conversion']['measured']['formula'],
                         'celsius = (value - 32) * 5 / 9')

    def test_04_unknown_scale_and_below_absolute_zero_rejected(self):
        bad_scale = rows(5, scale='RANKINE')
        r = self.client.post('/datasets', headers=self.auth,
                             json={'name': '未知温标应拒绝', 'records': bad_scale})
        self.assertEqual(r.status_code, 422)
        self.assertIn('未知温标', r.json()['detail'])
        kelvin_bad = rows(5, scale='K')
        kelvin_bad[2]['reference'] = -5
        r = self.client.post('/datasets', headers=self.auth,
                             json={'name': '低于绝对零度应拒绝', 'records': kelvin_bad})
        self.assertEqual(r.status_code, 422)
        self.assertIn('绝对零度', r.json()['detail'])

    def test_05_job_freezes_celsius_snapshot_and_analyzes(self):
        ds = self.client.post('/datasets', headers=self.auth,
                              json={'name': '冻结快照批次E2E', 'records': rows(30, 'K')}).json()
        job = self.client.post('/jobs', headers=self.auth,
                               json={'name': '快照分析E2E', 'dataset_id': ds['id'], 'params': {}}).json()
        self.assertEqual(job['status'], 'queued')
        snap = job['input_snapshot']
        self.assertEqual(len(snap['records']), 30)
        self.assertTrue(all('scale' not in r for r in snap['records']))
        self.assertEqual(snap['conversion_basis'][0]['reference']['original_scale'], 'KELVIN')
        # 直接驱动 worker 执行（绕过 redis 队列）
        from src.worker import execute
        execute(job['id'])
        done = self.client.get(f'/jobs/{job["id"]}', headers=self.auth).json()
        self.assertEqual(done['status'], 'completed', done.get('error'))
        self.assertEqual(done['result']['input_freeze']['records_frozen'], 30)
        self.assertEqual(done['result']['input_freeze']['storage_generation'],
                         'celsius-normalized')

    def test_06_viewer_cannot_control_migration(self):
        r = self.client.post('/migration/start', headers=self.viewer_auth, json={'batch_size': 100})
        self.assertEqual(r.status_code, 403)

    def test_07_full_migration_lifecycle_with_pause_and_verify(self):
        # 直接写入上线前历史库形态：裸摄氏行，无温标、无 conversion 块
        from src.models import Dataset as DS
        from src.utils.serialization import checksum
        import time as _time
        raw = [{'sample': f'HIST-{i}', 'band': '常温', 'reference': 15.0 + i,
                'measured': 15.4 + i} for i in range(120)]
        with Session() as db:
            db.add(DS(name='历史裸行批次E2E', source='pre-upgrade.csv', records=raw,
                      checksum=checksum(raw), created_at=_time.time()))
            db.commit()
        # 初始状态
        status = self.client.get('/migration/status', headers=self.auth).json()
        self.assertEqual(status['status'], 'idle')
        # 启动（合法批大小，120 条 -> 多批）
        r = self.client.post('/migration/start', headers=self.auth, json={'batch_size': 50})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()['status'], 'running')
        self.assertEqual(r.json()['total_records'], 120)
        # 暂停
        paused = self.client.post('/migration/pause', headers=self.auth).json()
        self.assertEqual(paused['status'], 'paused')
        # 暂停期间 worker tick 不推进
        before = self.client.get('/migration/status', headers=self.auth).json()
        migration.run_pending('e2e-worker')
        after = self.client.get('/migration/status', headers=self.auth).json()
        self.assertEqual(after['migrated_records'], before['migrated_records'])
        # 恢复并跑到底
        self.client.post('/migration/resume', headers=self.auth)
        for _ in range(60):
            migration.run_pending('e2e-worker')
        final = self.client.get('/migration/status', headers=self.auth).json()
        self.assertEqual(final['status'], 'completed')
        self.assertTrue(final['verification']['passed'])
        self.assertEqual(final['migrated_records'], 120)
        # 历史裸行已全部归一，原值仍可追溯
        all_ds = self.client.get('/datasets', headers=self.auth).json()
        hist = next(d for d in all_ds if d['name'] == '历史裸行批次E2E')
        self.assertEqual(hist['legacy_celsius_rows'], 0)
        detail = self.client.get(f"/datasets/{hist['id']}", headers=self.auth).json()
        self.assertIn('conversion', detail['records'][0])
        self.assertEqual(detail['records'][0]['conversion']['reference']['original_scale'], 'CELSIUS')
        self.assertEqual(detail['records'][0]['reference'], 15.0)

    def test_08_manual_verify_endpoint_reports(self):
        r = self.client.post('/migration/verify', headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['passed'])

    def test_09_failed_batch_reset_endpoint_recovers(self):
        from src.models import Dataset as DS
        from src.utils.serialization import checksum
        from sqlalchemy.orm.attributes import flag_modified
        import time as _time
        raw = [{'sample': f'RX-{i}', 'band': '常温', 'reference': 18.0,
                'measured': 18.2} for i in range(60)]
        raw[10]['reference'] = -400.0  # 低于绝对零度
        with Session() as db:
            ds = DS(name='重置恢复批次E2E', source='pre.csv', records=raw,
                    checksum=checksum(raw), created_at=_time.time())
            db.add(ds)
            db.commit()
            did = ds.id
        self.client.post('/migration/start', headers=self.auth, json={'batch_size': 60})
        for _ in range(8):
            migration.run_pending('e2e-worker')
        status = self.client.get('/migration/status', headers=self.auth).json()
        self.assertNotEqual(status['status'], 'completed')
        failed = [b for b in status['open_batches'] if b['status'] == 'failed']
        self.assertTrue(failed)
        batch_id = failed[0]['id']
        # viewer 无权重置
        denied = self.client.post(f'/migration/batches/{batch_id}/reset', headers=self.viewer_auth)
        self.assertEqual(denied.status_code, 403)
        # 修复数据（经持久化标记）
        with Session() as db:
            d = db.get(DS, did)
            d.records[10]['reference'] = 18.0
            flag_modified(d, 'records')
            d.checksum = checksum(d.records)
            db.commit()
        r = self.client.post(f'/migration/batches/{batch_id}/reset', headers=self.auth)
        self.assertEqual(r.status_code, 202, r.text)
        for _ in range(8):
            migration.run_pending('e2e-worker')
        final = self.client.get('/migration/status', headers=self.auth).json()
        self.assertEqual(final['status'], 'completed')
        self.assertTrue(final['verification']['passed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
