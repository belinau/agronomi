bash
#!/bin/sh
# Try Tailscale first (stable CGNAT IP, works across NAT)
TS_IP=$(tailscale status --json 2>/dev/null | jq -r '.TailscaleIPs[0]')
if [ -n "$TS_IP" ] && [ "$TS_IP" != "null" ]; then
  echo "$TS_IP"
  exit 0
fi
# Fall back to public IP
PUBLIC_IP=$(curl -s --max-time 3 ip.me 2>/dev/null)
if [ -n "$PUBLIC_IP" ]; then
  echo "$PUBLIC_IP"
  exit 0
fi
# No WAN IP — discovery announce fails gracefully, LAN/LoRa still work
exit 1
