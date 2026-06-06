# WireGuard 双向隧道配置备份

## 拓扑

```
家 (R5S/aarch64) ←── WireGuard Tunnel ──→ 公司 (x86)
   office / 10.0.1.1:50000                   home / 10.0.0.1:50000
   LAN 192.168.50.0/24                       LAN 192.168.50.0/24 + 192.168.33.0/24
```

## 地址映射

| 目标 | 家访问 | 公司访问 |
|------|--------|---------|
| 公司 192.168.50.x | 10.0.0.x | 192.168.50.x |
| 公司 192.168.33.x | 192.168.33.x (直通) | 192.168.33.x |
| 家 192.168.50.x | 192.168.50.x | 10.0.1.x |

## 文件说明

- `r5s-home/` — 家侧 R5S (ARM, ImmortalWrt)
  - `network.conf` — 追加到 `/etc/config/network`
  - `wg-tunnel-init.sh` — 放入 `/etc/init.d/wg-tunnel`，`chmod +x`，`enable`
- `company-x86/` — 公司侧 x86 (ImmortalWrt)
  - `network.conf` — 追加到 `/etc/config/network`
  - `wg-tunnel-init.sh` — 放入 `/etc/init.d/wg-tunnel`，`chmod +x`，`enable`

## 部署步骤

1. 追加 `network.conf` 中对应的配置到 `/etc/config/network`
2. 拷贝 `wg-tunnel-init.sh` 到 `/etc/init.d/wg-tunnel`
3. `chmod +x /etc/init.d/wg-tunnel`
4. `/etc/init.d/wg-tunnel enable`
5. `/etc/init.d/wg-tunnel start`

## 创建日期

2026-06-06
