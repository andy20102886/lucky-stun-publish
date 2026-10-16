# 固定入口那层是什么（Cloudflare Worker）

> 本仓库**不发布**这个 Worker 的源码 —— 它是各人自建的私有部署。
> 这里只写清它**必须实现什么**，你照着写一个几十行的 Worker 就行。

## 一、它解决什么

穿透打出来的 `IP:端口` 会变、且端口无法固定。Worker 的作用就一个：
**对外提供一个永不变的域名，内部永远跳到「当前那条规则的最新地址」。**

- 入口域名：`stun.example.com`（你自己的域名，挂在 Cloudflare 上）
- 当前地址存在 **Cloudflare KV** 里，由 Lucky 推送更新
- 访问者拿到的链接永远不变

## 二、KV 数据结构

| key | value |
|---|---|
| `rule:<规则名>` | 单条规则的 JSON |
| `__index` | 规则名数组，**顺序 = 最近更新在前** |

单条规则的样子：

```json
{"rule":"demo","ip":"203.0.113.10","port":"24263",
 "addr":"203.0.113.10:24263","url":"http://203.0.113.10:24263/",
 "time":"推送方报的时间","updated":"Worker 收到的时间(ISO)"}
```

KV 本身**没有删除接口的封装** —— 要删规则，直连 Cloudflare KV API 删 `rule:<名>`，并把名字从 `__index` 里摘掉（本仓库的 `unpublish` 就是这么干的）。

## 三、需要实现的路由

| 路由 | 方法 | 鉴权 | 作用 |
|---|---|---|---|
| `/api/stun`（别名 `/api/report`、`/report`） | ANY | `?key=<WEBHOOK_KEY>` | **接收穿透软件的推送**：写 KV + 顺带刷新 DDNS 域名的 A/AAAA 记录（灰云、TTL 60） |
| `/login` | POST | 口令 | 校验口令后下发 cookie（HttpOnly、Secure、30 天） |
| `/logout` | GET | — | 清 cookie |
| `/api/status` | GET | `?key=…` 或已登录 | JSON 列出全部规则 |
| `/go`、`/go/<规则名>` | GET | 公开 | **302 跳到该规则当前地址**；不带规则名 = 最近更新的那条 |
| `/p/<规则名>/...` | ANY | 公开 | **反向代理**（保持原域名）。⚠️ Worker 不能直接 fetch 纯 IP，要先解析到自己的 DDNS 域名再带端口请求 |
| `/` | GET | 口令 | 状态页（规则列表 + 三种入口按钮） |

### 推送解析要宽松

不同穿透软件的推送字段名不一样，建议兼容：

- 规则名：`rule` / `ruleName` / `name` / `onlyName` / `task` / `title` 任一
- 地址：`ip`+`port` / `ipAddr` / `addr` / `publicAddr` 任一
- body 形态：JSON / 表单 / querystring **三种都认**

### 两个容易踩的坑

- `/go` 的跳转要用 **302 + `cache-control: no-store`**。如果误用 301，浏览器会把跳转**永久缓存**，之后地址变了访问者还去旧地址。
- 反代 `/p/` 里**不能直接 fetch IP**，Cloudflare 会拒绝；要先解析到自己的域名（DDNS 那条）再请求。

## 四、需要的绑定（环境变量 / KV）

| 绑定名 | 类型 | 含义 |
|---|---|---|
| `PANEL_PASS` | 文本 | 面板登录口令 |
| `PANEL_TOKEN` | 文本 | 面板 cookie 的值（登录成功后下发，供脚本/接口复用） |
| `WEBHOOK_KEY` | 文本 | `/api/stun` 写入 与 `/api/status` 读取的钥匙 |
| `DDNS_HOST` | 文本 | 自动跟随公网 IP 的那个域名，如 `stun-ip.example.com` |
| `ZONE_ID` | 文本 | 你域名所在 zone 的 ID |
| `CF_DNS_TOKEN` | 文本 | 只带 DNS 编辑权限的 Cloudflare Token（刷新 DDNS 记录用） |
| `STUN` | KV 命名空间 | 存规则的 KV |

> 口令、钥匙、Token 一律**放在 Worker 的环境变量里**，不要写进代码、更不要进仓库。

## 五、怎么改这个 Worker（真需要时）

有 Cloudflare 主控 Token（Workers 读写权限）即可直接改：

```bash
ACC=<你的 CF Account ID>
TOK=<你的 CF API Token>

# 1) 取源码。注意：模块式 Worker 用 /content 会返 405，要用 /content/v2（返回 multipart）
curl -H "Authorization: Bearer $TOK" \
  "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/<脚本名>/content/v2" -o c.txt
# multipart 里第一段就是 worker.js，去掉边界头尾再存

# 2) 回传（ES module 格式，必须 multipart 上传）
#    metadata 里要带全部 bindings，否则绑定会被清空！
```

> ⚠️ **改 Worker 前先完整备份源码 + settings**。绑定丢失会导致面板登不上、推送写不进 KV。
