# 用户体系与 RBAC

后端支持四种认证模式：

- `disabled`：仅用于本地开发，所有请求获得完整权限；
- `api_key`：兼容原有静态 Bearer API Key；
- `rbac`：数据库用户、密码登录和访问/刷新令牌；
- `hybrid`：同时接受 API Key 和数据库用户访问令牌，适合迁移期。

## 首次启动

生产 Compose 默认使用 PostgreSQL 用户存储。生成高熵随机值并配置：

```dotenv
RESEARCH_AGENT_AUTH_MODE=rbac
RESEARCH_AGENT_IDENTITY_STORE_MODE=auto
RESEARCH_AGENT_AUTH_TOKEN_SECRET=replace-with-at-least-32-random-bytes
RESEARCH_AGENT_BOOTSTRAP_ADMIN_TENANT_ID=demo
RESEARCH_AGENT_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
RESEARCH_AGENT_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-long-random-password
RESEARCH_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME=System Administrator
```

首次启动会幂等创建租户管理员。创建成功后，从 Secret/环境配置中删除
`RESEARCH_AGENT_BOOTSTRAP_ADMIN_PASSWORD`，防止引导凭据长期存在。

## 登录和令牌

```http
POST /v1/auth/login
{"tenant_id":"demo","email":"admin@example.com","password":"..."}
```

返回 15 分钟访问令牌和服务端持久化的刷新令牌。刷新操作会旋转令牌；重复使用旧刷新令牌会被视为重放，撤销该用户全部会话并使现有访问令牌失效。

```http
POST /v1/auth/refresh
POST /v1/auth/logout
GET  /v1/auth/me
POST /v1/auth/change-password
```

密码使用带随机盐的 scrypt 存储。连续失败默认 5 次后锁定 15 分钟。改密、管理员重置密码、禁用用户或修改角色都会递增 `token_version` 并撤销刷新会话。

## 管理接口

租户管理员只能管理自己租户的用户：

```http
GET   /v1/users?limit=100&offset=0
POST  /v1/users
GET   /v1/users/{user_id}
PATCH /v1/users/{user_id}
POST  /v1/users/{user_id}/reset-password
GET   /v1/roles
```

系统阻止管理员禁用或降级自己，也阻止删除租户最后一个有效管理员。

## 固定角色与权限

- `ADMIN`：租户内全部权限，包括用户、审计、指标和运维；
- `RESEARCHER`：创建、读取、取消、导出研究以及写入语料；
- `REVIEWER`：读取、审核和导出研究结果。

API 实际检查细粒度 `Permission`，角色只是权限集合。用户响应和 `/v1/roles` 都会返回最终权限，便于前端控制菜单和操作按钮；安全判断始终以后端为准。

## 安全边界

- 邮箱唯一范围是 `(tenant_id, email)`；
- 访问令牌使用 HS256，强制校验 issuer、audience、有效期和 token type；
- 刷新令牌为高熵不透明凭据，数据库仅保存服务端密钥化哈希；
- 用户状态和角色在每次请求时从用户存储重新读取；
- 登录、登出、用户变更和密码操作写入租户审计日志；
- 公网部署仍需 TLS、限流/WAF、Secret Manager，并可在网关接入 OIDC。
