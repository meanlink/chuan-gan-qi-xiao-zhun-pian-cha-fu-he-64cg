# 传感器校准偏差复核

Bootstrap 区间估计的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹、温标声明与摄氏归一）→ 参数化分析（异步 Python worker，任务排队即冻结摄氏输入与换算依据）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览、审计轨迹和温标在线迁移控制台。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery、MigrationState、MigrationBatch。

## 温标与在线迁移
实验室同时接收摄氏、华氏、开尔文读数。存储统一保留**原值 + 温标 + 摄氏分析值 + 逐条换算依据**，分析只使用摄氏。
- 旧接入不带温标字段，按摄氏写入并标记 `legacy_celsius` 来源；新接入逐条声明 `reference_scale`/`measured_scale`（或行级 `scale`），接受 C/F/K、°C/°F、摄氏/华氏/开尔文等等写法。
- 未知温标、非有限数值、低于绝对零度（-273.15°C / -459.67°F / 0K）的读数在接入与迁移两处一律拒绝，不产生半成品。
- 每个分析任务在排队时冻结完整摄氏输入、原值、温标、换算公式、绝对零度校验与输入指纹；分析不回查数据集，迁移期间排队或在途的任务不混用两种存储，历史结论永久保留。
- 迁移按可配置批次（50–2000 条/批）在线进行，可随时暂停/恢复；中断后只续做未完成批次，已完成行按输入指纹幂等跳过、不重复换算。
- 协调者租约 + 状态版本乐观锁：并发接管后旧执行者的过期写回被拒绝，不能覆盖新进度。毒批次（如不可修复的非法读数）达到重试上限后停放并阻止报完成，人工修数后可重置恢复。
- 完成前全量核对历史数据集与迁移期间接入的新批次；存在漏迁或未完成批次时状态保持 `running`，绝不标记 `completed`。
- 页面「温标迁移」（仅 admin）支持启停迁移、查看逐批进度与核对结论、下钻每个数据集的原值与温标。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18206；后端文档 http://localhost:19206/docs；数据库宿主端口 17706。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=sensor-bootstrap；FRONTEND_PORT=18206；BACKEND_PORT=19206；DB_PORT=17706；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1007/，源码不含日志/截图/测试报告。
后端温标与迁移测试位于 `backend/tests/`：`test_temperature.py`（纯标准库，温标归一/绝对零度/换算精度/原值保留）、`test_migration.py`（SQLite 集成，暂停恢复、接管乐观锁、幂等、漏迁核对、毒批次停放与重置）、`test_e2e_api.py`（FastAPI 全路由端到端）。运行：`python -m unittest discover -s tests`。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
