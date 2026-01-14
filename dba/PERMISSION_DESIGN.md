一、设计背景
数据库需同时支撑两类业务：
1.内部 OA 系统：面向内部员工，要求数据安全、操作可追溯；
2.外部智能体产品：面向外部用户，支持多租户，要求稳定性、扩展性；
初期计划部署在同一 PostgreSQL 实例，兼顾资源成本与业务隔离。
二、核心设计原则
1.最小权限：仅授予业务必需的数据库 / 数据访问权限；
2.分层隔离：物理隔离（Schema）+ 逻辑隔离（行级）+ 权限隔离（DB 用户）；
3.可追溯：全量记录敏感操作，审计日志不可篡改；
4.可扩展：预留拆分独立实例、多租户扩容的设计空间。
三、数据库结构设计
3.1 Schema 规划
Schema名称	归属业务	核心用途	隔离级别
internal_oa	内部 OA 系统	内部用户、权限、审计等核心数据	物理隔离
external_agent	外部智能体产品	外部租户、用户、产品数据	物理隔离
public	公共基础	仅存放跨 Schema 的基础枚举 / 常量	共享
3.2 核心表结构
3.2.1 内部 OA 系统（internal_oa）
-- 内部用户表
CREATE TABLE internal_oa.users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL, -- 登录账号
    real_name VARCHAR(50) NOT NULL,       -- 真实姓名
    department VARCHAR(100) NOT NULL,    -- 所属部门
    role_id INT NOT NULL,                 -- 关联角色ID
    is_active BOOLEAN DEFAULT TRUE,       -- 是否启用
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 角色权限表
CREATE TABLE internal_oa.roles (
    role_id SERIAL PRIMARY KEY,
    role_name VARCHAR(50) UNIQUE NOT NULL, -- 角色名称（超级管理员/部门管理员/普通员工）
    permissions JSONB NOT NULL,            -- 权限列表（如：["user:read", "audit:view"]）
    description VARCHAR(200)
);
-- 操作审计表（核心）
CREATE TABLE internal_oa.operation_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    user_id INT NOT NULL,                  -- 操作用户ID
    operation_type VARCHAR(50) NOT NULL,   -- 新增/修改/删除/查询
    operation_target VARCHAR(100) NOT NULL,-- 操作对象（表/接口）
    operation_content JSONB NOT NULL,      -- 操作内容（变更前后数据）
    ip_address VARCHAR(50),                -- 操作IP
    operation_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES internal_oa.users(user_id)
);
3.2.2 外部智能体产品（external_agent）
-- 租户表（多租户核心）
CREATE TABLE external_agent.tenants (
    tenant_id SERIAL PRIMARY KEY,
    tenant_code VARCHAR(50) UNIQUE NOT NULL, -- 租户唯一标识
    tenant_name VARCHAR(100) NOT NULL,       -- 租户名称
    quota_limit INT DEFAULT 10000,           -- 资源配额（API调用次数）
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 外部用户表
CREATE TABLE external_agent.users (
    user_id SERIAL PRIMARY KEY,
    tenant_id INT NOT NULL,                  -- 所属租户ID
    user_account VARCHAR(50) UNIQUE NOT NULL,-- 外部用户账号
    user_nickname VARCHAR(50),               -- 用户昵称
    is_active BOOLEAN DEFAULT TRUE,
    FOREIGN KEY (tenant_id) REFERENCES external_agent.tenants(tenant_id),
    CONSTRAINT uk_tenant_user UNIQUE (tenant_id, user_account) -- 租户+账号唯一
);
-- 智能体业务数据表
CREATE TABLE external_agent.agent_data (
    data_id BIGSERIAL PRIMARY KEY,
    tenant_id INT NOT NULL,                  -- 租户隔离字段
    user_id INT NOT NULL,                    -- 所属用户ID
    agent_id VARCHAR(50) NOT NULL,          -- 智能体ID
    data_content TEXT,                       -- 业务数据
    FOREIGN KEY (tenant_id) REFERENCES external_agent.tenants(tenant_id),
    FOREIGN KEY (user_id) REFERENCES external_agent.users(user_id)
);
四、用户与权限模型设计
4.1 用户表设计：内外独立，不共用
维度	内部 OA 用户表（internal_oa.users）	外部用户表（external_agent.users）
核心字段	user_id、username、department、role_id	user_id、tenant_id、user_account、user_nickname
关联表	internal_oa.roles	external_agent.tenants
隔离方式	角色权限控制	租户 ID + 行级安全策略
设计原因	内部用户需强权限管控、审计追溯	外部用户需多租户隔离、扩展性

4.2 权限模型：RBAC 为主，资源权限为辅
4.2.1 内部 OA：RBAC（基于角色的访问控制）
（1）角色层级：超级管理员 → 部门管理员 → 普通员工 → 审计员；
（2）权限映射：角色关联权限列表（JSONB 格式），例如：
超级管理员：["user:all", "role:all", "audit:read"]；
普通员工：["user:read", "oa:operate"]；
审计员：["audit:read"]（仅只读审计表）；
（3）权限粒度：Schema 级 + 表级（仅允许访问 internal_oa 下指定表）。
4.2.2 外部业务：RBAC + 行级隔离
（1）租户内角色：租户管理员 → 普通用户；
租户管理员：["tenant:all", "user:manage"]（管理本租户所有用户 / 资源）；
普通用户：["agent:read", "agent:operate"]（仅访问自身数据）；
（2）行级隔离：通过 PostgreSQL RLS（行级安全策略）限制租户数据访问：
-- 启用行级安全策略
ALTER TABLE external_agent.agent_data ENABLE ROW LEVEL SECURITY;
-- 仅允许访问当前租户数据（应用层需设置app.tenant_id）
CREATE POLICY tenant_isolation ON external_agent.agent_data
    USING (tenant_id = current_setting('app.tenant_id')::INT);
五、审计日志设计
5.1 审计范围与规则
业务类型	审计内容	权限控制	保留周期
内部 OA	所有增删改操作、敏感查询（权限 / 用户）	仅允许 INSERT/SELECT，禁止 UPDATE/DELETE	永久保留
外部智能体	租户配置变更、用户注册 / 注销、数据删除	仅允许 INSERT/SELECT，禁止 UPDATE/DELETE	90 天以上
5.2 审计表核心约束
禁止修改 / 删除：回收所有用户对审计表的 UPDATE/DELETE 权限；
索引优化：为 audit_id、operation_time、user_id/tenant_id 建立索引；
写入强制：应用层所有敏感操作必须先写入审计表，再执行业务逻辑。
六、数据库层面权限控制
6.1 数据库用户（DB User）规划
DB 用户名	归属业务	权限范围	密码策略
oa_app_user	内部 OA 应用	仅访问 internal_oa Schema（增删改查）	定期轮换，配置中心存储
agent_app_user	外部智能体应用	仅访问 external_agent Schema（增删改查）	定期轮换，配置中心存储
audit_read_user	审计查询	仅只读 internal_oa.operation_audit、external_agent.tenant_audit	只读权限，严格管控
db_admin	DBA 运维	超级管理员（仅运维使用）	高强度密码，多人管控
6.2 权限配置核心 SQL
-- 1. 创建业务专属用户
CREATE USER oa_app_user WITH PASSWORD 'StrongPassword_123';
CREATE USER agent_app_user WITH PASSWORD 'StrongPassword_456';
CREATE USER audit_read_user WITH PASSWORD 'StrongPassword_789';

-- 2. 授予OA用户仅internal_oa权限
GRANT USAGE ON SCHEMA internal_oa TO oa_app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA internal_oa TO oa_app_user;
REVOKE ALL ON SCHEMA external_agent FROM oa_app_user; -- 明确回收外部权限

-- 3. 授予外部应用仅external_agent权限
GRANT USAGE ON SCHEMA external_agent TO agent_app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA external_agent TO agent_app_user;
REVOKE ALL ON SCHEMA internal_oa FROM agent_app_user;

-- 4. 审计用户仅只读审计表
GRANT SELECT ON internal_oa.operation_audit TO audit_read_user;
GRANT SELECT ON external_agent.tenant_audit TO audit_read_user;
REVOKE ALL ON OTHER TABLES IN SCHEMA internal_oa, external_agent FROM audit_read_user;

-- 5. 审计表权限限制（禁止修改/删除）
REVOKE DELETE, UPDATE ON internal_oa.operation_audit FROM oa_app_user;
GRANT INSERT ON internal_oa.operation_audit TO oa_app_user; -- 仅允许插入			
6.3 数据库隔离的适用场景
需数据库层面隔离（而非仅应用层）的场景：
1.数据安全要求高（内部 OA 敏感数据，应用层隔离易被绕过）；
2.多租户场景（外部业务需严格租户隔离，防止数据泄露）；
3.合规要求（等保 / 审计需数据库级权限控制、操作记录）；
4.运维管控（区分业务资源使用，定位性能问题）。		
七、扩展与风险控制
7.1 外部用户量增长的瓶颈
性能瓶颈：同一实例资源竞争（CPU/IO）、RLS 策略增加 SQL 开销、大表索引失效；
扩展瓶颈：多租户数据量过大导致备份 / 恢复耗时、权限模型耦合度高；
风险瓶颈：外部业务故障（慢查询 / 注入）影响内部 OA、数据备份成本线性增长。
7.2 拆分独立实例的触发条件
满足以下任一条件，建议拆分内部 / 外部业务到不同数据库实例：
资源层面：外部业务资源使用率（CPU / 内存 / IO）持续＞70%，或内部 OA 响应时间增加 50% 以上；
数据层面：外部业务数据量＞1000 万行，或备份时间＞4 小时；
业务层面：外部业务需独立 SLA（99.99% 可用性）、需做读写分离 / 分库分表；
合规层面：外部业务需跨境数据合规，或内部 OA 需等保三级隔离。		
