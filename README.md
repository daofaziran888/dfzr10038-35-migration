# 特征工坊 · 特征工程与标注数据管理平台

面向特征注册与检索、衍生特征工程、时间窗口计算、特征回溯填充、样本分层抽取、标注任务派发、标注质量审核、特征血缘追踪与在线/离线特征服务的数据特征工程与标注管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 特征注册 | `feature_reg` | 特征定义 | 特征编号、特征名称、数据类型 |
| 衍生工程 | `derive_rule` | 衍生规则 | 规则编号、输入特征、衍生函数 |
| 时间窗口 | `time_window` | 窗口模板 | 模板编号、窗口类型、窗口宽度 |
| 特征回溯 | `backfill_job` | 回溯任务 | 任务编号、目标特征、起始日期 |
| 样本分层 | `sample_strata` | 分层策略 | 策略编号、特征范围、分层字段 |
| 标注派发 | `label_task` | 标注任务 | 任务编号、样本范围、标注类型 |
| 标注审核 | `label_review` | 审核批次 | 批次编号、标注任务、审核人 |
| 一致性计算 | `consent_calc` | 一致性报告 | 报告编号、标注任务、评估指标 |
| 特征存储 | `feature_store` | 存储配置 | 配置编号、特征列表、存储引擎 |
| 在线服务 | `online_serv` | 服务端点 | 端点编号、特征列表、QPS上限 |
| 离线导出 | `offline_exp` | 导出管道 | 管道编号、特征范围、导出格式 |
| 特征血缘 | `feature_line` | 血缘链路 | 链路编号、上游特征、下游特征 |
| 特征监控 | `feature_mon` | 监控规则 | 规则编号、目标特征、监控类型 |
| 版本管理 | `version_mgr` | 特征版本 | 版本编号、关联特征、版本标签 |
| 数据切分 | `data_split` | 切分配置 | 配置编号、数据集、切分方式 |
| 特征目录 | `feat_catalog` | 目录索引 | 索引编号、目录名称、父目录 |
| 质量规则 | `quality_rule` | 质量检查 | 规则编号、目标特征、检查类型 |
| 标注词典 | `label_dict` | 标注项 | 词典编号、标签名称、标签值 |
| 批量接入 | `batch_load` | 接入任务 | 任务编号、数据源、目标特征 |
| 特征报告 | `report_task` | 分析报表 | 报告编号、报告类型、特征范围 |
| 调度编排 | `schedule_job` | 调度计划 | 计划编号、任务类型、关联对象 |
| 治理审计 | `govern_audit` | 审计记录 | 记录编号、审计对象、审计类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 端点特征版本迁移与切换

旧版在线端点把版本标签直接拼进逗号分隔的 `特征列表`（如
`order_amount_30d,v2,v1`）。迁移把这些字符串拆成 **特征身份** 与 **绑定版本**
两部分，写入第二版端点特征版本数据（端点行上的 `端点特征版本`）。

规则集中在 `backend/app/domain/endpoint_versions.py`（纯函数）和
`backend/app/services/endpoint_inventory.py`（清单状态机）：

- 在 `feature_reg` 查得到的名字一律按真实特征保留，`v3` 这类真实特征不会被
  当成版本标签删除；未登记且形如 `vN` 的才归为旧版本标签，保留原值和来源。
- 无法核实的旧项记为“待核实”，保留原值、原因和补核方式，不阻断其他特征的绑定。
- 金额特征 `order_amount_30d` 的已核实旧绑定迁移前从旧清单读到 `10`，
  目标版本 `v3` 读到 `12`。
- 端点清单是**只读的复查凭据**：确认后切换不会改写它，回执里的清单指纹与清单一致。
- 存在待核实项或端点尚未迁移时，`全量切换` 返回 `ok=false` 并落一条“未生效”回执，
  版本状态保持原样，端点继续服务旧值。

专用接口（`backend/app/routers/feature_versions.py`）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/online_serv/{id}/feature-inventory` | 迁移样本：身份与绑定分开的清单 |
| POST | `/api/online_serv/{id}/feature-inventory/migrate` | 重新生成草稿清单 |
| POST | `/api/online_serv/{id}/feature-inventory/verify` | 补核单个旧项（保留原值） |
| POST | `/api/online_serv/{id}/feature-inventory/confirm` | 确认唯一清单并写入版本数据 |
| GET | `/api/online_serv/{id}/features/{feature}/value` | 按端点特征版本数据读实际特征值 |
| GET | `/api/online_serv/{id}/readiness` | 判断此刻切换是否生效及补核方式 |
| GET | `/api/version_mgr/{id}/switch-preflight` | 切换前预检 |
| GET | `/api/version_mgr/{id}/receipt` | 版本管理的切换回执 |

端到端校验：`cd backend && .venv/bin/python tests/test_endpoint_versions.py`
（脚本自行拉起临时端口，逐项验证迁移、补核、并发确认、切换与回执）。

领域：数据工程
子方向：数仓与数据湖
