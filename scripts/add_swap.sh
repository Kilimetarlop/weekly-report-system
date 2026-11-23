#!/bin/sh
# 1 GB 内存的服务器必须先加交换空间，否则生成 PDF（LibreOffice）时可能内存不足。
# 用法（只需执行一次，重启后仍有效）：sudo sh scripts/add_swap.sh
set -eu
if swapon --show | grep -q .; then
  echo '已有交换空间，无需重复添加：'
  swapon --show
  exit 0
fi
fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
# 优先用内存，交换空间只在不够时兜底
sysctl -w vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
echo '已添加 2 GB 交换空间：'
swapon --show
free -h
