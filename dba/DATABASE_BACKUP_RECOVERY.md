一、设计思路
基于 “同一 PostgreSQL 实例支撑内部 OA + 外部智能体业务” 的特性（内部 OA 要求数据可追溯、外部业务要求高可用），备份恢复方案核心遵循 “分层备份、按需恢复、成本与可用性平衡” 原则：
分层备份：全量 + 增量 + WAL 归档结合，兼顾备份效率与数据完整性；
多维度恢复：支持单表、单 Schema、全实例恢复，适配不同故障场景；
明确 RPO/RTO：内部 OA（核心业务）RPO≤5 分钟、RTO≤30 分钟；外部业务 RPO≤15 分钟、RTO≤1 小时。
二、备份策略设计
2.1 备份类型选择：全量 + 增量 + WAL 归档（PostgreSQL 基础备份 + WAL）
PostgreSQL 无原生 “增量备份”，通过 “基础备份（全量）+ WAL（预写日志）归档” 实现增量效果，是 PostgreSQL 官方推荐的高可用备份方案。
备份类型	实现方式	核心作用
全量备份（基础备份）	使用pg_basebackup工具，备份数据库实例全量数据（含所有 Schema、表、配置）	作为恢复的基础，所有增量恢复均基于全量备份
WAL 归档	开启 PostgreSQL WAL 归档，将产生的 WAL 日志实时归档至独立存储（如 NFS / 对象存储）	实现增量数据恢复，保障 RPO（可恢复到任意时间点）
2.2 备份频率与保留周期
结合业务特性（内部 OA 写低频但敏感、外部业务写高频），制定差异化备份策略：
备份类型	频率	保留周期	存储介质
全量备份	每周日凌晨 2:00（业务低峰期）	30 天（内部 OA）/7 天（外部业务）	本地 SSD + 异地对象存储
WAL 归档	实时（每产生 16MB WAL 文件触发）	45 天（覆盖全量备份保留周期）	异地对象存储（如 S3/OSS）
单表备份	内部 OA 审计表：每日凌晨 3:00	90 天（满足审计追溯要求）	异地对象存储
关键配置（PostgreSQL）
-- 开启WAL归档
ALTER SYSTEM SET wal_level = replica; -- 需重启生效，生产环境建议用replica级别
ALTER SYSTEM SET archive_mode = on;
ALTER SYSTEM SET archive_command = 'cp %p /mnt/postgres_wal_archive/%f'; -- 归档至NFS/对象存储
ALTER SYSTEM SET max_wal_size = 1GB; -- 减少WAL切换频率，降低归档压力
ALTER SYSTEM SET wal_keep_size = 16MB; -- 保留足够WAL文件，避免增量恢复缺失

-- 重载配置
SELECT pg_reload_conf();

全量备份脚本（Shell，可通过 crontab 定时执行）
#!/bin/bash
# 全量备份脚本：pg_backup.sh
BACKUP_DIR="/mnt/postgres_full_backup/$(date +%Y%m%d)"
WAL_ARCHIVE_DIR="/mnt/postgres_wal_archive"
DB_USER="db_admin"
DB_HOST="localhost"

# 创建备份目录
mkdir -p $BACKUP_DIR

# 执行基础备份（全量）
pg_basebackup -U $DB_USER -h $DB_HOST -D $BACKUP_DIR -F p -X s -P -z

# 备份完成后，将备份文件同步至异地对象存储（如OSS）
ossutil cp -r $BACKUP_DIR oss://postgres-backup/full/$(date +%Y%m%d)

# 清理7天前的外部业务全量备份（保留内部OA 30天）
find /mnt/postgres_full_backup -type d -name "202*" -mtime +7 -exec rm -rf {} \;

2.3 不按此策略备份的风险
风险点	后果
仅全量无 WAL 归档	RPO≥24 小时，故障时丢失全量备份后所有数据，外部业务数据丢失量不可控
全量备份频率过低	恢复时需依赖更早的全量备份 + 大量 WAL，RTO 大幅增加（如超过 2 小时）
无异地备份	本地存储故障（如磁盘损坏）时，所有备份丢失，无法恢复
审计表无长期保留	违反内部 OA 可追溯要求，无法满足合规审计
三、恢复方案设计
3.1 核心指标（RPO/RTO）
业务类型	RPO（最大数据丢失量）	RTO（恢复服务时长）	优先级
内部 OA 系统	≤5 分钟	≤30 分钟	最高
外部智能体产品	≤15 分钟	≤1 小时	高
3.2 恢复流程设计（按故障场景）
场景 1：误删部分业务数据（最常见，如误删内部 OA 用户、外部租户数据）
处理流程
1.紧急止损：
暂停对应业务的写操作（内部 OA：禁用 oa_app_user 的 INSERT/UPDATE/DELETE 权限；外部业务：禁用 agent_app_user 的对应权限）；
记录误操作时间点（如2026-01-14 10:30:00），确认误删的数据范围（表名、数据 ID）。
2.数据恢复：
步骤 1：从最近的全量备份中恢复数据至临时实例（避免覆盖生产库）：
# 恢复全量备份到临时目录
pg_ctl -D /mnt/postgres_temp restore -F p -f /mnt/postgres_full_backup/20260111
步骤 2：应用 WAL 归档至误操作前 1 分钟（如2026-01-14 10:29:00），恢复丢失数据：
# 使用pg_waldump定位对应WAL文件，应用至指定时间点
pg_resetwal -D /mnt/postgres_temp --restore-point "2026-01-14 10:29:00"
步骤 3：从临时实例导出误删数据（如COPY internal_oa.users TO '/tmp/recover_users.csv' WHERE user_id IN (1001,1002);），导入生产库。
3.验证与恢复服务：
核对恢复的数据完整性（如用户数、租户数据量）；
恢复业务写权限，监控数据写入是否正常。

