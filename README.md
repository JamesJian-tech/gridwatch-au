# NEM × Apache Doris：澳洲国家电力市场实时分析

把 AEMO NEMWEB 每 5 分钟发布的调度数据（区域电价、需求、联络线潮流、每台机组出力）持续写入 Apache Doris，用 SQL 做电价、发电结构、电池套利和跨州价差分析。

```
NEMWEB (每 5 分钟)                 家用服务器
┌────────────────────────┐   poll   ┌──────────────────┐ Stream Load ┌──────────────┐  MySQL 协议  ┌────────────────┐
│ Current/  (~2 天)      │ ───────▶ │ python -m        │ ──────────▶ │ Doris FE+BE  │ ◀─────────── │ mysql / Grafana │
│ Archive/  (~13 个月)   │          │ nemdoris live    │   (JSON)    │ db: nem      │              │ / Jupyter       │
└────────────────────────┘          └──────────────────┘             └──────────────┘              └────────────────┘
```

## 数据内容

| Doris 表 | 来源 (NEMWEB) | 粒度 | 说明 |
|---|---|---|---|
| `dispatch_price` | DispatchIS → DISPATCH.PRICE | 5 分钟 × 5 个区域 | 电能 RRP 与 10 个 FCAS 价格 |
| `dispatch_regionsum` | DispatchIS → DISPATCH.REGIONSUM | 5 分钟 × 5 个区域 | 需求、可用容量、净交换、半调度风光出力、电池 |
| `dispatch_interconnectorres` | DispatchIS → DISPATCH.INTERCONNECTORRES | 5 分钟 × 6 条联络线 | 实测潮流、损耗、上下限 |
| `unit_scada` | Dispatch_SCADA → DISPATCH.UNIT_SCADA | 5 分钟 × 约 500 个 DUID | 每台机组实际出力 MW（每年约 5000 万行） |
| `duid_info` | AEMO NEM Registration and Exemption List | 每个 DUID 一行 | 电站名、所在区域、燃料类型，自动归类成 `fuel_group` |
| `ingest_log` | 采集器写入 | 每个文件一行 | 记录已入库的文件，用于跳过重复下载 |

另有视图 `v_price`、`v_region_5min`、`v_unit_5min`，以及可选的小时级物化视图 `mv_region_hourly`。

**时间约定**：所有时间都是 NEM 时间（AEST，UTC+10，全年不调夏令时），`settlementdate` 是 5 分钟区间的**结束**时刻。墨尔本夏令时（AEDT）期间，本地时间比表里的时间早 1 小时。

## 前置要求

- Linux 服务器（Ubuntu 22.04/24.04 均可），x86-64 且 CPU 支持 AVX2
- Docker + Docker Compose v2
- 建议内存 16GB 以上，磁盘预留 20GB（数据本身每年大约 1–2GB，这是粗略估计）
- Python 3.10+

## 快速开始

### 1. 主机参数（只需一次）

```bash
sudo sysctl -w vm.max_map_count=2000000
echo 'vm.max_map_count=2000000' | sudo tee /etc/sysctl.d/99-doris.conf
```

### 2. 启动 Doris

```bash
cp .env.example .env          # 默认镜像版本 4.1.3，可在 .env 里改
docker compose up -d
docker compose logs -f be     # 等到 BE 注册成功
mysql -h 127.0.0.1 -P 9030 -uroot -e "SHOW BACKENDS\G" | grep -E "Alive|Version"
```

用的是 host 网络模式，所以 8030 / 9030 / 8040 会直接暴露在服务器所有网卡上。**先设置 root 密码**，再用防火墙只放行内网：

```bash
mysql -h 127.0.0.1 -P 9030 -uroot -e "SET PASSWORD FOR 'root' = PASSWORD('换成你的密码');"
# 然后把密码填进 .env 的 DORIS_PASSWORD
```

### 3. 建表

```bash
mysql -h 127.0.0.1 -P 9030 -uroot -p < sql/01_schema.sql
mysql -h 127.0.0.1 -P 9030 -uroot -p < sql/02_views.sql
mysql -h 127.0.0.1 -P 9030 -uroot -p < sql/04_materialized_views.sql   # 可选
```

