#!/bin/sh /etc/rc.common
START=95

start() {
    nft add rule inet fw4 forward oifname home tcp flags syn tcp option maxseg size set 1380 2>/dev/null
    nft add rule inet fw4 forward iifname home tcp flags syn tcp option maxseg size set 1380 2>/dev/null
    nft add rule inet fw4 output oifname home tcp flags syn tcp option maxseg size set 1380 2>/dev/null
    nft add chain inet fw4 dstnat '{ type nat hook prerouting priority dstnat; policy accept; }' 2>/dev/null
    nft add chain inet fw4 srcnat '{ type nat hook postrouting priority srcnat; policy accept; }' 2>/dev/null
    nft insert rule inet fw4 srcnat oifname home ip saddr 192.168.50.0/24 snat ip to ip saddr & 0.0.0.255 | 10.0.0.0 2>/dev/null
    nft add rule inet fw4 dstnat iifname home ip daddr 10.0.0.0/24 dnat ip to ip daddr & 0.0.0.255 | 192.168.50.0 2>/dev/null
}
