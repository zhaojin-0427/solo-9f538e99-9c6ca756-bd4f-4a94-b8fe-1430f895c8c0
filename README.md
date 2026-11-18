# OSM 洪水分析水域面 API

按 OSM 关系号提交**逐次递增修订**的完整节点经纬度与无序 way（含 `outer` /
`inner` 角色），服务按节点引用拼接 way（允许线段反向），生成一个外环、至多
一个内环的 GeoJSON Polygon，并管理"当前供数面"的状态机。

水域面改版后，洪泛分析员还可以为每个关系号**登记一组固定监测点**，服务随后
在每次修订中给出各点位的内外状态，并在点位进水 / 退水时记录事件。

技术栈：**FastAPI + Shapely + SQLite（WAL 模式）**，可 `docker compose` 启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档（可上传修订、登记监测点、查询状态与事件）
open http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
FLOOD_DB_PATH=./flood.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relations/{relation_id}/revisions` | 上传一个完整修订（修订号必须严格递增） |
| GET  | `/relations/{relation_id}/revisions/{revision}` | 按修订号读取**原始输入 + 几何 + 状态 + 监测结果** |
| GET  | `/relations/{relation_id}/revisions` | 列出该关系全部修订号与状态 |
| GET  | `/relations/{relation_id}/current` | 取当前供数面（最新有效修订） |
| POST | `/relations/{relation_id}/monitor-points` | 登记监测点组（1～8 个，登记后不可改） |
| GET  | `/relations/{relation_id}/monitor-points` | 读取监测点组与内外基线 |
| GET  | `/relations/{relation_id}/monitor-events` | 列出全部进水 / 退水事件 |
| GET  | `/health` | 健康检查 |

### 上传请求体

```json
{
  "revision": 1,
  "nodes": [
    {"id": 1, "lat": 0.0, "lon": 0.0},
    {"id": 2, "lat": 0.0, "lon": 1.0},
    {"id": 3, "lat": 1.0, "lon": 1.0},
    {"id": 4, "lat": 1.0, "lon": 0.0}
  ],
  "ways": [
    {"id": 10, "role": "outer", "node_refs": [1, 2, 3, 4, 1]}
  ]
}
```

约束：每次提交为该修订的**完整快照**；最多 **8 条 way、32 个节点**；way
无序，拼接时可反向遍历；`node_refs` 闭合方式沿用 OSM 惯例（首尾节点相同）。

## 监测点登记与进退水事件

### 登记

```json
POST /relations/42/monitor-points
{
  "points": [
    {"id": 1, "lat": 0.5, "lon": 0.5},
    {"id": 2, "lat": 0.5, "lon": 1.5}
  ]
}
```

- 每个关系号**仅可登记一次**，1～8 个编号唯一的点位，登记后不可改；
  重复登记返回 409 `already_registered`。
- 以**登记事务提交时**有效的当前面（最新修订且为 valid）建立内外基线：
  落在水面内记 `inside`，落在外环之外或孔洞内记 `outside`。
- 没有有效当前面（无修订或最新修订无效）时整组拒绝：409 `no_current_face`。
- 任一点位**恰好落在外环或孔洞边界**上时整组拒绝：409 `point_on_boundary`
  （响应附 `point_ids`），不留下半份登记。

### 修订处理（仅登记后的修订）

登记之后的每个修订都会带上 `monitoring` 字段（上传响应与按修订号查询一致）：

```json
"monitoring": {
  "points": [{"point_id": 1, "state": "inside"}, {"point_id": 2, "state": "pending"}],
  "events": [{"point_id": 1, "direction": "in", "revision": 5}]
}
```

- **有效版**：逐点判定——面内 `inside`、面外（含孔洞内）`outside`、
  恰好压在环边界上记 `pending`（待判），待判点**不产生**进退事件；
  恢复明确内外时与**上次明确状态**比较，仅在变化时记录事件：
  `direction=in` 表示进水（外→内），`direction=out` 表示退水（内→外），
  同时记录点号与修订号。
- **无效版**：不改任何点位状态（沿用上一修订的状态，无新事件），
  下一个有效版仍与**中断前**的明确状态比较。
- 登记之前（含基线修订本身）的修订 `monitoring` 为 `null`。

`GET /relations/{relation_id}/monitor-events` 按修订号、点号顺序返回该关系
全部进退水事件。

## 校验与状态机

以下情况修订标记为 `invalid`，**原始输入完整保留**，响应中返回错误码及相关
way id，当前面**停止供数**（`/current` 返回 409）：

- `missing_nodes`：way 引用了未提交的节点
- `broken_ring`：单 way 未闭合、way 间断环、外环拆成多个连通分量
- `fork`：三条以上 way 端点汇聚，或 way 端点落在另一条 way 内部（T 型分叉）
- `self_intersection`：环自交 / 节点重复访问（8 字形等）
- `inner_ring_violation`：内环触碰外环边界、越过外环或位于外环之外
- `missing_outer`：没有 `outer` 角色的 way

停止供数期间，**上一个有效面仍可按修订号读取**，响应里的
`last_valid_revision` 指向它；后续出现有效修订后 `/current` 自动恢复供数。

输入约束违例（超 8/32 上限、角色非法、修订号非递增等）返回 422 / 409，
**不落库**。

## 一致性、原子性与持久化

- SQLite 启用 **WAL + `synchronous=FULL`**：提交即 fsync，重启后数据可查；
  读不阻塞写，读者只能看到完整提交的修订。
- 每次写入（修订、登记）在单个 `BEGIN IMMEDIATE` 事务内完成"递增检查 +
  插入 / 基线计算 + 落库"；登记与上传交错时**按提交顺序**确定基线、状态与
  事件，存储失败整体回滚，**不会留下半份修订或半份登记**。
- 修订的监测状态与事件与修订本体**同事务写入**，因此上传响应与事后按修订号
  查询必然一致；原始输入、几何、状态亦同。
- 数据库位于挂载卷 `/data/flood.db`（环境变量 `FLOOD_DB_PATH` 可改）。

## 自测

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

覆盖：单/多 way 拼接与反向、内环挖洞、缺节点、断环、分叉、T 型分叉、自交、
内环触碰/越界、状态机恢复、修订号冲突、上下限校验、重启恢复、事务原子性、
并发上传/读取、监测点登记（基线、整组拒绝、不可改）、进退水事件、待判与
中断恢复比较、登记/上传交错、上传响应与按修订号查询一致、`/docs` 可用性。
