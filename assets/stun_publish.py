#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucky STUN 临时发布到公网 —— 一条命令把内网服务挂到公网固定入口。

  python stun_publish.py list                              # 看现有通道
  python stun_publish.py publish <规则名> <IP:端口>         # 发布（等打洞完成）
  python stun_publish.py unpublish <规则名>                 # 撤下（删规则+监控+平台记录）
  python stun_publish.py status                            # 看平台侧登记情况

依赖：仅标准库（NAS / PC 自带 python3 即可跑）。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 本机 IPv6 路线不稳：平台域名有 AAAA 记录，走 v6 会 SSL 握手超时 → 强制只用 IPv4
_orig_gai = socket.getaddrinfo


def _gai_v4(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_gai(host, port, family, type, proto, flags)
    only4 = [r for r in res if r[0] == socket.AF_INET]
    return only4 or res


socket.getaddrinfo = _gai_v4

# ---------------- 配置 ----------------
# 🚨 凭据不进代码库。真实值放本机配置文件（默认 ~/.workbuddy/stun-publish.json，
#    可用环境变量 STUN_PUBLISH_CONF 改路径）。换机器/换口令只改那个文件。
#    文件格式见仓库根的 config.example.json。
ENV_CONF = "STUN_PUBLISH_CONF"          # 可选：用环境变量指定配置文件路径
ENV_TEMPLATE = "STUN_PUBLISH_TEMPLATE"  # 可选：一份 JSON 配置模板，缺配置时自动复制
CONF_PATH = os.path.expanduser(os.environ.get(ENV_CONF) or "~/.workbuddy/stun-publish.json")

DEFAULTS = {
    "lucky": {"base": "http://192.168.1.10:16601", "user": "", "pass": ""},
    "platform": {"base": "https://stun.example.com",
                 "webhook_key": "", "panel_pass": "", "panel_token": "",
                 "ddns_host": "stun-ip.example.com"},
    "uptimerobot": {"key": ""},
    "cloudflare": {"account": "", "kv_namespace": "", "api_token": ""},
}


def bootstrap_conf():
    """配置文件缺失时的引导：优先读环境变量给的 JSON 模板，其次是同目录的 config.example.json。

    凭据不进代码库 —— 要么手改 ~/.workbuddy/stun-publish.json，
    要么设 STUN_PUBLISH_TEMPLATE 指向你自己的 JSON（内容形如 config.example.json）。
    """
    tmpl = os.environ.get(ENV_TEMPLATE, "")
    if not tmpl or not os.path.isfile(os.path.expanduser(tmpl)):
        return None
    try:
        raw = json.load(open(os.path.expanduser(tmpl), encoding="utf-8"))
    except Exception as e:
        sys.stderr.write("[!] 模板解析失败 %s: %s\n" % (tmpl, e))
        return None
    try:
        d = os.path.dirname(CONF_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        json.dump(raw, open(CONF_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        sys.stderr.write("[i] 已由模板 %s 生成配置 -> %s\n" % (tmpl, CONF_PATH))
    except Exception as e:
        sys.stderr.write("[!] 写配置失败 %s: %s\n" % (CONF_PATH, e))
        return None
    return raw


def load_conf(force=False):
    c = json.loads(json.dumps(DEFAULTS))
    if force or not os.path.isfile(CONF_PATH):
        if bootstrap_conf() is None and not os.path.isfile(CONF_PATH):
            sys.stderr.write(
                "\n[!] 找不到配置文件 %s。\n"
                "    照 config.example.json 手工建好它，或设 STUN_PUBLISH_TEMPLATE 指向你的模板。\n\n" % CONF_PATH)
            sys.exit(9)
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for sec, kv in user.items():
            if sec.startswith("_"):
                continue
            if isinstance(kv, dict):
                c.setdefault(sec, {}).update(kv)
            else:
                c[sec] = kv
    except Exception as e:
        sys.stderr.write("\n[!] 配置读取失败 %s: %s\n\n" % (CONF_PATH, e))
        sys.exit(9)
    return c


# -h/--help 不该被「缺配置」挡住：只有真正要干活的命令才强制加载配置
if any(x in sys.argv[1:] for x in ("-h", "--help", "-help")):
    CONF = json.loads(json.dumps(DEFAULTS))
else:
    CONF = load_conf()
LUCKY = CONF["lucky"]["base"]
LUSER, LPASS = CONF["lucky"]["user"], CONF["lucky"]["pass"]

PLATFORM = CONF["platform"]["base"]
WEBHOOK_KEY = CONF["platform"]["webhook_key"]       # /api/stun 、/api/status 的钥匙

URKEY = CONF["uptimerobot"]["key"]                  # UptimeRobot（保活）
UR_PREFIX = "STUN-"

CF_TOKEN = CONF["cloudflare"]["api_token"]
CF_ACC = CONF["cloudflare"]["account"]
CF_KV_NS = CONF["cloudflare"]["kv_namespace"]       # Worker stun-hop 的 KV（存规则地址）


# --------------------------------- 通用 -------------------------------------
def http(url, data=None, method=None, headers=None, timeout=30, raw=None):
    hdr = {"Content-Type": "application/json",
           # Cloudflare 会拦 python-urllib 的 UA（报 1010），必须伪装成浏览器
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
    hdr.update(headers or {})
    if raw is not None:
        body = raw
    elif data is not None:
        body = json.dumps(data).encode()
    else:
        body = None
    last = None
    for attempt in range(3):                 # 公网链路偶发抖动，重试 3 次
        req = urllib.request.Request(url, data=body, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.5)
    return -1, last


def jget(url, headers=None, timeout=30):
    st, raw = http(url, headers=headers, timeout=timeout)
    try:
        return json.loads(raw)
    except Exception:
        return {"_http": st, "_raw": raw[:400]}


# --------------------------------- Lucky ------------------------------------
def _sig():
    ms = str(int(time.time() * 1000))
    b = ms[:-1]
    return b + str(sum(int(c) for c in b) % 8)


_TOK = {"v": None}


def lucky(path, method="GET", data=None, timeout=30):
    """带签名 + 登录态的 Lucky API 调用"""
    if _TOK["v"] is None:
        st, raw = http(LUCKY + "/api/login?_=" + _sig(),
                       data={"account": LUSER, "password": LPASS}, method="POST")
        _TOK["v"] = json.loads(raw)["token"]
    url = LUCKY + path + ("&" if "?" in path else "?") + "_=" + _sig()
    st, raw = http(url, data=data, method=method,
                   headers={"Lucky-Admin-Token": _TOK["v"]}, timeout=timeout)
    try:
        return st, json.loads(raw)
    except Exception:
        return st, {"_raw": raw[:400]}


def rules():
    st, j = lucky("/api/stunrulelist")
    out = []
    for r in (j.get("list") or []):
        addr = r.get("PublicAddr") or ""
        out.append({"name": r.get("Name"), "key": r.get("Key"),
                    "addr": addr, "port": addr.split(":")[-1] if ":" in addr else "",
                    "enable": bool(r.get("Enable")),
                    "target": r.get("TargetAddrList") or r.get("TargetAddr")})
    return out


def find_rule(name):
    for r in rules():
        if r["name"] == name:
            return r
    return None


def create_rule(name, ip, port):
    """以现有规则为模板克隆一条新规则（保证字段合法）"""
    cur = rules()
    if cur:
        st, j = lucky("/api/stun/" + cur[0]["key"])
        tpl = j.get("rule") or {}
    else:
        tpl = {}
    if not tpl:
        raise RuntimeError("Lucky 里没有任何规则可作模板")

    tpl.pop("Key", None)
    tpl["Name"] = name
    tpl["Enable"] = True
    tpl["TargetAddressList"] = [ip]
    tpl["TargetPort"] = int(port)
    tpl["GlobalWebhook"] = True        # 跟随端口变化，自动推给平台
    tpl["WebhookEnable"] = False
    tpl["RetryCount"] = 2              # 推送失败重试，别用 0
    tpl["ListenPort"] = 0              # 0 = 让 Lucky 自己挑本地端口
    tpl["UPnP"] = True
    tpl["NatPMP"] = False
    tpl["DisablePortForward"] = False  # False = 启用内置转发
    tpl["DisableStunAvalidCheck"] = False
    if "Options" in tpl:
        tpl["Options"]["DisableSelfForwardingCheck"] = False

    st, j = lucky("/api/stunrule", method="POST", data=tpl)
    if j.get("ret") not in (0, None) or st not in (200,):
        raise RuntimeError("建规则失败 http=%s resp=%s" % (st, str(j)[:300]))
    return j


def delete_rule(key):
    return lucky("/api/stunrule?key=" + key, method="DELETE")


# --------------------------------- 平台（stun-hop） --------------------------
def push_platform(rule_name, ip, port):
    """主动把 ip:port 登记到固定入口平台（Lucky 的全局 Webhook 是备份）"""
    body = json.dumps({"ruleName": rule_name, "ip": ip, "port": str(port),
                       "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
    st, raw = http(PLATFORM + "/api/stun?key=" + WEBHOOK_KEY, raw=body, method="POST")
    try:
        return json.loads(raw)
    except Exception:
        return {"_http": st, "_raw": raw[:300]}


def platform_status():
    return jget(PLATFORM + "/api/status?key=" + WEBHOOK_KEY)


def platform_purge(rule_name):
    """从平台 KV 里删掉这条规则（平台本身没有删除接口，直接改 KV）"""
    base = "https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces/%s/values/" % (
        CF_ACC, CF_KV_NS)
    h = {"Authorization": "Bearer " + CF_TOKEN}
    idx = jget(base + "__index", headers=h)
    if isinstance(idx, list) and rule_name in idx:
        idx = [x for x in idx if x != rule_name]
        st, _ = http(base + "__index", method="PUT", raw=json.dumps(idx).encode(),
                     headers={"Authorization": "Bearer " + CF_TOKEN,
                              "Content-Type": "application/json"})
    st, _ = http(base + urllib.parse.quote("rule:" + rule_name, safe=""),
                 method="DELETE", headers=h)
    return st in (200, 404)


# --------------------------------- UptimeRobot ------------------------------
def ur_post(path, data):
    st, raw = http("https://api.uptimerobot.com/v2/" + path,
                   raw=urllib.parse.urlencode(data).encode(),
                   headers={"Content-Type": "application/x-www-form-urlencoded",
                            "Cache-Control": "no-cache"}, timeout=30)
    try:
        return json.loads(raw)
    except Exception:
        return {"stat": "fail", "_http": st, "_raw": raw[:200]}


def ur_monitors():
    return ur_post("getMonitors", {"api_key": URKEY, "format": "json", "limit": 50}).get("monitors") or []


def ur_ensure_monitor(rule_name, ip, port):
    """保活：没监控就建，端口变了就同步（同名去重，不会重复建）"""
    want = UR_PREFIX + rule_name
    for m in ur_monitors():
        if m.get("friendly_name") == want:
            if str(m.get("port")) != str(port) or m.get("url") != ip:
                ur_post("editMonitor", {"api_key": URKEY, "format": "json",
                                        "id": m["id"], "url": ip, "port": port})
                return m["id"], "synced"
            return m["id"], "exists"
    time.sleep(1)
    j = ur_post("newMonitor", {"api_key": URKEY, "format": "json",
                               "friendly_name": want, "type": 4,
                               "url": ip, "port": port, "interval": 300})
    if j.get("stat") == "ok":
        return j["monitor"]["id"], "created"
    return None, "failed: %s" % (j.get("error") or j)


def ur_del_monitor(rule_name):
    n = 0
    for m in ur_monitors():
        if m.get("friendly_name") == UR_PREFIX + rule_name:
            ur_post("deleteMonitor", {"api_key": URKEY, "format": "json", "id": m["id"]})
            n += 1
    return n


# --------------------------------- 命令 -------------------------------------
def cmd_init_conf(_):
    """重建本机凭据配置 ~/.workbuddy/stun-publish.json（换机器/换口令后用）"""
    c = load_conf(force=True)
    print("✓ 配置已就绪：%s" % CONF_PATH)
    print("  Lucky      : %s（用户 %s）" % (c["lucky"]["base"], c["lucky"]["user"] or "(空)"))
    print("  平台       : %s（钥匙 %s）" % (c["platform"]["base"],
                                       "有" if c["platform"]["webhook_key"] else "缺"))
    print("  UptimeRobot: %s" % ("有" if c["uptimerobot"]["key"] else "缺"))
    print("  Cloudflare : %s（KV %s）" % ("有" if c["cloudflare"]["api_token"] else "缺",
                                       c["cloudflare"]["kv_namespace"] or "(空)"))
    return 0


def cmd_list(_):
    rs = rules()
    print("Lucky 通道 %d 条：" % len(rs))
    print("%-12s %-24s %-8s %s" % ("规则名", "公网地址", "启用", "目标"))
    for r in rs:
        print("%-12s %-24s %-8s %s" % (r["name"], r["addr"], r["enable"], r["target"]))
    p = platform_status()
    print("\n平台登记 %s 条（%s）" % (p.get("count"), p.get("ddns_host")))


def cmd_status(_):
    p = platform_status()
    if not p.get("success"):
        print("平台查询失败：%s" % p)
        return 1
    print("入口域名：%s" % p.get("base"))
    print("IP 直连域：%s:<端口>（自动跟随公网 IP）" % p.get("ddns_host"))
    print("%-12s %-24s %s" % ("规则名", "当前地址", "更新时间"))
    for r in p.get("rules") or []:
        print("%-12s %-24s %s" % (r["rule"], r["addr"], r["updated"]))


def cmd_publish(a):
    ip, _, port = a.target.partition(":")
    if not ip or not port.isdigit():
        print("目标格式应为 IP:端口，例如 192.168.1.10:13006")
        return 2

    old = find_rule(a.name)
    if old:
        print("同名规则已存在（%s，%s），先撤下…" % (a.name, old["addr"]))
        delete_rule(old["key"])
        time.sleep(3)

    print("[1/5] 在 Lucky 建规则 %s -> %s:%s" % (a.name, ip, port))
    create_rule(a.name, ip, port)

    print("[2/5] 等待 STUN 打洞（最长 %ds）…" % a.wait)
    t0, got = time.time(), None
    while time.time() - t0 < a.wait:
        time.sleep(4)
        r = find_rule(a.name)
        if r and r["addr"]:
            got = r
            break
    if not got:
        print("   ✗ 超时未拿到公网地址。常见原因：目标服务没在监听 / 出口路由器不是 .2 / NAT 类型不达标。")
        print("   规则已建好，稍后可再看：python %s list" % sys.argv[0])
        return 3
    pub_ip, pub_port = got["addr"].rsplit(":", 1)
    print("   ✓ 公网地址 = %s" % got["addr"])

    print("[3/5] 登记到固定入口平台")
    r = push_platform(a.name, pub_ip, pub_port)
    print("   %s" % ("✓ changed=%s" % r.get("changed") if r.get("success") else "✗ %s" % r))

    if a.no_monitor:
        print("[4/5] 跳过 UptimeRobot（NAS 守护脚本 5 分钟内会自动纳管保活）")
    else:
        print("[4/5] 建/同步 UptimeRobot 保活监控")
        mid, how = ur_ensure_monitor(a.name, pub_ip, pub_port)
        print("   %s (%s)" % (mid, how))

    print("[5/5] 完成。对外可用地址：")
    print("   固定跳转（推荐，端口漂了也不怕）： %s/go/%s" % (PLATFORM, urllib.parse.quote(a.name)))
    print("   IP 直连（域名跟随 IP）：        https://stun-ip.example.com:%s" % pub_port)
    print("   反代（保持域名，别跑大流量）：   %s/p/%s/" % (PLATFORM, urllib.parse.quote(a.name)))
    return 0


def cmd_unpublish(a):
    r = find_rule(a.name)
    if r:
        delete_rule(r["key"])
        print("✓ Lucky 规则已删除：%s（%s）" % (a.name, r["addr"]))
    else:
        print("· Lucky 里没有名为 %s 的规则" % a.name)
    n = ur_del_monitor(a.name)
    print("· UptimeRobot 监控删除 %d 个" % n)
    if a.keep_platform:
        print("· 平台记录保留（面板上会显示为过期地址）")
    else:
        print("· 平台记录清除：%s" % ("✓" if platform_purge(a.name) else "✗"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Lucky STUN 临时发布到公网")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出 Lucky 通道 + 平台登记数").set_defaults(func=cmd_list)

    s = sub.add_parser("publish", help="发布一条内网服务到公网")
    s.add_argument("name", help="规则名（会出现在固定入口 URL 里，建议英文）")
    s.add_argument("target", help="内网目标 IP:端口，如 192.168.1.10:13006")
    s.add_argument("--wait", type=int, default=60, help="等待打洞秒数，默认 60")
    s.add_argument("--no-monitor", action="store_true", help="不建 UptimeRobot 监控")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("unpublish", help="撤下一条通道")
    s.add_argument("name")
    s.add_argument("--keep-platform", action="store_true", help="保留平台登记记录")
    s.set_defaults(func=cmd_unpublish)

    sub.add_parser("status", help="平台侧当前登记").set_defaults(func=cmd_status)

    sub.add_parser("init-conf", help="重建本机凭据配置（缺配置时也会自动引导），"
                                     "并打印自检结果").set_defaults(func=cmd_init_conf)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