指标达标：
RPO：≤5 分钟（WAL 实时归档，恢复到误操作前 1 分钟）；
RTO：≤30 分钟（临时实例恢复 + 数据导入，内部 OA 优先）。

场景 2：单表 / 单 Schema 损坏（如 external_agent.agent_data 表损坏、internal_oa Schema 索引失效）
处理流程
1.定位损坏范围：
使用pg_checksums检查表完整性：pg_checksums -D /var/lib/postgresql/14/main check --table=external_agent.agent_data；
确认损坏类型（表数据损坏 / 索引损坏 / Schema 权限异常）。
2.针对性恢复：
索引损坏：直接重建索引（REINDEX TABLE external_agent.agent_data;），RTO≤5 分钟；
表数据损坏：
① 从全量备份 + WAL 恢复单表至临时表：CREATE TABLE external_agent.agent_data_temp AS SELECT * FROM backup_db.external_agent.agent_data;；
② 替换损坏表：ALTER TABLE external_agent.agent_data RENAME TO agent_data_bak; ALTER TABLE external_agent.agent_data_temp RENAME TO agent_data;；
Schema 损坏：从全量备份恢复对应 Schema（pg_restore -U db_admin -d postgres -n internal_oa /mnt/postgres_full_backup/20260111）。
3.验证：
执行业务查询（如SELECT COUNT(*) FROM external_agent.agent_data;），确认数据完整；
检查权限是否正常（如 agent_app_user 能否访问该表）。

指标达标：
RPO：≤15 分钟（外部业务）/≤5 分钟（内部 OA）；
RTO：≤1 小时（外部业务）/≤30 分钟（内部 OA）。

场景 3：整个数据库实例不可用（如服务器宕机、存储损坏、实例崩溃）
处理流程
1.紧急切换（如有备用实例）：
将业务流量切换至备用实例（通过 VIP / 域名解析），RTO≤10 分钟；
若无备用实例，立即启动全实例恢复流程。
2.全实例恢复：
步骤 1：在新服务器部署 PostgreSQL，配置与原实例一致（参数、表空间、权限）；
步骤 2：恢复最新全量备份至新实例：
pg_basebackup -U db_admin -h 备份存储地址 -D /var/lib/postgresql/14/main -F p -X s -P
步骤 3：应用 WAL 归档至故障发生前的最新时间点：
# 配置recovery.conf，指定WAL归档目录和恢复目标
echo "restore_command = 'cp /mnt/postgres_wal_archive/%f %p'" > /var/lib/postgresql/14/main/recovery.conf
echo "recovery_target_time = '2026-01-14 11:00:00'" >> /var/lib/postgresql/14/main/recovery.conf
步骤 4：启动新实例，校验数据完整性（全量 count 对比、核心业务查询）。
3.流量切换与监控：
将业务流量切换至新实例；
监控 24 小时，确认实例性能、数据写入正常。

指标达标：
RPO：≤15 分钟（外部业务）/≤5 分钟（内部 OA）；
RTO：≤1 小时（外部业务）/≤30 分钟（内部 OA，优先恢复）。
3.3 恢复验证机制
验证维度	验证方式
数据完整性	对比恢复前后的核心表行数（如 internal_oa.users、external_agent.tenants）
权限有效性	模拟业务用户（oa_app_user/agent_app_user）访问对应 Schema / 表，确认权限正常
业务可用性	执行核心业务操作（内部 OA 登录、外部智能体数据查询），确认功能正常
性能验证	检查恢复后实例的 CPU/IO/ 内存使用率，确保无性能瓶颈
四、落地保障措施
4.1 备份监控
配置监控告警：全量备份失败、WAL 归档延迟＞5 分钟、备份存储使用率＞80% 时触发告警（邮件 + 短信）；
每日巡检：检查备份文件完整性、WAL 归档文件数量是否正常。
4.2 恢复演练
每月执行一次单表恢复演练（如恢复 external_agent.agent_data 的历史数据）；
每季度执行一次全实例恢复演练，验证 RTO/RPO 是否达标。
4.3 文档与权限
备份恢复流程文档化，指定专人负责（DBA + 运维）；
备份存储权限管控：仅 DBA 可访问，禁止随意修改 / 删除备份文件。
