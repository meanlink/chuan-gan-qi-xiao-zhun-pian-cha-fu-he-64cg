from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from src.config.database import get_db
from src.middlewares.auth import current_user, admin_only
from src.services import migration
router = APIRouter(prefix='/migration')


class StartInput(BaseModel):
    batch_size: int = Field(default=200, ge=50, le=2000)


@router.get('/status')
def status(db=Depends(get_db), u=Depends(current_user)):
    return migration.status_payload(db)


@router.post('/start', status_code=202)
def start(data: StartInput, db=Depends(get_db), u=Depends(admin_only)):
    try:
        migration.start(db, data.batch_size, u.email)
        db.commit()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return migration.status_payload(db)


@router.post('/pause', status_code=202)
def pause(db=Depends(get_db), u=Depends(admin_only)):
    migration.pause(db, u.email)
    db.commit()
    return migration.status_payload(db)


@router.post('/resume', status_code=202)
def resume(db=Depends(get_db), u=Depends(admin_only)):
    migration.resume(db, u.email)
    db.commit()
    return migration.status_payload(db)


@router.post('/verify')
def verify(db=Depends(get_db), u=Depends(admin_only)):
    result = migration.verify(db, u.email)
    db.commit()
    return result


@router.get('/batches/{batch_id}')
def batch_detail(batch_id: int, db=Depends(get_db), u=Depends(current_user)):
    b = db.get(migration.MigrationBatch, batch_id)
    if not b:
        raise HTTPException(404, '迁移批次不存在')
    return {
        'id': b.id, 'dataset_id': b.dataset_id, 'dataset_name': b.dataset_name,
        'start_index': b.start_index, 'end_index': b.end_index, 'status': b.status,
        'attempts': b.attempts, 'migrated_rows': b.migrated_rows, 'error': b.error,
        'input_checksum': b.input_checksum, 'updated_at': b.updated_at,
    }


@router.post('/batches/{batch_id}/reset', status_code=202)
def reset_batch(batch_id: int, db=Depends(get_db), u=Depends(admin_only)):
    try:
        migration.reset_batch(db, batch_id, u.email)
        db.commit()
    except KeyError:
        raise HTTPException(404, '迁移批次不存在')
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return migration.status_payload(db)
