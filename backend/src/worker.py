import time, json, logging, uuid
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record
from src.services import migration
logging.basicConfig(level=logging.INFO)
WORKER_ID = f'worker-{uuid.uuid4().hex[:12]}'
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            # 只使用任务排队时冻结的摄氏输入与换算依据，不回查数据集（迁移期间两种存储不混用）。
            snapshot=j.input_snapshot or {}
            records=snapshot.get('records')
            if not records:
                # 兼容迁移功能上线前已排队的旧任务：按当时口径即时构造摄氏视图。
                from src.services.temperature import analysis_views
                d=db.get(Dataset,j.dataset_id)
                records=analysis_views(d.records)
            result=analyze(records,j.params)
            json.dumps(result,allow_nan=False)
            result['input_freeze']={
                'dataset_id':snapshot.get('dataset_id',j.dataset_id),
                'dataset_checksum':snapshot.get('dataset_checksum',''),
                'input_checksum':snapshot.get('input_checksum',''),
                'storage_generation':snapshot.get('storage_generation','legacy-celsius'),
                'records_frozen':len(records),
            }
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    try:
        migration.run_pending(WORKER_ID)
    except Exception:
        logging.exception('Migration tick failed')
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        # 崩溃遗留的迁移领导者租约同样回收，便于新执行者接管。
        migration.recover_lease(db, WORKER_ID)
        # 崩溃时正在处理的批次退回未完成，恢复后只续做未完成部分，不重复已完成行。
        recovered = migration.recover_stranded_batches(db, WORKER_ID)
        if recovered:
            record(db, 'worker', 'migration.recovered', {'batches': recovered})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
