# 公众号精选日报 · 云端自动化

电脑关机也能每天 08:00 自动把日报发到邮箱。

## 架构

```
本地电脑(开机时)                      GitHub Actions(云端, 全自动)
┌─────────────────────┐              ┌──────────────────────────┐
│ WeWe RSS 抓取公众号   │   git push   │ 每天 08:00 (北京时间)       │
│ sync_data.py 导出JSON │ ──────────→ │ digest_cloud.py 评分精选   │
│ (每 2 小时自动运行)    │   几 MB 数据  │ QQ邮箱 SMTP 发送到 Outlook │
└─────────────────────┘              │ sent_history 提交回仓库     │
                                     └──────────────────────────┘
```

- 数据文件: `data/articles_recent.json`(本地推送, 最近 5 天文章)
- 去重记录: `data/sent_history.json`(云端运行后自动提交, 45 天内不重复推荐)
- 日报归档: `output/digest_YYYY-MM-DD.html`

## Secrets 配置(仓库 Settings → Secrets and variables → Actions)

| 名称 | 说明 |
|------|------|
| SMTP_USER | 发件 QQ 邮箱, 如 123456@qq.com |
| SMTP_PASS | QQ 邮箱 SMTP 授权码(设置 → 账户 → POP3/SMTP 服务) |
| TO_EMAIL | 收件邮箱 zhanghongquan@outlook.com |

## 本地同步

修改公众号订阅请打开 WeWe RSS 管理面板: http://localhost:4000 (需先启动服务)

数据同步脚本 `sync_data.py` 配置在 `sync_config.json`(不入库):
- 自动拉起 WeWe RSS 服务(未运行时)
- 导出最近 5 天文章并推送

手动运行:
```
python sync_data.py
```

手动触发云端日报(测试): 仓库 Actions → Daily Digest → Run workflow
