# 插件市场

## 索引格式

官方索引仓库为 `mohobot-plugin-index`，入口 `index.json`：

```json
{
  "$meta": {"schema_version": 1, "name": "mohobot-plugin-index", "updated_at": "2026-11-21"},
  "plugins": {
    "CarefreeSongs712/mohobot-plugin-demo": {
      "author": "CarefreeSongs712",
      "name": "demo",
      "display_name": "演示插件",
      "version": "1.0.0",
      "description": "一句话说明",
      "repo": "https://github.com/CarefreeSongs712/mohobot-plugin-demo",
      "download_url": "https://github.com/CarefreeSongs712/mohobot-plugin-demo/archive/refs/tags/v1.0.0.zip",
      "sha256": "<64 位十六进制>",
      "api_version": 1,
      "mohobot_version": "",
      "license": "MIT",
      "tags": ["demo"],
      "updated_at": "2026-11-21"
    }
  }
}
```

`plugins` 的键必须等于 `author/name`；`sha256` 必填；`repo` 只允许 GitHub HTTPS。

## 来源

- 官方源固定为 `https://raw.githubusercontent.com/CarefreeSongs712/mohobot-plugin-index/main/index.json`，只读。
- 自定义源保存在 `data/plugin_registries.json`，可增删，禁止覆盖或删除官方源。
- 索引请求优先走 `https://v4.gh-proxy.org/` 代理；代理只用于 raw、归档与 git，不用于 GitHub API。
- 索引缓存写入 `data/cache/plugin_market/`，远端失败时回退最近缓存。

## 已安装优先级

`data/plugins/` 中的插件优先于主仓库 `plugins/` 中的同名内置副本：市场安装同名的外置插件后，运行时使用已安装版本，内置副本不再加载。系统插件不应通过市场安装同名仓库。

## 安装事务

1. 下载归档到临时目录，限制归档 50 MiB、解压 100 MiB；
2. 拒绝绝对路径、`..`、符号链接与超限解压；
3. 校验 `metadata.yaml`、名称、API 版本与 SHA256；
4. 目标目录已存在则拒绝安装；
5. 解压到 `data/plugins/{name}`，写入 `data/plugin_sources.json`；
6. 重载插件系统。

市场安装记录包含 `install_method=market`、`registry_id`、`plugin_id`、`repo`、`download_url`、`version`、`installed_at`。非市场安装不允许按市场源更新。

## 更新

`POST /api/plugins/market/update` 只允许 `install_method=market` 的插件。更新包校验名称与 SHA256 后，先把旧目录复制到临时备份，再替换；文件操作失败时恢复旧目录。重载失败不会自动回滚，需在面板手动重载或重新安装。

## 删除

`DELETE /api/plugins/{name}` 把插件目录移入 `data/plugin_recycle/`，可选删除 `data/plugins_config/{name}.json` 与 `data/plugins_data/{name}/`。内置插件不允许通过该接口删除。

## 面板接口

- `GET /api/plugins/market?refresh=true`
- `POST /api/plugins/market/refresh`
- `POST /api/plugins/market/install` `{plugin_id, source_id}`
- `POST /api/plugins/market/update` `{plugin_id, source_id}`
- `POST /api/plugins/{name}/reload`
- `DELETE /api/plugins/{name}?delete_config=&delete_data=`
- `GET/POST /api/plugin-sources`
- `DELETE /api/plugin-sources/{id}`

## 外部仓库发布

每个插件一个公开仓库，仓库名 `mohobot-plugin-<slug>`。发布前补齐 `metadata.yaml`、README、LICENSE、测试与 CI。CI 检出主仓库并设置 `PYTHONPATH` 后运行插件测试；不要依赖 `pip install mohobot`（主仓库未提供可安装包）。

索引更新步骤：

1. 在插件仓库打 tag，生成 `https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip`；
2. 计算归档 SHA256；
3. 更新 `mohobot-plugin-index/index.json` 的 `version`、`download_url`、`sha256`、`updated_at`；
4. 提交并推送索引仓库。
