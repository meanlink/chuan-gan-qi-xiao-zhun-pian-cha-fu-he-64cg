from sqlalchemy import String, JSON, Double, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class MigrationState(Base):
    """温标迁移的全局单例（id 固定为 1），承载可暂停/恢复/接管的协调状态。"""
    __tablename__ = 'migration_states'
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default='idle')  # idle/running/paused/verifying/completed/failed
    batch_size: Mapped[int] = mapped_column(Integer, default=200)
    total_datasets: Mapped[int] = mapped_column(Integer, default=0)
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    migrated_datasets: Mapped[int] = mapped_column(Integer, default=0)
    migrated_records: Mapped[int] = mapped_column(Integer, default=0)
    failed_records: Mapped[int] = mapped_column(Integer, default=0)
    leader_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    leader_heartbeat: Mapped[float | None] = mapped_column(Double, nullable=True)
    # 乐观锁：每次协调状态变更 +1，旧执行者带着过期版本写回时被拒绝，不能覆盖新进度。
    version: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(1000), default='')
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    updated_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class MigrationBatch(Base):
    """迁移批次：一个待迁数据批次拆成若干可恢复的小批次，按行区间处理。"""
    __tablename__ = 'migration_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, index=True)
    dataset_name: Mapped[str] = mapped_column(String(160))
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)  # 不含
    status: Mapped[str] = mapped_column(String(20), default='pending')  # pending/running/done/failed/skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    migrated_rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    # 处理前的内容指纹，重试时核对，确保只换算一次、输入未被中途改动。
    input_checksum: Mapped[str] = mapped_column(String(64), default='')
    updated_at: Mapped[float | None] = mapped_column(Double, nullable=True)
