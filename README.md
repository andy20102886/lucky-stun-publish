# lucky-stun-publish

**一句话：** 把任意内网服务（NAS / 路由器 / PC / 摄像头 / 任意 `IP:端口`）**临时**发布到公网，对外只给一个**永不变的固定域名入口**。一条命令上线、一条命令撤下、不留残留。

> 适用场景：临时把家里的服务给同事/客户看一眼、临时演示、临时远程访问。
> 不适用：长期对外提供服务 —— 这套是「临时通道」，用完请撤下。

---

## 一、解决的是什么问题

家庭宽带大多是 **CGNAT（没有公网 IPv4）**，端口映射注定无效；即便能映射，运营商给的 `IP:端口` 也会**随时变化**，而且**端口无法固定**。

所以「给个地址就能访问」这件事要拆成两半：

| 问题 | 本项目的解法 |
|---|---|
| 没有公网 IP，怎么打通？ | **STUN 打洞**（Lucky 内置），由运营商 NAT / 路由器 UPnP 开出临时映射 |
| 打出来的 `IP:端口` 老在变，链接没法发人 | 一层 **Cloudflare Worker 固定入口**：链接永不变，内部自动跳到当前地址 |
| 映射表项不活跃会被回收，早上起来全掉 | **外部保活 + 自动自愈**（见 §四） |

---

## 二、链路原理

```
外网访问者 ──► https://stun.example.com/go/<规则名>        ← 固定入口，永远发这一个
                        │  302 查 KV 里的最新地址
                        ▼
                <公网IP>:<临时端口>                        ← 会变，不用管
                        │  运营商 NAT / 家庭路由器 UPnP 映射（Lucky 申请 + 心跳维持）
                        ▼
                内网机上的 Lucky（内置转发）
                        ▼
                目标服务 192.168.x.x:<端口>                ← 你真正要发布的东西
```

**关键点：地址是「报」上去的，不是「查」出来的。**
Lucky 每次拿到新的 `IP:端口`，会调平台的一个 Webhook（`POST /api/stun?key=…`）把地址写进 Cloudflare KV；`/go/<规则名>` 就是拿 KV 里的最新值做 302 跳转。发布脚本**自己也会再推一次**（双保险），所以即使 Lucky 的 Webhook 没触发，固定入口也是准的。

---

## 三、安装

### 3.1 依赖

| 组件 | 作用 | 必需 |
|---|---|---|
| **Python 3** | 跑发布脚本，**只用标准库**，无需 pip 安装 | ✅ |
| **Lucky**（STUN 穿透版） | 真正打洞 + 内置转发 | ✅ |
| **一个 Cloudflare Worker 平台** | 固定入口 + 记录当前地址的 KV | ✅ |
| **UptimeRobot** | 每 5 分钟从境外真实外网戳一次端口（保活） | 建议 |
| **Cloudflare API Token** | 脚本清理平台 KV 记录时用 | 建议 |

> ⚠️ 本项目**不包含**那个 Cloudflare Worker 的源码（它是各人自建的私有部署）。它需要实现的最小接口见 [`docs/platform-worker.md`](docs/platform-worker.md)，照着那几条路由自己写一个几十行的 Worker 即可。

### 3.2 配置

凭据**不进代码库**。把 [`config.example.json`](config.example.json) 复制成本机配置文件，填上自己的值：

- 默认路径：`~/.workbuddy/stun-publish.json`
- 换路径：环境变量 `STUN_PUBLISH_CONF=/path/to/your.json`
- 想自动化引导：`STUN_PUBLISH_TEMPLATE=/path/to/your.json`，脚本发现缺配置时会自动复制过去

```bash
cp config.example.json ~/.workbuddy/stun-publish.json
# 然后编辑它，填 Lucky 地址/账号口令、平台域名与钥匙、UptimeRobot Key、CF Token
```

配置项含义见 [`config.example.json`](config.example.json) 里的注释（`_说明` 字段）。

### 3.3 一键脚本

`assets/stun_publish.py` 纯标准库，任何有 python3 的机器都能直接跑：

```bash
python assets/stun_publish.py publish   <规则名> <内网IP:端口>   # 发布（等打洞完成，约 20 秒）
python assets/stun_publish.py list                            # 看所有通道
python assets/stun_publish.py status                          # 看平台侧登记
python assets/stun_publish.py unpublish <规则名>               # 撤下 + 清理干净
python assets/stun_publish.py init-conf                       # 换机器/换口令后重建配置
```

