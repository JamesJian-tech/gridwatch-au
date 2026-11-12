@AGENTS.md

## Claude Code

- 改完 `nemdoris/` 下的代码后，跑一遍 `.venv/bin/python -m pytest tests`，测试通过才算完成。没有 `.venv` 就按上面的开发命令先建好。
- 改了 SQL、Stream Load 或 `docker-compose.yml` 时，告诉用户 push 之后要看 integration workflow 的结果，并说明本地只验证到了单元测试这一层。
