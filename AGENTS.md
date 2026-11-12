# AGENTS.md

AEMO NEMWEB 5 分钟调度数据 → Apache Doris 的采集器（`nemdoris/`）和分析 SQL（`sql/`）。部署步骤、表含义和分析示例看 `README.md`；CLI 参数看 `python -m nemdoris <cmd> --help`。本文件只记录从代码里不容易看出来的约定和坑。

## 开发命令

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests          # 完全离线，不需要 Doris，也不访问 NEMWEB
.venv/bin/python -m nemdoris check        # 真实访问 NEMWEB；没有 Doris 时最后一行报 [!!] Doris，属正常
```

- `pytest` 不在 `requirements.txt` 里，需要单独装。
- 开发机（Apple Silicon Mac）上一般没有 Docker 和 Doris，目标部署环境是 Linux 家用服务器。需要真实 Doris 的验证，走下面的 integration workflow。
- 配置全部来自环境变量 `DORIS_HOST / DORIS_HTTP_PORT / DORIS_QUERY_PORT / DORIS_USER / DORIS_PASSWORD / DORIS_DB`（`nemdoris/__main__.py:config_from_env`），默认值指向本机 root、库名 `nem`。

## 数据流

`aemo.fetch_listing` 列目录 → `aemo.download` 下载 zip → `aemo.parse_zip`（会递归解开嵌套 zip，Archive 日文件就是 zip 套 zip）→ `schema.to_doris_rows` 按 `MMS_TABLES` 投影并转换类型 → `doris.StreamLoader.load` 通过 Stream Load 以 JSON 写入 → 最后写一行 `ingest_log`。读取走 MySQL 协议（`DorisQuery`，基于 PyMySQL）。

## 不变量

**时间**：所有时间都是 NEM 时间（AEST，UTC+10，不调夏令时），代码里全部用不带时区的 `datetime`（`aemo.nem_now()` 会去掉 tzinfo）。`settlementdate` 是 5 分钟区间的**结束**时刻，所以小时聚合要先 `minutes_sub(settlementdate, 5)`，参考 `sql/04_materialized_views.sql`。

**幂等**：重复写入靠三层保证，改动时三层都要保持成立：
1. `ingest_log` 记录已入库的文件名，`live` 和 `backfill` 会跳过这些文件。
2. `make_label(table, source_file)` 由输入确定性地生成，Doris 在 label 有效期内会拒绝同一 label，`StreamLoader` 把 `Label Already Exists` 当成功处理。label 的输入保持不变。
3. 所有事实表都是 `UNIQUE KEY` 加 merge-on-write，Current/ 和 Archive/ 的数据重叠时会覆盖成同一行。

`ingest_log` 一定在数据表全部写完之后才写，这样中途失败的文件下次会重试。

**Stream Load 跳转**：FE 返回 307 跳到 BE 的另一个端口，`requests` 跟随跳转时会丢掉 Authorization 头，所以 `StreamLoader` 关掉自动跳转、自己手动重发。`tests` 里的 `_FakeDoris` 就是在验证这一点。

**intervention**：同一区间可能同时有 `intervention=0` 和 `1` 两行，价格分析一律取 0。`sql/02_views.sql` 里的视图已经做了过滤，写新查询时优先基于视图。

**对 NEMWEB 保持礼貌**：保留 `aemo.USER_AGENT`、60 秒轮询间隔和回填时每个文件之间的 `time.sleep(1)`。测试里用内置 CSV 和本地 HTTP 服务模拟，不访问真实网络。

## 新增或修改字段、表

一个 MMS 表要改四处，保持同步：
1. `nemdoris/schema.py` 的 `MMS_TABLES`：AEMO 的 `(package, table)` → Doris 表名、要保留的列和类型标记、`required` 主键列。列名是 AEMO 表头的小写形式；解析按表头进行，AEMO 新增的列会被忽略。
2. `sql/01_schema.sql` 的 `CREATE TABLE`：列名和 `MMS_TABLES` 一致；沿用 `UNIQUE KEY`、按月 `AUTO PARTITION`、`replication_num = 1`。
3. 如果是新表，在 `__main__.py:cmd_check` 里写死的表列表中加上它。
4. `tests/test_nemdoris.py`：在内置 CSV 里加上样例行，并补断言。

## SQL 文件

- 所有 SQL 文件都可以重复执行：表和物化视图用 `IF NOT EXISTS`，视图先 `DROP VIEW IF EXISTS` 再创建。新增对象时沿用这个写法。
- 改 Doris 专有语法（AUTO PARTITION、异步物化视图、`date_trunc` 等）时，对照 Doris 4.x 官方文档确认，再用下面的 integration workflow 在真实 Doris 上验证。

## GitHub Actions

- `.github/workflows/ci.yml`：每次 push 或开 PR 时，在 Python 3.10 和 3.13 上跑 `pytest`。
- `.github/workflows/pages.yml`：每 30 分钟更新一次 GitHub Pages 看板，见下一节。
- `.github/workflows/integration.yml`：在 `ubuntu-24.04` runner 上用 `docker compose` 起真实的 Doris，依次执行全部 SQL、回填 1 天、做完整性和幂等检查、`live --once`、`load-duid`、`03_analysis.sql` 和刷新物化视图。改动 `nemdoris/`、`sql/` 或 `docker-compose.yml` 会自动触发，每周一 06:00 AEST 也会定时跑一次。这是开发机上唯一能接触到真实 Doris 的验证途径，加新表或新检查时，在它的 Verify 步骤里补上对应的断言。

## GitHub Pages 看板（`nemdoris/pages.py`、`site/`、`pages.yml`）

这是独立于 Doris 的第二条数据链路：NEMWEB DispatchIS → `site-data` 分支上按天存放的 CSV → `dashboard.json` → `site/app.js`。

- **对 NEMWEB 限速**：每秒请求几次就会被 NEMWEB 返回 403。每次下载后保持 `pause=1.0` 秒；已经齐全的天数（288 个区间 × 5 个区域）不再从 Current/ 下载。
- **按天归档**：CSV 用区间**开始**时刻所在的那一天命名（`pages.nem_day`），所以 `00:00` 结束的区间属于前一天，每天正好 288 个区间。
- **出错就停**：Current/ 下载失败时立刻停止，`state.json` 只推进到最后一个成功的文件，下次运行从失败的文件接着下载。如果上次处理到的文件已经不在 Current/ 里了（中间断档），从 Archive/ 补齐。
- **数据存储分支**：`site-data` 分支每次都强制推送成单个提交，里面只有数据，没有代码。
- **JSON 契约**：`dashboard.json` 的结构是 `pages.build` 和 `site/app.js` 之间的契约，改字段时两边一起改，并同步更新 `tests/test_pages.py::test_build_dashboard`。
- **图表配色**：每个区域的颜色固定（`REGION_SLOT`），在所有图表里都一样。深浅两套色值都写在 `style.css` 的 CSS 变量里，已经通过 dataviz 色板校验。
- **本地预览**：用 `python3 -m http.server -d site`，见 README。

## 文档约定

`README.md` 面向用户，用中文写；代码、注释和日志用英文。