### 4. 安装采集器并自检

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
.venv/bin/python -m nemdoris check
```

`check` 会测试 NEMWEB 能否访问、Doris 能否连接，并列出每张表的行数和时间范围。

### 5. 回填历史数据

Archive 目录保留大约 13 个月的日文件。先试一周：

```bash
.venv/bin/python -m nemdoris backfill --start 2026-11-01
```

没问题再回填一整年（DispatchIS 每天约 5.5MB、SCADA 约 1.2MB，一年总下载量大约 2.5GB，单线程加 1 秒间隔，大约要跑 1–2 小时）：

```bash
.venv/bin/python -m nemdoris backfill --start 2025-11-15 --end 2026-10-31
```

回填可以随时中断再继续，已入库的文件会自动跳过；即使重复写入，UNIQUE KEY 也会去重。

### 6. 实时采集

前台试跑一轮：

```bash
.venv/bin/python -m nemdoris live --once
```

长期运行用 systemd（先改好 `deploy/nem-collector.service` 里的用户名和路径）：

```bash
sudo cp deploy/nem-collector.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nem-collector
journalctl -u nem-collector -f
```

默认每 60 秒查看一次 Current 目录，新文件一般在区间结束后 1 分钟左右出现。不想常驻进程的话，也可以用 cron 每 5 分钟跑一次 `live --once`。

### 7. 机组燃料类型（做发电结构分析需要）

到 AEMO 官网 Registration 页面下载 **NEM Registration and Exemption List**（.xlsx），然后：

```bash
.venv/bin/python -m nemdoris load-duid ~/Downloads/NEM-Registration-and-Exemption-List.xlsx
```

脚本会自动找到包含 `DUID` 列的发电机组工作表，按列名模糊匹配区域、燃料和技术类型，再归类为 Black coal / Brown coal / Gas / Hydro / Wind / Solar / Battery / Liquid fuel / Bioenergy / Other。如果分类不合你意，也可以准备一个含 `duid,region,fuel_group` 三列的 CSV 直接导入覆盖。

AEMO 每隔几个月更新一次这个列表，新电站投运后重新导入即可。

## 分析示例

`sql/03_analysis.sql` 有 16 条现成查询，包括：

- 各区域最新电价和需求
- 每日均价、波动、负电价占比、$300 以上高价次数
- 负电价按小时分布（适合画热力图），以及按月趋势
- 傍晚高峰减去午间价差，粗略衡量电池套利空间
- 需求十分位对应的平均电价
- 维州各燃料按小时的平均出力
- 每日可再生能源占比（不含屋顶光伏）
- **每个电池 DUID 的电能套利收入估算**、平均买入价和卖出价
- 负电价时褐煤机组降出力的幅度
- VIC↔NSW 联络线潮流和两州价差

例如，查看过去 90 天维州负电价集中在哪几个小时：

```sql
SELECT nem_hour, ROUND(100 * AVG(CASE WHEN rrp < 0 THEN 1 ELSE 0 END), 1) AS pct_negative
FROM nem.v_price
WHERE regionid = 'VIC1' AND interval_end >= DATE_SUB(NOW(), INTERVAL 90 DAY)
GROUP BY nem_hour ORDER BY nem_hour;
```

## 接 Grafana

Doris 兼容 MySQL 协议，Grafana 里添加一个 MySQL 数据源，主机填 `服务器IP:9030`、数据库填 `nem` 即可。时间序列面板的时间列用 `interval_end`，Grafana 时区设成 `UTC+10` 才能和数据对齐。仪表盘查询优先用 `mv_region_hourly`，速度更快。

## 注意事项

- **intervention**：发生市场干预时，同一区间会有 intervention=0 和 1 两行。电价一律取 0（定价运行），视图已经处理好了。
- **电池出力的符号**：按 2024 年 IESS 改革后的双向单元注册的电池，充电时 `scadavalue` 为负。
- **SCADA 不含屋顶光伏**，所以可再生占比会偏低。要加上屋顶光伏，可以接 NEMWEB 的 `ROOFTOP_PV/ACTUAL` 目录。
- **单副本**：`replication_num = 1`，磁盘坏了数据就没了。可以定期用 `BACKUP` 备份到 S3/NAS，或者直接重新回填（Archive 保留约 13 个月）。
- 采集器加了 User-Agent，轮询间隔 60 秒，回填每个文件之间停 1 秒，对 NEMWEB 比较友好。

## 测试状况

`pytest tests` 共 12 个测试，覆盖 AEMO CSV 解析（含嵌套 zip 和多出来的新列）、类型转换、目录列表解析、Stream Load（用本地模拟的 FE→BE 307 跳转验证鉴权头没丢）、采集流程和 DUID 表格导入，全部通过。NEMWEB 的文件命名和目录结构已对照 2026 年 11 月的线上目录核实过。

第一次部署时建议先 `backfill` 一周，用 `check` 和 `03_analysis.sql` 里第 15、16 条查询确认数据完整（每个区域每天应有 288 个区间）。

### GitHub Actions

- **ci**（`.github/workflows/ci.yml`）：每次 push 或开 PR 时，在 Python 3.10 和 3.13 上跑单元测试。
- **integration**（`.github/workflows/integration.yml`）：在 GitHub 的 Ubuntu runner 上用 `docker compose` 起一套真实的 Doris，依次执行全部 SQL，回填 1 天并检查 5 个区域是否各有 288 个区间，重复回填确认不会重复写入，再跑一轮 `live --once`、导入 DUID CSV、执行 `03_analysis.sql` 全部查询、刷新物化视图。改动 `nemdoris/`、`sql/` 或 `docker-compose.yml` 时自动触发，每周一 06:00 AEST 定时跑一次，也可以在 Actions 页面手动运行并指定回填日期。

GitHub Actions 的机器每次跑完就销毁，只适合做验证。长期实时采集仍然放在家用服务器上，用 systemd 运行。

### GitHub Pages 看板

- **pages**（`.github/workflows/pages.yml`）：每天 NEM 时间 06:17 从 NEMWEB 抓取前一天以来的 DispatchIS 文件，生成静态看板并发布到 GitHub Pages。这个流程**不经过 Doris**。GitHub 的定时任务可能延迟，某天被跳过时，下次运行会从 Archive 补齐。
- **数据存放**：5 分钟级的区域数据和联络线数据，以每天一个 CSV 的形式存在 `site-data` 分支上。这个分支每次运行都强制覆盖成单个提交，所以仓库历史不会越来越大。
- **生成和页面**：`python -m nemdoris pages` 读取这些 CSV，生成 `data/dashboard.json`；`site/` 里的静态页面用 ECharts 把它画出来。

第一次使用：

1. 打开仓库的 Settings → Pages → Build and deployment，把 Source 选为 **GitHub Actions**。
2. 在 Actions 页面手动运行 pages，`backfill_from` 填 `90`，回填最近 90 天的数据。Archive 最多保留大约 13 个月，填 `400` 就是全部回填；每个日文件之间会停 1 秒。
3. 之后定时任务会自动更新。

本地预览：

```bash
.venv/bin/python -m nemdoris pages --store store --out site/data/dashboard.json --backfill-from 7
python3 -m http.server -d site 8000      # 打开 http://localhost:8000
```

NEMWEB 每秒请求几次就会返回 403，所以每次下载之后都会暂停 1 秒；已经齐全的天数不会再从 Current/ 目录重复下载。

## 可以继续扩展的方向

- `PREDISPATCH` / `P5MIN` 价格预测，对比预测与实际出清价
- 超过 13 个月的历史：NEMWEB `Data_Archive/Wholesale_Electricity/MMSDM` 按月提供完整 MMS 表
- 用 Doris 的 Routine Load 改成 Kafka 管道
- 把能源相关 ASX 股票（AGL、ORG 等）的行情一起放进同一个库，研究电价和股价的关系
