#!/bin/sh /etc/rc.common
START=95

start() {
    # MSS clamping
    nft add rule inet fw4 forward oifname office tcp flags syn tcp option maxseg size set 1380 2>/dev/null
    nft add rule inet fw4 forward iifname office tcp flags syn tcp option maxseg size set 1380 2>/dev/null
    nft add rule inet fw4 output oifname office tcp flags syn tcp option maxseg size set 1380 2>/dev/null

    # NAT chains
    nft add chain inet fw4 dstnat '{ type nat hook prerouting priority dstnat; policy accept; }' 2>/dev/null
    nft add chain inet fw4 srcnat '{ type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null

    # SNAT: home LAN → tunnel (192.168.50.x → 10.0.1.x)
    nft insert rule inet fw4 srcnat oifname office ip saddr 192.168.50.0/24 snat ip to ip saddr & 0.0.0.255 | 10.0.1.0 2>/dev/null

    # DNAT: tunnel → home LAN (10.0.1.x → 192.168.50.x)
    nft add rule inet fw4 dstnat iifname office ip daddr 10.0.1.0/24 dnat ip to ip daddr & 0.0.0.255 | 192.168.50.0 2>/dev/null
}
