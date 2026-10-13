# ADR-004：关闭通配 CORS，改为 Origin/Host 校验的本地信任边界

## Status
Accepted（2026-10-07）— 修复已复现的安全缺陷

## Background
实测缺陷：`applytracker.py:6629` 对所有响应一律发 `Access-Control-Allow-Origin: *`。
后果：**任何网页零点击就能读走 / 改掉 / 删掉作者的投递记录**（已复现）。
成因链：
- `_send`（`applytracker.py:6623`）无条件加 `ACAO: *`，连 HTML 页面响应也带；
- `do_OPTIONS`（6653）对任何来源直通 `204`；
- `do_POST`（6704）只查 `Host`（`_host_ok`，6638），**不看 `Origin`** —— 写请求会被跨站页面执行（CSRF），即使响应读不到。
- 只有 GET/POST 查了 `_host_ok`，OPTIONS 完全没查。

威胁模型：**只针对网页**（恶意站点用 JS 跨站打 `127.0.0.1:8788`）。
浏览器扩展不在防御对象内 —— 用户主动装的扩展即用户信任。

## Decision
**锁定：移除通配 CORS；新增 `_origin_ok()`，对所有 `do_*` 与 OPTIONS 生效；保留 `_host_ok` 作为第二道。**

### 规则
```
_origin_ok():
  origin = headers["Origin"]
  if origin 为空/缺失:                     return True        # curl、同源导航、file:// 调试
  scheme, host = 解析 origin
  if scheme == "chrome-extension":         return True        # 用户装的扩展（信任边界外，见上）
  if host ∈ {127.0.0.1, localhost, ::1}:   return True        # 看板自身同源
  return False                                                # 其余一律拒
```
处理：
- GET（`6656`）/ POST（`6704`）进入时先判 `_host_ok()` 再判 `_origin_ok()`，任一不过 → `403`。
- `do_OPTIONS`（6653）**同样先过两道校验**，不过 → `403`（原来的 204 直通是预检放行的漏洞）。
- `_send`（`6623`）**删除 `Access-Control-Allow-Origin: *`**（6629）与 `Access-Control-Allow-Headers`（6630）。
- **不再回任何 ACAO**：扩展走 `host_permissions`（`extension/manifest.json:7`）本就绕过 CORS，同源页面不需要 ACAO，回显只会扩大攻击面。
- 跨站写请求（无论响应能否读到）在服务端被 `403` 拦下 —— 这是对 CSRF 的实质防线。

### 为什么不干脆换鉴权 token
本机 `127.0.0.1` + 单人，无账号体系（产品定义）。Origin/Host 校验对该威胁模型已足够，
且不引入「token 存哪、扩展怎么带」的新复杂度。多租户/公网场景才需要 token，本产品不做。

## Consequences
- 正面：恶意网页读/写/删全部被 `403`；不改变正常使用（看板同源、扩展走 host_permissions、命令行 curl 无 Origin）。
- 负面：若同学把服务改成绑 `0.0.0.0` 或换非回环 host，Origin 校验会拒（有意为之 —— 本就该拒）。
- 回归：新增自检 —— 伪造 `Origin: https://evil.com` 的 GET/POST/OPTIONS 必须 `403`；
  无 Origin 的 POST（模拟扩展/curl）必须放行；`chrome-extension://` Origin 必须放行。

## Related ADRs
- ADR-005（同在 `do_POST` 路径上的写入一致性加固）
