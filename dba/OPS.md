一、数据库磁盘空间持续增长 / 即将耗尽：排查与处理流程
1.1 紧急止损（磁盘即将耗尽时）
优先保障数据库不宕机，立即执行以下操作：
# 1. 临时扩充分区（如PostgreSQL数据目录挂载在/var/lib/postgresql）
lvextend -L +10G /dev/mapper/pg-data # 临时扩容10G
resize2fs /dev/mapper/pg-data        # 刷新文件系统

# 2. 暂停非核心写操作（如外部业务的日志归档、内部OA的非核心审计备份）
psql -U db_admin -c "ALTER ROLE agent_app_user NOLOGIN;" # 临时禁止外部应用连接（仅紧急场景）
1.2 根因排查（按优先级）
步骤 1：定位空间占用 TOP 对象
-- 1. 查看各Schema磁盘占用
SELECT 
  nspname AS schema_name,
  pg_size_pretty(SUM(pg_total_relation_size(c.oid))) AS total_size
FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE nspname IN ('internal_oa', 'external_agent', 'public')
GROUP BY nspname
ORDER BY SUM(pg_total_relation_size(c.oid)) DESC;

-- 2. 查看各表磁盘占用（TOP10）
SELECT 
  schemaname || '.' || relname AS table_name,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
  pg_size_pretty(pg_relation_size(relid)) AS data_size,
  pg_size_pretty(pg_indexes_size(relid)) AS index_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;

-- 3. 检查WAL日志占用（PostgreSQL核心）
SELECT 
  pg_size_pretty(pg_xlog_location_diff(pg_current_xlog_insert_location(), '0/00000000')) AS wal_total_size;
-- PostgreSQL 10+版本用：
SELECT pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_insert_lsn(), '0/00000000')) AS wal_total_size;

-- 4. 检查临时文件占用
SELECT temp_file_size, temp_files FROM pg_stat_database WHERE datname = 'postgres';
步骤 2：常见根因及特征
根因类型	特征表现
大表无归档 / 清理	external_agent.agent_data 等表数据量持续增长，无历史数据归档策略
WAL 日志堆积	pg_wal 目录占用＞50% 磁盘，archive_command 配置错误或归档存储不可用
索引膨胀	表的 index_size 远大于 data_size（如索引膨胀率＞200%）
临时文件堆积	temp_files/temp_file_size 数值异常高，存在大量未释放的临时查询
审计表无清理	internal_oa.operation_audit 单表占用＞30% 磁盘，无按时间分区 / 清理
1.3 针对性处理
场景 1：WAL 日志堆积（最紧急）
-- 1. 检查归档状态
SELECT archiver_status FROM pg_stat_bgwriter; -- 正常应为"running"

-- 2. 修复归档配置（如归档目录权限错误）
ALTER SYSTEM SET archive_command = 'cp %p /mnt/postgres_wal_archive/%f'; -- 重新配置归档命令
SELECT pg_reload_conf();

-- 3. 手动清理过期WAL（仅归档完成后）
pg_archivecleanup /mnt/postgres_wal_archive/ $(pg_current_wal_lsn())
场景 2：大表数据膨胀
-- 1. 历史数据归档（外部业务agent_data）
-- 创建归档表（按月份分区）
CREATE TABLE external_agent.agent_data_202601 AS 
SELECT * FROM external_agent.agent_data WHERE created_at < '2026-02-01';
-- 删除归档后的数据
DELETE FROM external_agent.agent_data WHERE created_at < '2026-02-01';
-- 清理表空间（释放磁盘）
VACUUM FULL external_agent.agent_data;