例：

```bash
python assets/stun_publish.py publish demo 192.168.1.10:8000
# → 对外入口 https://stun.example.com/go/demo
```

---

## 四、为什么不会「第二天早上全掉线」

两层保护，跟发布是**同一套**，不用额外操作：

1. **保活**：UptimeRobot 每 5 分钟从境外节点真外网戳一次端口。NAT / 路由器的映射表项**不活跃就会被回收**，必须有外部流量来维持。
2. **自愈**：内网机上挂一个守护脚本（每 5 分钟跑一次，用 Lucky 的计划任务触发）：
   - 新规则**自动纳管**；
   - 端口漂了**自动同步**监控目标地址；
   - 探到 DOWN 就自动「禁用 → 启用」该规则**重新打洞**。

发布脚本会顺手建/同步 `STUN-<规则名>` 监控；**就算不建，守护脚本 5 分钟内也会自动接管**。所以临时发布短于 5 分钟也是安全的。

---

## 五、对外可以给的三种地址

| 形态 | 地址 | 特点 |
|---|---|---|
| **固定跳转（首选）** | `https://stun.example.com/go/<规则名>` | 302 跳到当前 `IP:端口`。**端口漂了也不用改链接**，发给别人最省事 |
| IP 直连域 | `https://stun-ip.example.com:<端口>` | 域名自动跟随公网 IP（秒级 DDNS），端口要自己带 |
| 反代 | `https://stun.example.com/p/<规则名>/` | 保持原域名、支持子路径，**别跑大流量**（CF 免费额度） |

> ⚠️ **只发固定跳转那一条**，别发 `IP:端口` —— 那是运营商 CGNAT 的共享出口，端口随时会变。
> 另外 `/p/` 反代走 Cloudflare 额度，别拿它当长期 CDN。

---

## 六、排错速查

| 现象 | 原因 / 处置 |
|---|---|
| 打洞超时（一直等不到地址） | ① **目标服务没在监听**（最常见，先在内网 `curl IP:端口` 验一下）② 内网机默认路由不是出口路由器，被本地代理/旁路由劫持，导致探测拿到假地址 ③ 运营商 NAT 类型变了 → 重试一次或换个时间 |
| 通道显示「通」，但打开报错 / Broken pipe | TCP 通 ≠ 服务可用。穿透点在本地接连接，所以 TCP 探测会通；后面目标没服务就是死路。**先起服务再发布** |
| 平台/域名请求报 `error code: 1010` | Cloudflare 拦 `python-urllib` 的 UA。脚本已伪装 Chrome UA；自己手写脚本记得带 `User-Agent` |
| 公网域名 SSL 握手超时（时好时坏） | 该域名有 AAAA 记录、本机 IPv6 路线不稳。脚本已**强制 IPv4 + 重试 3 次** |
| 端口又变了 | 正常。穿透端口**无法指定/固定**，所以对外一律发 `/go/<规则名>` |
| 面板上看不到某条规则 | 平台只在**收到过地址推送**后才有记录。用 `publish` 会主动推一次 |

---

## 七、安全红线

- 发布 = **把服务暴露到全互联网**，等于给所有人开门。**别发布没有认证的后台**；发布前确认服务自己有登录/口令。用完立刻 `unpublish`。
- 只动这三样：**穿透规则**、**UptimeRobot 监控**、**平台 KV 记录**。不要为了这套东西去改系统目录 / 系统配置。
- 撤下必须用 `unpublish`（只删穿透规则、不清监控和平台记录 → 会留残留）。
- 清理是否干净，用这三条验：`list` 里规则没了、`status` 里平台条数减 1、访问 `/go/<规则名>` 返回 **404**。
- 免费额度有限：反代走 CF 额度、UptimeRobot 免费版 50 个监控（守护脚本到 45 就停手）。

---

## 八、目录结构

```
lucky-stun-publish/
├── README.md                     ← 本文件：原理 + 安装 + 用法 + 排错
├── config.example.json           ← 配置模板（全是占位符，复制后填自己的）
├── assets/
│   └── stun_publish.py           ← 发布/撤下/巡检脚本（纯标准库）
└── docs/
    ├── platform-worker.md        ← 固定入口那层（CF Worker）的接口与 KV 结构
    └── troubleshooting.md        ← 深水区排错（含守护脚本设计要点）
```

## License

MIT —— 见 [LICENSE](LICENSE)。
