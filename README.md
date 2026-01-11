# AnyRouter Proxy 一键部署

Claude Code 透明代理工具，支持多上游容错、多 API Key 轮询。

## 快速使用

```bash
# 一键部署（自动配置环境、检测代理、生成配置）
~/anyrouter_proxy/deploy.sh

# 启动 Claude Code
source ~/.bashrc
claude
```

## 从 GitHub 一键部署

```bash
git clone https://github.com/Aaroen/anyrouter_proxy.git ~/anyrouter_proxy
# 建议把敏感信息放在 .secrets（不会被 Git 跟踪）
cp ~/anyrouter_proxy/.env.example ~/anyrouter_proxy/.env
cp ~/anyrouter_proxy/.secrets.example ~/anyrouter_proxy/.secrets
chmod 600 ~/anyrouter_proxy/.secrets ~/anyrouter_proxy/.env
# 手动编辑 ~/anyrouter_proxy/.secrets，填入正确的 API_KEYS / CANDIDATE_URLS
~/anyrouter_proxy/deploy.sh
```

## 参数说明

| 环境变量 | 说明 |
|----------|------|
| `API_KEYS` | API Keys，逗号分隔 |
| `CANDIDATE_URLS` | 上游 URL 列表，逗号分隔 |
| `CLASH_PROXY_ADDR` | Clash 代理地址 |

安全建议：
- 永远不要把真实 `API_KEYS` 写进可提交文件；推荐仅写入 `~/anyrouter_proxy/.secrets`（已在 `.gitignore` 中忽略）。
- 运行时统计/冷静期状态会以 key 的 hash 标识落盘（不保存明文 key）。

## 文件结构

```
~/anyrouter_proxy/
├── deploy.sh           # 一键部署脚本
├── app.py              # FastAPI 代理服务
├── strict_wrapper.py   # Claude Code 包装器
├── .env                # 环境配置
├── requirements.txt    # Python 依赖
├── env/                # 配置目录
└── logs/               # 日志目录
```

## 功能特性

- ✅ 多上游 URL 自动容错切换
- ✅ 多 API Key 负载均衡轮询
- ✅ Clash 代理自动检测与优选
- ✅ WAF 挑战自动绕过
- ✅ 72 小时冷静期机制

## 部署到远程服务器

```bash
# 1. 传输文件
rsync -avz -e "ssh -p PORT" ~/anyrouter_proxy/ user@host:~/anyrouter_proxy/

# 2. 远程执行
ssh -p PORT user@host "~/anyrouter_proxy/deploy.sh"
```