-- 2. 审计表分区（内部OA）
-- 按月份拆分审计表，仅保留近90天数据
CREATE TABLE internal_oa.operation_audit_202512 PARTITION OF internal_oa.operation_audit
FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');
DROP TABLE internal_oa.operation_audit_202509; -- 删除90天前的分区
场景 3：索引膨胀
-- 重建膨胀索引（如external_agent.agent_data的idx_tenant_agent_id）
REINDEX INDEX CONCURRENTLY external_agent.idx_tenant_agent_id;
-- 避免锁表，优先使用CONCURRENTLY（PostgreSQL 9.2+支持）
1.4 长期预防措施
1.为核心大表（agent_data、operation_audit）配置分区表（按时间 / 租户），自动归档历史数据；
2.配置定时任务：每日清理临时文件、每周 VACUUM 分析表、每月检查索引膨胀；
3.监控磁盘使用率：设置阈值告警（80% 预警，90% 紧急告警）；
4.限制单条数据大小：如 external_agent.agent_data.data_content 设为 VARCHAR (4096)，避免大文本占用空间。
二、发生误删数据：标准处理流程
2.1 核心原则
先止损，再恢复，避免二次破坏；
优先恢复到临时实例，不直接覆盖生产数据；
全程记录操作，便于审计和回滚。
2.2 标准流程（6 步）
步骤 1：紧急止损（0-5 分钟）
-- 1. 暂停对应业务的写权限（避免数据被覆盖）
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA internal_oa FROM oa_app_user;
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA external_agent FROM agent_app_user;

-- 2. 记录误操作关键信息
-- 记录误操作时间：SELECT now();
-- 记录误操作语句：从pg_stat_activity查询（需提前开启log_min_duration_statement = 0）
SELECT query, backend_start FROM pg_stat_activity WHERE state = 'idle in transaction';

-- 3. 保护WAL日志（防止被清理）
ALTER SYSTEM SET wal_keep_size = '10GB'; -- 临时增大WAL保留量
SELECT pg_reload_conf();
步骤 2：评估影响范围（5-10 分钟）
确认误删对象：表名、数据行 ID、涉及业务（内部 OA / 外部）；
确认数据量：如SELECT COUNT(*) FROM internal_oa.users WHERE user_id IN (1001,1002);（误删的用户 ID）；
确认 RPO/RTO 要求：内部 OA 需≤5 分钟 RPO，外部业务≤15 分钟。
步骤 3：准备恢复环境（10-15 分钟）
# 1. 创建临时恢复实例（与生产环境版本一致）
mkdir -p /mnt/postgres_temp
pg_ctl initdb -D /mnt/postgres_temp

# 2. 恢复最新全量备份至临时实例
pg_basebackup -U db_admin -h localhost -D /mnt/postgres_temp -F p -X s -P

步骤 4：数据恢复（15-30 分钟）
# 1. 配置临时实例的恢复目标（误操作前1分钟）
echo "restore_command = 'cp /mnt/postgres_wal_archive/%f %p'" > /mnt/postgres_temp/recovery.conf
echo "recovery_target_time = '2026-01-14 10:29:00'" >> /mnt/postgres_temp/recovery.conf # 误操作时间：10:30:00
echo "recovery_target_action = 'promote'" >> /mnt/postgres_temp/recovery.conf

# 2. 启动临时实例
pg_ctl -D /mnt/postgres_temp start

# 3. 导出误删数据
psql -U db_admin -d postgres -c "COPY internal_oa.users TO '/tmp/recover_users.csv' WHERE user_id IN (1001,1002);" -h localhost -p 5433
步骤 5：导入生产库（30-35 分钟）
-- 1. 导入恢复的数据
COPY internal_oa.users FROM '/tmp/recover_users.csv' WITH (FORMAT csv);

-- 2. 验证数据完整性
SELECT user_id, username FROM internal_oa.users WHERE user_id IN (1001,1002);
步骤 6：恢复业务权限（35-40 分钟）
-- 恢复写权限
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA internal_oa TO oa_app_user;
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA external_agent TO agent_app_user;

-- 恢复WAL配置
ALTER SYSTEM SET wal_keep_size = '16MB';
SELECT pg_reload_conf();
2.3 特殊场景补充
误删整表：先从全量备份恢复表结构，再按上述流程恢复数据；
误删 Schema：需恢复整个 Schema 至临时实例，再迁移核心表数据。

