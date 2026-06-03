# JAV Search v1.2 更新说明

本次更新包含两项功能：**解除 15 条结果限制 + 分页显示**，以及**网页登录认证**。

---

## 一、解除 15 条结果限制 + 分页

### 问题背景

此前每次搜索最多只显示 15 条结果。经确认，这**不是来源网站（JavBus / JavDB）的限制**，而是早期代码中写死的上限——为了控制详情页抓取速度，列表抓取被截断到 15 条。

### 改动内容

1. **翻页抓取**
   JavBus 和 JavDB 的搜索均改为自动翻页：先逐页收集列表中的影片链接，直到达到设定上限或没有更多页，再统一并发抓取每条的详情（演员、标签、简介等）。

2. **可配置上限**
   新增配置项 `max_results`，表示每个数据源最多抓取多少条，默认 **60**。

3. **前端分页显示**
   结果不再一次性平铺，而是分页展示：
   - 每页 **12 条**
   - 底部有页码导航（上一页 / 页码 / 下一页），并显示「第 X / Y 页，共 N 条」
   - 翻页、翻看详情、翻译、资源搜索均按全局索引对应，不会错位

### 性能提示

每一条结果都需要额外访问一次详情页来获取完整信息，因此抓取条数越多越慢，走代理时尤为明显。`max_results` 设为 60 是速度与数量的平衡值；若觉得慢，可调小。

### 涉及文件

- `backend/scrapers/javbus.py` — 新增 `_fetch_list_paged()` 翻页抓取，移除旧的截断逻辑
- `backend/scrapers/javdb.py` — `_search()` 改为多页抓取
- `backend/scrapers/__init__.py` — 聚合搜索新增 `max_results` 参数并向下传递
- `backend/config_manager.py` — 默认配置新增 `max_results: 60`
- `backend/main.py` — 搜索接口从配置读取 `max_results`
- `frontend/index.html` — 新增 `renderPage()` / `renderPagination()` / `gotoPage()` 分页逻辑

---

## 二、网页登录认证

### 功能说明

- 网页需登录后才能访问，**不开放注册**
- **账号、密码、会话密钥全部在 docker-compose 的 `environment` 中配置**
- 会话基于 HMAC 签名的 Cookie 实现，**无需数据库**，重启容器后凭据仍有效（只要 `AUTH_SECRET` 不变）

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AUTH_USERNAME` | 登录用户名 | `admin` |
| `AUTH_PASSWORD` | 登录密码。**留空则关闭认证**（任何人可直接访问，仅建议内网调试时用） | 空 |
| `AUTH_SECRET` | 会话签名密钥，填一串足够长的随机字符串。修改它会让所有已登录用户重新登录 | 进程启动时随机生成 |
| `AUTH_SESSION_TTL` | 会话有效期（秒） | `604800`（7 天） |

> **部署前务必修改 `AUTH_PASSWORD` 和 `AUTH_SECRET`。**
> 若不设 `AUTH_SECRET`，每次重启容器会随机生成新密钥，导致所有人需要重新登录。

### 配置示例

```yaml
environment:
  - AUTH_USERNAME=admin
  - AUTH_PASSWORD=你的强密码
  - AUTH_SECRET=一串很长的随机字符串例如32位以上
  - AUTH_SESSION_TTL=604800
```

### 使用流程

1. 访问 `http://群晖IP:8085`，未登录时自动跳转到登录页 `/login`
2. 输入 compose 中配置的账号密码，登录成功后进入主界面
3. 主界面右上角有「退出」按钮，点击即登出并回到登录页
4. 会话过期或未登录时，所有接口返回 401，前端自动跳回登录页

### 行为细节

- 白名单路径（登录页、登录接口、健康检查、登录状态查询）无需认证即可访问
- 受保护的接口未登录时返回 `401`，页面请求则重定向到登录页
- 密码校验使用恒定时间比较，降低时序攻击风险

### 涉及文件

- `backend/auth.py` — **新增**，认证核心（凭据校验、token 生成与验签）
- `backend/main.py` — 新增认证中间件、`/api/login`、`/api/logout`、`/api/auth/status` 接口，以及 `/login` 页面路由
- `frontend/login.html` — **新增**，登录页面
- `frontend/index.html` — 新增登录状态检查、退出按钮、401 自动跳转
- `docker-compose.yml` / `docker-compose.host.yml` — 新增 `AUTH_*` 环境变量

---

## 三、部署步骤

下载新版覆盖原项目目录后：

```bash
cd /volume1/docker/jav-search

# 先编辑 compose，修改 AUTH_PASSWORD 和 AUTH_SECRET
# （host 网络模式用 docker-compose.host.yml）

docker-compose down
docker-compose -f docker-compose.host.yml up -d --build
docker-compose logs -f
```

启动后访问 `http://群晖IP:8085`，会先看到登录界面。

---

## 四、新增 / 变更的 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/login` | 登录页面 |
| POST | `/api/login` | 提交账号密码，成功后下发会话 Cookie |
| POST | `/api/logout` | 退出登录，清除 Cookie |
| GET  | `/api/auth/status` | 查询认证是否启用、当前是否已登录 |
| POST | `/api/search` | （变更）现按配置的 `max_results` 翻页抓取 |
