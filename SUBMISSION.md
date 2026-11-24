# 提交说明

Fork：<https://github.com/starqi0329/AI-Engineer-Quiz>  
分支：`feature/modify-tag-title-describe`

## 运行与验证（Docker）

在仓库根目录：

```bash
docker compose up --build -d
docker compose exec keepstack python -m keepstack seed
```

浏览器打开 <http://localhost:8000>，使用 `admin` / `admin` 登录。  
`docker-compose.yml` 已设置 `KEEPSTACK_AI_ENABLED=false`。数据在命名卷 `keepstack-data`，只在第一次执行 seed。重复 seed 会再插入一套素材和集合。

Seed 使用 `random.seed(7)`，生成 24 个素材和 3 个集合，以及 `curator`、`contributor`、`viewer`（密码 `demo1234`）。演示数据里没有 `approved`、`campaign`、`internal`，标题里也没有 harbor。题目场景需要登录后自行改标题或加标签，再用组合条件查询。

页面验证：

1. 左侧点标签名称加入「必须全部包含」，点 Exclude 加入「命中任一即排除」。同一标签不能同时在两边。上方芯片可单条去掉，Clear 清空关键词、类型、集合和标签。
2. 排序用创建时间升序或降序。每页 24 条，上一页 / 下一页，并显示总数。改筛选或排序回到第 1 页。
3. Export CSV 下载当前筛选和排序下的全部匹配行，不限于本页。未登录导出返回 401。
4. 用 admin、curator 或 contributor 打开素材，改标题、描述或标签后再搜索、再导出。viewer 不能改。

自动化测试不在镜像里（镜像只装 `requirements.txt`，没有 pytest）。在装了开发依赖的环境执行：

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run --no-sync python -m pytest -q
```

测试自己插入临时库数据，不依赖 seed 文件。覆盖：有关键词时仍按创建时间排序、相同 `created_at` 分页不重复不遗漏、导出与分页顺序一致、中文/逗号/引号/换行、零结果仍有表头、导出遵守素材类型、改标题/描述/标签后的下一次查询和导出。

## 实测结果

- 关键词、集合、包含全部标签、排除任一标签取交集，只返回 `active`。标签按完整小写名称匹配。同一标签既包含又排除时结果为空。关键词空白表示不限制关键词。`roa` 仍会命中 `road`，这是原有 FTS 前缀检索。
- 有关键词时不再按相关度打乱，列表按所选创建时间排序，并用素材 id 打破相同时间的平局。
- 导出列：`uuid,title,description,filename,media_type,tags,created_at`。多个标签按名称排序，用 `; ` 连接。文件为 UTF-8 BOM，便于 Excel 打开中文。
- 保存或增删标签后，当前页列表会重新查询。旧标题不再命中，新标题、新描述和导出列与库中内容一致。
- 容器内用上述方式启动后，登录、翻页、导出和改元数据可用。本机未安装 pytest 的解释器上，用独立脚本跑过排序、导出和修改后查询这几项，结果符合断言。完整 `pytest -q` 需在带 `.[dev]` 的环境再跑一遍。

## 取舍

- 筛选、排序、导出共用同一套匹配条件，避免列表和 CSV 各写一套 SQL。
- 包含用「不同标签名数量等于指定个数」，排除用 `NOT EXISTS`。集合条件用 `EXISTS`，避免 JOIN 占位符顺序把集合 id 和状态绑错。
- 左侧 Top tags 仍是全库用量最高的 18 个，数字不是当前匹配数。新标签往往只有 1 次，不会出现在这列。
- 分页大小定为 24。请求序号丢掉过期响应，快速改条件时只显示最后一次。

## 限制与未完成

- 语义搜索入口仍在。语义模式不套用包含、排除和集合条件。验收范围是关键词搜索。
- 没有单独的自动化用例把「某集合 + harbor + approved + campaign − internal」串成一条。包含/排除逻辑有查询实现，修改后查询的用例覆盖了包含标签。
- `f2a074b` 的 Dockerfile 多了清华 PyPI 镜像，只影响构建拉包，与检索功能无关。按题目要求没有改写该提交。
- 不要求、也未做：异步导出、跨请求快照、语义检索优化、新权限体系。
