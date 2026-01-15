一、优化背景
基于 “同一 PostgreSQL 实例支撑内部 OA + 外部智能体业务” 的场景，结合前期评估的风险点（资源竞争、审计表写入性能、外部业务数据膨胀），从数据目录存储、核心参数调优维度做针对性优化，兼顾稳定性、性能与成本。
二、数据目录与存储使用方式优化
2.1 现状分析
当前数据库所有 Schema（internal_oa/external_agent/public）默认存储在 PostgreSQL 默认数据目录（如/var/lib/postgresql/14/main），未做存储分层，存在以下问题：
1.内部 OA 审计表（写密集）与外部 agent_data 表（读密集）共用存储 IO，易引发资源竞争；
2.所有数据混存，备份 / 恢复时无法按业务维度拆分，效率低；
3.无存储分层策略，高频访问数据（如外部 agent_data）未使用高性能存储，低频数据（如审计历史）占用高性能资源。
2.2 优化建议
2.2.1 按业务维度拆分表空间（Tablespace）
表空间名称	关联业务 / 表	存储介质	设计目的
ts_oa_audit	nternal_oa.operation_auditi	高性能 SSD	审计表为写密集型，SSD 降低写入延迟，保障内部 OA 操作可追溯性
ts_external_agent	external_agent.agent_data	高性能 SSD	外部智能体数据为高频读写，SSD 提升查询 / 写入性能
ts_archive	internal_oa.users_history、external_agent.agent_data_history	普通机械硬盘	历史归档数据访问频率低，机械硬盘降低存储成本
2.2.2 具体操作（PostgreSQL）
-- 1. 创建表空间（需先在服务器创建对应目录，如/var/lib/postgresql/ts_oa_audit）
CREATE TABLESPACE ts_oa_audit OWNER db_admin LOCATION '/var/lib/postgresql/ts_oa_audit';
CREATE TABLESPACE ts_external_agent OWNER db_admin LOCATION '/var/lib/postgresql/ts_external_agent';
CREATE TABLESPACE ts_archive OWNER db_admin LOCATION '/var/lib/postgresql/ts_archive';

-- 2. 将核心表迁移至对应表空间
ALTER TABLE internal_oa.operation_audit SET TABLESPACE ts_oa_audit;
ALTER TABLE external_agent.agent_data SET TABLESPACE ts_external_agent;

-- 3. 新增归档表并指定表空间（用于存储外部业务历史数据）
CREATE TABLE external_agent.agent_data_history (LIKE external_agent.agent_data INCLUDING ALL)
TABLESPACE ts_archive;
2.2.3 优化价值与不优化的风险
优化点:
表空间分层存储	
优化价值
1. 读写密集表隔离 IO 资源，减少内部 OA 与外部业务的资源竞争；
2. 归档数据降存储成本；
3. 备份可按表空间拆分，提升效率
不优化的风险
1. 存储 IO 瓶颈导致内部 OA 审计写入延迟、外部智能体查询卡顿；
2. 全量备份耗时＞4 小时，运维效率低；
3. 高性能存储资源被低频数据占用，成本浪费
三、PostgreSQL 核心参数调整（3 个关键参数）
3.1 参数 1：shared_buffers（共享缓冲区）
调整建议
默认值：通常为物理内存的 1/16（如 16G 内存服务器默认 1G）；
调整后：shared_buffers = 4GB（假设服务器总内存 16G，设置为物理内存的 1/4）。
调整原因
1.场景适配：当前实例承载两类业务，外部 agent_data 表高频读（多租户数据查询）、内部 OA 高频写（审计日志），更大的共享缓冲区可缓存热点数据（如外部租户的常用智能体配置、内部 OA 的角色权限表），减少磁盘 IO；
2.性能提升：PostgreSQL 通过 shared_buffers 减少对操作系统缓存的依赖，对读密集型的外部业务，缓存命中率可提升 30% 以上。
不调的风险
1.外部业务查询频繁触发磁盘 IO，响应时间从＜100ms 增至＞500ms，用户体验下降；
2.内部 OA 审计表写入时，缓冲区不足导致频繁刷盘，引发 checkpoint 压力增大，实例稳定性降低；
3.内存资源利用率低（默认 1G），16G 内存服务器的剩余内存未被有效利用。
注意事项
shared_buffers 不宜超过物理内存的 1/2（避免操作系统内存不足）；
调整后需重启 PostgreSQL 生效。
3.2 参数 2：max_connections（最大连接数）
调整建议
默认值：100；
调整后：max_connections = 300，同时配套设置superuser_reserved_connections = 10（保留给 DBA 的超级连接）。
调整原因
1.业务诉求：
内部 OA：同时在线员工约 500 人，单员工平均占用 1-2 个连接，峰值连接数约 100；
外部智能体：多租户并发访问，峰值连接数约 150；
预留 50 个连接给审计、监控、备份等运维操作；
2.避免连接耗尽：默认 100 连接无法支撑两类业务峰值，易触发 “too many connections” 错误。
不调的风险
1.外部业务高峰期（如租户集中调用智能体 API）触发连接耗尽，返回 500 错误，违反外部业务稳定性要求；
2.内部 OA 员工操作时（如批量权限修改）因连接不足导致操作失败，影响内部办公；
3.运维人员（如 DBA）无法通过超级连接登录排查问题，故障恢复时间延长。
注意事项
连接数增加会消耗内存（每个连接约占用几 MB 内存），需确保服务器内存充足（16G 内存支撑 300 连接无压力）；
建议配合pgbouncer连接池使用，避免连接数过多导致的资源浪费（后期优化项）。
3.3 参数 3：checkpoint_completion_target（检查点完成目标）
调整建议
默认值：0.5；
调整后：checkpoint_completion_target = 0.9。
调整原因
1.场景适配：当前实例存在大量写操作（内部审计表写入、外部 agent_data 更新），checkpoint 是 PostgreSQL 将脏数据刷到磁盘的关键操作，0.9 表示 checkpoint 在checkpoint_timeout（默认 5 分钟）的 90% 时间内完成，刷盘更平滑；
2.减少 IO 风暴：内部 OA 审计表批量写入 + 外部业务高频更新易引发集中刷盘，0.9 的配置可分散 IO 压力，避免瞬间 IO 飙升。
不调的风险
1.高写入场景下，checkpoint 集中刷盘导致 IO 使用率瞬间达 100%，引发内部 OA 审计写入延迟、外部业务查询卡顿；
2.频繁的 IO 风暴可能触发 PostgreSQL WAL（预写日志）同步失败，增加数据丢失风险；
3.实例负载波动大，稳定性下降，极端场景下可能触发 OOM 或实例宕机。
注意事项
需配套确认checkpoint_timeout为默认值（5min），无需调整；
调整后无需重启，执行SELECT pg_reload_conf();即可生效。