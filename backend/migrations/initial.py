import logging
from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

logger = logging.getLogger(__name__)

# 既有表的增量列定义（表名 -> [(列名, 列类型 DDL, 默认值 SQL)]）。
# 项目无 Alembic，create_all 只能建新表，线上旧库需要显式补列；默认值保证旧数据语义不变。
_ADDITIVE_COLUMNS = {
    'jobs': [
        ('input_snapshot', 'JSON', "'{}'"),
    ],
}


def _add_missing_columns():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue  # 新库由 create_all 一次性建出
            present = {col['name'] for col in inspector.get_columns(table)}
            for name, ddl_type, default_sql in columns:
                if name in present:
                    continue
                conn.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type} DEFAULT {default_sql}'
                ))
                logger.info('schema additive: %s.%s added', table, name)


def migrate():
    Base.metadata.create_all(engine)
    _add_missing_columns()


if __name__ == '__main__':
    migrate()