三、业务 QPS 短时间明显上涨：应对思路
3.1 紧急评估（0-5 分钟）
步骤 1：定位 QPS 上涨来源

-- 1. 查看当前连接数和活跃会话
SELECT 
  usename, -- 连接用户（oa_app_user/agent_app_user）
  count(*) AS conn_count,
  state
FROM pg_stat_activity
GROUP BY usename, state;

-- 2. 查看TOP消耗资源的SQL
SELECT 
  query,
  calls, -- 执行次数（QPS核心指标）
  total_time/1000 AS total_sec,
  mean_time/1000 AS mean_sec
FROM pg_stat_statements
ORDER BY calls DESC
LIMIT 10;
-- 需提前开启pg_stat_statements扩展：shared_preload_libraries = 'pg_stat_statements'
步骤 2：判断上涨类型
上涨类型	特征	应对优先级
合法业务增长	外部 agent_app_user 连接数上涨，SQL 为核心业务查询（如 agent_data 查询）	高
慢查询导致 QPS 虚高	单条 SQL mean_sec＞1s，calls 持续上涨，CPU/IO 使用率＞90%	最高
异常流量（攻击）	大量无效连接，SQL 为随机查询 / 批量插入，来源 IP 集中	紧急
3.2 分级应对策略
场景 1：异常流量 / 攻击（紧急）
# 1. 封禁异常IP（服务器层面）
iptables -A INPUT -s 192.168.1.100 -j DROP # 异常IP

# 2. 限制业务用户连接数
psql -U db_admin -c "ALTER ROLE agent_app_user CONNECTION LIMIT 200;" # 限制外部应用连接数

# 3. 终止异常会话
SELECT pg_terminate_backend(pid) FROM pg_stat_activity 
WHERE usename = 'agent_app_user' AND query LIKE '%invalid_query%';
场景 2：慢查询导致 QPS 虚高（最高优先级）
-- 1. 终止慢查询会话
SELECT pg_terminate_backend(pid) FROM pg_stat_activity 
WHERE state = 'active' AND now() - query_start > '10 seconds';

-- 2. 临时创建索引（针对慢查询）
CREATE INDEX CONCURRENTLY idx_agent_data_user_id ON external_agent.agent_data(user_id);

-- 3. 调整参数缓解压力
ALTER SYSTEM SET work_mem = '64MB'; -- 增大排序内存，减少临时文件
ALTER SYSTEM SET max_parallel_workers_per_gather = 4; -- 开启并行查询
SELECT pg_reload_conf();
场景 3：合法业务增长（高优先级）
# 1. 临时扩容资源（CPU/内存）
# 如云服务器可临时升级配置，或增加swap分区
fallocate -l 16G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile

# 2. 读写分离临时方案（紧急扩容）
# 将读请求分流至只读副本（需提前搭建）
psql -U db_admin -c "ALTER SYSTEM SET synchronous_standby_names = 'replica1';"
3.3 长期优化措施
1.开启连接池（pgbouncer）：将 max_connections 从 300 降至 100，通过连接池提升复用率；
2.优化慢 SQL：对 TOP 慢查询进行改写（如减少 JOIN、添加索引）；
3.业务限流：在应用层为外部租户设置 QPS 阈值（如单租户≤100 QPS）；
4.监控预警：配置 QPS 阈值告警（如外部业务 QPS＞1000 触发告警），提前发现流量上涨。
3.4 验证与复盘
监控 QPS、CPU/IO 使用率是否回落至正常范围；
复盘慢 SQL 根因，纳入迭代优化清单；
评估资源是否需长期扩容，或启动外部业务拆分至独立实例。
四、核心总结
1.磁盘空间问题：优先止损扩容，再排查 WAL / 大表 / 索引膨胀，长期靠分区表 + 定时清理预防；
2.误删数据问题：先暂停写操作，恢复到临时实例验证，再导入生产库，全程记录操作；
3.QPS 上涨问题：先区分流量类型，异常流量先封禁，慢查询先终止 + 加索引，合法增长临时扩容 + 长期优化。