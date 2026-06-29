# Development Notes

## 目标演进

项目从一个“闲鱼无物流商品价差辅助后台”逐步收敛为“后台自动规划数字权益商品的精简操作台”。

最终主流程：

```text
后台自动选品 -> 发布队列 -> 发布助手填表 -> 人工确认发布 -> 咨询回复 -> 接单履约
```

当前产品刻意不提供：

- 手动添加商品
- 机会扫描
- 商品管理列表
- 行情采样调试面板
- 详细市场指标面板
- 完全无人值守发布

## 关键开发阶段

### 1. 初始本地 MVP

提交：`bc4e670 Initial xianyu arbitrage assistant`

建立了纯 Python 标准库版本：

- `app.py`
- SQLite 本地数据库
- 内嵌 Web UI
- 商品、行情、草稿、订单等基础表

### 2. 行情与发布辅助

提交：

- `47a8219 Add market collection helper`
- `723615e Add publish form helper`

增加了浏览器 bookmarklet：

- `/collector.js?product_id=...`
- `/publisher.js?product_id=...`

后来基础界面移除了行情采样入口，但后端脚本仍保留，供调试或未来内部自动化使用。

### 3. 机会扫描尝试与移除

提交：

- `16aedb2 Add opportunity scanner`
- `0d5071c Remove opportunity scanner`

曾经实现过手动关键词机会扫描，但用户目标变为“后台全权自动选品”，所以该功能被移除。

### 4. 履约增强

提交：`221be69 Enhance order fulfillment sourcing`

在订单履约中加入“最新低价货源”粘贴能力：

- 接单时重新搜索低价货源；
- 系统保存新候选；
- 按采购上限筛选；
- 无可用候选则生成缺货建议。

### 5. 后台自动选品

提交：

- `f26bc4c Add autopilot product publishing queue`
- `55e1914 Switch autopilot to digital goods`

先加入自动选品和发布队列，随后把候选池从人力服务类商品切换为无需人力交付的数字权益类商品。

当前候选池在 `AUTO_PRODUCT_CATALOG`。

### 6. 产品收敛

提交：

- `700cef8 Remove manual product creation`
- `0d5071c Remove opportunity scanner`
- `3a28768 Simplify main operator UI`

删除手动添加商品、机会扫描和偏调试界面。当前前端只保留日常操作所需的基础功能。

### 7. 发布助手

提交：`bc0a147 Add publish queue assistant`

加入发布队列助手：

- `/api/publish-queue/start`
- `ready -> active`
- 复制填表脚本
- 打开闲鱼入口页
- 用户人工确认发布后标记 `published`

## 当前代码入口

- 自动选品：`run_autopilot`
- 候选商品池：`AUTO_PRODUCT_CATALOG`
- 定价：`analyze_market`
- 发布填表脚本：`publisher_script`
- 发布队列开始：`start_publish_queue`
- 发布队列状态：`update_publish_queue_status`
- 咨询回复：`generate_reply`
- 接单履约：`create_order`

## 另一台电脑继续开发

```bash
git clone git@github.com:jams99/xianyu_product.git
cd xianyu_product
python3 app.py
```

打开：

```text
http://127.0.0.1:8765
```

验证：

```bash
python3 -m py_compile app.py
```

`xianyu_agent.db` 是本地运行数据，不提交到仓库。新电脑首次运行会自动创建数据库。

## 开发边界

继续开发时保持这些原则：

- 不绕过闲鱼登录、验证码、风控。
- 不自动点击最终发布。
- 不自动付款。
- 不恢复手动添加商品。
- 不恢复机会扫描作为日常入口。
- 不把自动选品改回需要人工服务交付的商品。
- 前端保持基础操作台，不做复杂管理后台。
