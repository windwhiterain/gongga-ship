#!/usr/bin/env python3
"""Locate where latency is really lost, then shape it in front of the bottleneck.

The unit of measure here is not "an average". Every command ends with a
distribution and a specific hop, process or host to blame.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

IS_WINDOWS = platform.system() == "Windows"
SKILLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- talking to machines -------------------------------------------------------


class Failure(Exception):
    pass


def run(argv, timeout=120, stdin_text=None):
    try:
        done = subprocess.run(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise Failure("no such command: %s" % argv[0])
    except subprocess.TimeoutExpired:
        raise Failure("timed out after %ds: %s" % (timeout, " ".join(argv[:3])))
    return done.returncode, done.stdout or "", done.stderr or ""


def to_powershell(script, bom=True):
    """Write a script out with no quoting in between us and PowerShell.

    bom=True  -> for `powershell.exe -File` (PS 5.1 reads BOM-less .ps1 as ANSI
                 and mangles non-ASCII text).
    bom=False -> for transports that send the *text*, like `remote run --file`,
                 which cat the file to the far end. A BOM would then arrive as a
                 literal U+FEFF character and PowerShell rejects the script with
                 "The term '\ufeff' is not recognized".
    """
    script = script.replace("\r\n", "\n").replace("\n", "\r\n")
    handle, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(handle, "wb") as stream:
        stream.write((b"\xef\xbb\xbf" if bom else b"") + script.encode("utf-8"))
    return path


def local_powershell(script, timeout=180):
    path = to_powershell(script)
    try:
        code, out, err = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return code, out, err


def remote_target(name):
    """Return (kind, detail) for a remembered target, so we know how to reach it."""
    path = os.path.expanduser("~/.remote/targets.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            targets = json.load(stream)
    except (OSError, ValueError):
        return None
    return targets.get(name)


def peer_powershell(name, script, timeout=240):
    """Run a PowerShell script on another machine and bring back stdout."""
    if remote_target(name):
        remote = os.path.join(SKILLS, "remote", "remote")
        if os.path.exists(remote):
            path = to_powershell(script, bom=False)
            try:
                code, out, err = run(["bash", remote, "run", name, "--file", path,
                                      "--shell", "powershell", "--direct"], timeout=timeout)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if code != 0 and not out.strip():
                raise Failure("remote %s failed: %s" % (name, (err or out).strip()[:300]))
            return out
    # No remembered target (or no remote tool): plain ssh, closed stdin.
    path = to_powershell(script)
    try:
        code, out, err = run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                              name, "powershell -NoProfile -ExecutionPolicy Bypass -File -"],
                             timeout=timeout, stdin_text=script)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if code != 0 and not out.strip():
        raise Failure("ssh %s failed: %s" % (name, (err or out).strip()[:300]))
    return out


def ps_json(script):
    """Ask PowerShell for JSON and parse it. Nothing is parsed from human text."""
    wrapper = "$ErrorActionPreference='SilentlyContinue'\n" + script + "\n| ConvertTo-Json -Depth 6 -Compress\n"
    code, out, err = local_powershell(wrapper)
    return parse_json_output(out, err)


def parse_json_output(out, err):
    text = out.strip()
    if not text:
        raise Failure("no output from PowerShell%s" % ((": " + err.strip()[:300]) if err.strip() else ""))
    # Take the last JSON-looking line: PowerShell may emit warnings before it.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise Failure("PowerShell did not return JSON: %s" % text[:300])


# --- shared probes (PowerShell side) ------------------------------------------

PING_HELPER = r"""
$script:ping = New-Object System.Net.NetworkInformation.Ping
function Send-Ping {
    param([string]$Target, [int]$Timeout = 1000, [int]$Ttl = 128)
    try {
        $buffer = New-Object byte[] 32
        $options = New-Object System.Net.NetworkInformation.PingOptions($Ttl, $true)
        $reply = $script:ping.Send($Target, $Timeout, $buffer, $options)
        if ($reply.Status -eq 'Success') { return [int]$reply.RoundtripTime }
        if ($reply.Status -eq 'TtlExpired') { return -2 }   # a router answered
        return -1
    } catch { return -1 }
}
"""

UPLINK_HELPER = r"""
function Get-UplinkNic {
    # Tunnels and virtual switches also report 'Up' and can be faster than the
    # real link, so prefer the adapter that actually holds a default gateway.
    $routed = Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway -and $_.InterfaceAlias -notmatch '^(et_|vEthernet|Loopback|Tunnel|Teredo)' } |
        Select-Object -First 1
    if ($routed) { return $routed.InterfaceAlias }
    $fallback = Get-NetAdapter |
        Where-Object { $_.Status -eq 'Up' -and $_.Name -notmatch '^(et_|vEthernet|Loopback|Tunnel)' } |
        Sort-Object LinkSpeed -Descending | Select-Object -First 1
    if ($fallback) { return $fallback.Name }
    return $null
}
"""

MIHOMO_CONNECTIONS = r"""
function Get-MihomoConnections {
    $paths = @(
        (Join-Path $env:USERPROFILE '.kualemon\mihomo\config.yaml'),
        (Join-Path $env:USERPROFILE '.config\mihomo\config.yaml'),
        (Join-Path $env:USERPROFILE '.config\clash\config.yaml')
    )
    $secret = ''
    foreach ($p in $paths) {
        if (Test-Path $p) {
            $text = Get-Content $p -Raw
            if ($text -match '(?m)^secret:\s*"?([^"\r\n#]+)"?') { $secret = $Matches[1].Trim() }
            break
        }
    }
    $headers = @{}
    if ($secret) { $headers['Authorization'] = "Bearer $secret" }
    foreach ($port in 9090, 9091, 9097) {
        try {
            $reply = Invoke-RestMethod -Uri "http://127.0.0.1:$port/connections" -Headers $headers -TimeoutSec 6 -ErrorAction Stop
            return $reply
        } catch { }
    }
    return $null
}
"""


# --- doctor: is it the proxy? -------------------------------------------------


DOCTOR_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$result = [ordered]@{}

# Default route: which adapter actually carries traffic right now.
$routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric | Select-Object -First 1
$result.default_route = if ($routes) {
    [ordered]@{ if_index = $routes.ifIndex; gateway = $routes.NextHop; metric = $routes.RouteMetric }
} else { $null }

if ($routes) {
    $nic = Get-NetAdapter -InterfaceIndex $routes.ifIndex -ErrorAction SilentlyContinue
    $result.default_adapter = if ($nic) {
        [ordered]@{ name = $nic.Name; description = $nic.InterfaceDescription; speed = $nic.LinkSpeed }
    } else { $null }
}

# Every adapter that looks like a tunnel, plus whether it carries /0.
$tunnels = @()
foreach ($a in Get-NetAdapter -ErrorAction SilentlyContinue) {
    $isTunnel = ($a.Name -match '^(et_|tun|Tunnel|vEthernet|utun|wg)') -or
                ($a.InterfaceDescription -match 'Tunnel|TAP|WireGuard|EasyTier|Clash|Mihomo')
    if (-not $isTunnel) { continue }
    $ips = @()
    foreach ($addr in Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue) {
        $ips += $addr.IPAddress
    }
    $carries = @(Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue).Count -gt 0
    $tunnels += [ordered]@{ name = $a.Name; status = "$($a.Status)"; addresses = $ips; carries_default = $carries }
}
$result.tunnels = $tunnels

# Local proxy listeners.
$listeners = @()
foreach ($port in 7890, 7891, 7897, 1080, 10808, 8889) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $listeners += [ordered]@{ port = $port; process = "$($proc.ProcessName)"; pid = $conn.OwningProcess }
    }
}
$result.proxy_listeners = $listeners

# Where the target process's sockets actually go.
if ($env:LATENCY_APP) {
    $procs = @(Get-Process -Name $env:LATENCY_APP -ErrorAction SilentlyContinue)
    $conns = @()
    foreach ($p in $procs) {
        foreach ($c in Get-NetTCPConnection -OwningProcess $p.Id -ErrorAction SilentlyContinue) {
            if ($c.State -ne 'Established') { continue }
            $conns += [ordered]@{
                remote = "$($c.RemoteAddress):$($c.RemotePort)"
                local  = "$($c.LocalAddress):$($c.LocalPort)"
                remote_ip = "$($c.RemoteAddress)"
            }
        }
    }
    $result.app = [ordered]@{ name = $env:LATENCY_APP; processes = $procs.Count; connections = $conns }
}

$result | ConvertTo-Json -Depth 6 -Compress
"""


def cmd_doctor(args):
    """Answer one question: is a proxy or tunnel sitting in this flow's path?"""
    # The app name is injected into the script text: passing it through the
    # environment would need run() to forward env, which it does not.
    script = DOCTOR_SCRIPT
    if args.app:
        script = script.replace("if ($env:LATENCY_APP) {",
                                "if ($true) {").replace("$env:LATENCY_APP", '"%s"' % args.app.replace('"', '`"'))
    else:
        script = script.replace("if ($env:LATENCY_APP) {", "if ($false) {")
    path = to_powershell(script)
    try:
        code, out, err = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], timeout=120)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    text = out.strip()
    if not text:
        raise Failure("doctor produced nothing%s" % ((": " + err.strip()[:300]) if err.strip() else ""))
    data = None
    for line in reversed(text.splitlines()):
        if line.strip().startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if data is None:
        raise Failure("doctor did not return JSON: %s" % text[:300])

    out_lines = []
    add = out_lines.append
    add("===== default route =====")
    route = data.get("default_route")
    adapter = data.get("default_adapter")
    if route and adapter:
        add("  %s  ->  gateway %s   (%s, %s)" % (adapter.get("name"), route.get("gateway"),
                                                 adapter.get("description"), adapter.get("speed")))
    elif route:
        add("  gateway %s (ifIndex %s)" % (route.get("gateway"), route.get("if_index")))
    else:
        add("  no default route")

    add("")
    add("===== tunnel-like adapters =====")
    tunnels = as_list(data.get("tunnels"))
    capturing = [t for t in tunnels if t.get("carries_default")]
    if not tunnels:
        add("  none")
    for t in tunnels:
        mark = "  <== carries 0.0.0.0/0" if t.get("carries_default") else ""
        add("  %-18s %-10s %s%s" % (t.get("name"), t.get("status"),
                                    ",".join(t.get("addresses") or ["-"]), mark))

    add("")
    add("===== local proxy listeners =====")
    listeners = as_list(data.get("proxy_listeners"))
    if not listeners:
        add("  none listening on common proxy ports")
    for item in listeners:
        add("  %-6s %s (pid %s)" % (item.get("port"), item.get("process"), item.get("pid")))

    app = data.get("app")
    if app:
        add("")
        add("===== %s =====" % app.get("name"))
        conns = as_list(app.get("connections"))
        if not app.get("processes"):
            add("  not running")
        else:
            add("  %d process(es), %d established TCP connection(s)" % (app.get("processes"), len(conns)))
            proxy_ports = {str(i.get("port")) for i in listeners}
            via_proxy = 0
            for c in conns:
                remote = c.get("remote", "")
                local = c.get("local", "")
                port = remote.rsplit(":", 1)[-1]
                note = ""
                if remote.startswith("127.0.0.1:") and port in proxy_ports:
                    note = "  (to local proxy)"
                    via_proxy += 1
                add("    %-24s from %-24s%s" % (remote, local, note))
            add("")
            if via_proxy:
                add("  %d of %d connections go to a local proxy port." % (via_proxy, len(conns)))
            else:
                add("  none of these connections go to a local proxy port.")

    add("")
    add("===== verdict =====")
    if capturing:
        add("  A tunnel holds the default route: traffic really is being captured")
        add("  (%s). If the app also talks to 127.0.0.1 above, its packets are")
        add("  proxied -- start looking at the proxy, not the wire." % capturing[0].get("name"))
    else:
        add("  No tunnel holds the default route, so this traffic is NOT being captured")
        add("  by a TUN. A local proxy only affects apps that explicitly connect to it")
        add("  (e.g. HTTPS_PROXY, or a socket to 127.0.0.1 as listed above).")
    print("\n".join(out_lines))
    return 0


# --- hops: which hop starts losing? -------------------------------------------

HOPS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
__PING_HELPER__
$target = '__TARGET__'
$maxTtl = __MAXTTL__
$count = __COUNT__
$timeout = __TIMEOUT__

# Walk the path with rising TTL. Structured replies, so no traceroute text is
# ever parsed -- that output is localised and differs between Windows builds.
$path = @()
for ($ttl = 1; $ttl -le $maxTtl; $ttl++) {
    $buffer = New-Object byte[] 32
    $options = New-Object System.Net.NetworkInformation.PingOptions($ttl, $true)
    $reply = $script:ping.Send($target, 1200, $buffer, $options)
    $address = if ($reply.Address) { "$($reply.Address)" } else { '' }
    $reached = ($reply.Status -eq 'Success')
    $path += [ordered]@{ ttl = $ttl; address = $address; reached = $reached }
    if ($reached) { break }
}

# Then measure every hop that answered.
$rows = @()
foreach ($hop in $path) {
    if (-not $hop.address) {
        $rows += [ordered]@{ ttl = $hop.ttl; address = ''; rtts = @(); reached = $hop.reached }
        continue
    }
    $rtts = @()
    for ($i = 0; $i -lt $count; $i++) {
        $rtts += (Send-Ping $hop.address $timeout 128)
        Start-Sleep -Milliseconds 60
    }
    $rows += [ordered]@{ ttl = $hop.ttl; address = $hop.address; rtts = $rtts; reached = $hop.reached }
}
@($rows) | ConvertTo-Json -Depth 6 -Compress
"""


def distribution(values, threshold):
    """min/avg/p95/max plus the share above a threshold. Numbers, not adjectives."""
    good = [v for v in values if v >= 0]
    if not good:
        return None
    ordered = sorted(good)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "n": len(values),
        "ok": len(good),
        "min": min(good),
        "avg": sum(good) / float(len(good)),
        "p95": ordered[index],
        "max": max(good),
        "over": sum(1 for v in good if v > threshold),
        "lost": len(values) - len(good),
    }


def cmd_hops(args):
    script = (HOPS_SCRIPT
              .replace("__PING_HELPER__", PING_HELPER)
              .replace("__TARGET__", args.dest)
              .replace("__MAXTTL__", str(args.max_ttl))
              .replace("__COUNT__", str(args.count))
              .replace("__TIMEOUT__", str(args.timeout)))
    if args.peer:
        out = peer_powershell(args.peer, script)
    else:
        code, out, err = local_powershell(script, timeout=60 + args.count * args.max_ttl * 2)
    rows = None
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("["):
            try:
                rows = json.loads(line)
                break
            except ValueError:
                continue
    if rows is None:
        raise Failure("hops produced no data: %s" % out.strip()[:300])

    print("path to %s  (%d probes per hop)" % (args.dest, args.count))
    print("")
    rows = as_list(rows)
    print("  hop  address            ok/n   min   avg   p95   max   >100ms")
    print("  ---  -----------------  ----  ----  ----  ----  ----  ------")
    worst = None
    for row in as_list(rows):
        stats = distribution(row.get("rtts") or [], 100)
        if not stats:
            # The hop answered the TTL probe but ignores later pings (ICMP
            # rate-limiting is common). Say that, instead of "no answer".
            print("  %3d  %-17s   0/%-2d  (answered TTL, drops pings)"
                  % (row["ttl"], row.get("address") or "-", len(as_list(row.get("rtts")))))
            continue
        flag = ""
        if stats["max"] > 100:
            flag = "  <== spikes start here"
            if worst is None:
                worst = row["ttl"]
        print("  %3d  %-17s  %2d/%-2d  %4d  %4d  %4d  %4d   %2d/%-2d%s"
              % (row["ttl"], row["address"], stats["ok"], stats["n"], stats["min"],
                 stats["avg"], stats["p95"], stats["max"], stats["over"], stats["ok"], flag))

    print("")
    if worst is None:
        print("  No hop shows spikes above 100ms.")
    else:
        print("  First hop with spikes above 100ms: hop %d (%s)." % (worst, rows[worst - 1].get("address")))
        if worst <= 2:
            print("  That is your own gear -- look at the LAN link, Wi-Fi or the router.")
        else:
            print("  Hops before it are clean, so this is an upstream queue. When a link")
            print("  is saturated the queue builds at the bottleneck: shaping *before*")
            print("  that point (see 'latency cap') removes the wait instead of hiding it.")
    return 0


# --- top: who is actually using the uplink? -----------------------------------

TOP_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
__MIHOMO_HELPER__
$seconds = __SECONDS__
$deadline = (Get-Date).AddSeconds($seconds)
$seen = @{}

while ((Get-Date) -lt $deadline) {
    $reply = Get-MihomoConnections
    if ($null -ne $reply) {
        foreach ($c in $reply.connections) {
            $key = "$($c.id)"
            $host_name = "$($c.metadata.host)"
            if (-not $host_name) { $host_name = "$($c.metadata.destinationIP)" }
            $process = "$($c.metadata.process)"
            $rate = 0
            if ($seen.ContainsKey($key)) {
                if ($c.upload -ge $seen[$key].last) { $rate = $c.upload - $seen[$key].first }
                $seen[$key].last = $c.upload
                $seen[$key].rate = $rate
            } else {
                $seen[$key] = @{ first = $c.upload; last = $c.upload; rate = $c.upload;
                                 host = $host_name; process = $process; chain = "$($c.chains -join '<')" }
            }
        }
    }
    Start-Sleep -Milliseconds 700
}

$byHost = @{}
foreach ($k in $seen.Keys) {
    $item = $seen[$k]
    $h = $item.host
    if (-not $byHost.ContainsKey($h)) {
        $byHost[$h] = @{ host = $h; bytes = 0; process = $item.process; chain = $item.chain }
    }
    # last-first, not last: a connection that existed before sampling started
    # already carries a large cumulative count, and adding it invents traffic.
    $byHost[$h].bytes += ($item.last - $item.first)
}
$rows = @()
foreach ($k in $byHost.Keys) {
    $item = $byHost[$k]
    $rows += [ordered]@{
        host    = $item.host
        rate_kb = [math]::Round($item.bytes / $seconds / 1KB, 1)
        total_mb = [math]::Round($item.bytes / 1MB, 2)
        process = $item.process
        chain   = $item.chain
    }
}
$rows = $rows | Sort-Object @{Expression = 'rate_kb'; Descending = $true}
@($rows) | ConvertTo-Json -Depth 6 -Compress
"""


def cmd_top(args):
    script = (TOP_SCRIPT
              .replace("__MIHOMO_HELPER__", MIHOMO_CONNECTIONS)
              .replace("__SECONDS__", str(args.seconds)))
    out = peer_powershell(args.peer, script, timeout=args.seconds + 120) if args.peer else local_powershell(script)[1]
    rows = None
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("["):
            try:
                rows = json.loads(line)
                break
            except ValueError:
                continue
    if rows is None:
        raise Failure("top produced no data (is mihomo running there?): %s" % out.strip()[:300])
    rows = as_list(rows)
    if not rows:
        print("no upstream traffic through the core in the last %ds." % args.seconds)
        return 0

    print("upstream through mihomo over %ds  (peak, not average, is what saturates a link)"
          % args.seconds)
    print("")
    print("  %-32s %10s %10s  %s" % ("host", "KB/s", "total MB", "process"))
    print("  %-32s %10s %10s  %s" % ("-" * 32, "-" * 10, "-" * 10, "-" * 10))
    for row in rows[:args.limit]:
        print("  %-32s %10.1f %10.2f  %s"
              % (row.get("host", "-")[:32], row.get("rate_kb", 0), row.get("total_mb", 0),
                 (row.get("process") or "-")[:24]))

    total = sum(r.get("rate_kb", 0) for r in rows)
    print("")
    print("  total upstream: %.1f KB/s = %.1f Mbps" % (total, total * 8 / 1024.0))
    top = rows[0]
    if total > 0 and top.get("rate_kb", 0) > total * 0.5:
        print("  One host carries %.0f%% of it: %s" % (100.0 * top["rate_kb"] / total, top.get("host")))
    return 0


# --- probe: traffic and RTT on the same clock, on the same machine -------------

PROBE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
__PING_HELPER__
__UPLINK_HELPER__
$nic = Get-UplinkNic
if (-not $nic) { @{ error = 'no physical uplink adapter found' } | ConvertTo-Json; exit }
$target = '__TARGET__'
$seconds = __SECONDS__
$interval = __INTERVAL__
$timeout = __TIMEOUT__

# Sampling and RTT are measured on the *same* machine: two machines' clocks
# cannot be aligned well enough, and comparing them invents correlations.
$clock = [Diagnostics.Stopwatch]::StartNew()
$samples = @()
while ($clock.Elapsed.TotalSeconds -lt $seconds) {
    Start-Sleep -Milliseconds $interval
    $stamp = $clock.Elapsed.TotalMilliseconds
    $sent = (Get-NetAdapterStatistics -Name $nic).SentBytes
    $rtt = Send-Ping $target $timeout 128
    $samples += "$stamp|$sent|$rtt"
}
[ordered]@{ nic = $nic; target = $target; samples = $samples } | ConvertTo-Json -Depth 6 -Compress
"""


def cmd_probe(args):
    script = (PROBE_SCRIPT
              .replace("__PING_HELPER__", PING_HELPER)
              .replace("__UPLINK_HELPER__", UPLINK_HELPER)
              .replace("__TARGET__", args.target)
              .replace("__SECONDS__", str(args.seconds))
              .replace("__INTERVAL__", str(args.interval))
              .replace("__TIMEOUT__", str(args.timeout)))
    out = peer_powershell(args.peer, script, timeout=args.seconds + 120) if args.peer else local_powershell(script)[1]
    data = None
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if data is None:
        raise Failure("probe produced no data: %s" % out.strip()[:300])
    if data.get("error"):
        raise Failure(data["error"])

    samples = []
    for raw in as_list(data.get("samples")):
        parts = raw.split("|")
        if len(parts) != 3:
            continue
        samples.append((float(parts[0]), int(parts[1]), int(parts[2])))

    # Rate must come from timestamps: a blocking probe makes the nominal
    # interval a lie, which is how "24 Gbps" readings get invented.
    rates = []
    for index in range(1, len(samples)):
        t0, b0, _ = samples[index - 1]
        t1, b1, rtt = samples[index]
        gap = (t1 - t0) / 1000.0
        if gap <= 0:
            continue
        rates.append(((b1 - b0) / 1024.0 / gap, rtt, gap))

    if not rates:
        raise Failure("no usable samples")

    gaps = [r[2] for r in rates]
    print("probe on %s: %s -> %s, %d samples over %ds"
          % (("peer " + args.peer) if args.peer else "this machine",
             data.get("nic"), data.get("target"), len(rates), args.seconds))
    print("  real sample gap: %.2f .. %.2f s (nominal %d ms)"
          % (min(gaps), max(gaps), args.interval))

    # A per-sample window is too short to trust: one TCP burst can fill the
    # adapter's staging buffer and read faster than the WAN link ever runs.
    # Recompute over ~1s windows so the peak means something.
    smoothed = []
    trail = 0
    for index in range(len(samples)):
        while trail < index and samples[index][0] - samples[trail][0] > 1000:
            trail += 1
        span = (samples[index][0] - samples[trail][0]) / 1000.0
        if span >= 0.8:
            moved = (samples[index][1] - samples[trail][1]) / 1024.0
            smoothed.append((moved / span, samples[index][2]))
    if not smoothed:
        raise Failure("sampling window too short to compute a rate")

    raw_peak = max(r[0] for r in rates)
    up = [r[0] for r in smoothed]
    peak = max(up)
    print("  egress over ~1s windows: median %.0f KB/s   peak %.0f KB/s (%.1f Mbps)"
          % (sorted(up)[len(up) // 2], peak, peak * 8 / 1024.0))
    if raw_peak > peak * 1.5:
        print("  (single %dms samples peaked at %.0f KB/s; that is buffer burst, not link rate)"
              % (args.interval, raw_peak))

    # The useful comparison is the distribution of RTT while the link is idle
    # versus busy. An average would hide exactly the spikes being hunted.
    print("")
    print("  egress bucket        n   RTT median   max   >100ms")
    print("  ------------------  ---  ----------  -----  ------")
    edges = [0, peak * 0.25, peak * 0.6, peak + 1]
    labels = ["low", "medium", "high (busiest)"]
    stats_by_label = {}
    for index, label in enumerate(labels):
        low, high = edges[index], edges[index + 1]
        group = [rtt for rate, rtt in smoothed if low <= rate < high and rtt >= 0]
        if not group:
            continue
        stats = distribution(group, 100)
        stats_by_label[label] = stats
        print("  %-18s  %3d  %9.0f  %5d  %2d/%-2d"
              % (label, stats["ok"], stats["avg"], stats["max"], stats["over"], stats["ok"]))

    print("")
    low_stats = stats_by_label.get("low")
    high_stats = stats_by_label.get("high (busiest)")

    def spiking(stats):
        return bool(stats) and stats["ok"] and stats["max"] > 100

    # A quiet link cannot tell us anything about queueing, and saying so is more
    # useful than inventing a verdict from noise.
    if peak < 500:
        print("  The link barely got used in this window (peak %.0f KB/s), so this" % peak)
        print("  sample cannot say anything about congestion. Re-run while it is busy:")
        print("    latency probe --peer <peer> --seconds 60")
    elif low_stats and spiking(low_stats) and low_stats["over"] > low_stats["ok"] * 0.1:
        # Spikes while idle mean the path itself is bad: no amount of shaping
        # will fix a link that already jitters with nothing on it.
        print("  It spikes even when idle (low bucket: %d/%d above 100ms, max %dms)."
              % (low_stats["over"], low_stats["ok"], low_stats["max"]))
        print("  That is not queueing -- shaping will not help. Look at the path itself:")
        print("    Wi-Fi signal / interference, 2.4 GHz vs 5 GHz roaming, cable, router.")
        print("    'latency hops --dest <that host>' shows whether it starts at your gear.")
    elif high_stats and spiking(high_stats):
        share = 100.0 * high_stats["over"] / max(1, high_stats["ok"])
        print("  It only spikes when busy: %.0f%% of packets above 100ms at the busiest"
              % share)
        print("  bucket (max %dms), against %dms idle. That is a queue building at the"
              % (high_stats["max"], low_stats["max"] if low_stats else -1))
        print("  bottleneck, and shaping in front of it removes the wait:")
        print("    latency cap --peer <peer> on <50-70% of the measured uplink>")
    else:
        print("  No queueing and no path problem showed up in this window.")

    return 0


# --- shaping: Windows QoS ------------------------------------------------------
#
# Three findings are baked into the scripts below. Each was measured, and each
# one silently produces a policy that looks correct and does nothing:
#
#   1. Only -PolicyStore ActiveStore is enforced. The default store and
#      PersistentStore accept the policy and ignore it (8 Mbps policy, 11.8 Mbps
#      observed). PersistentStore may also fail outright with "The network path
#      was not found".
#   2. -AppPathNameMatchCondition does not throttle. Measured: a 2 Mbps policy
#      against curl.exe still pushed 11.4 Mbps. Throttle is whole-machine only.
#   3. ActiveStore does not survive a reboot, so a scheduled task must re-apply
#      it at startup.
#
# And one about verification, which cost the most time:
#
#   4. Reading ActiveStore policies and SYSTEM-owned scheduled tasks requires
#      admin. Without it Get-NetQosPolicy returns *zero rows* (not an error) and
#      schtasks says "Access is denied". A non-elevated check therefore reports
#      a working setup as "not enabled".

ADMIN_PROBE = r"""
$ProgressPreference = 'SilentlyContinue'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
"""

CAP_STATUS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$admin = __ADMIN__
$policyName = 'LatencyCap'
$taskName = 'LatencyCap'
$current = Get-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -ErrorAction SilentlyContinue
$legacy = Get-NetQosPolicy -Name $policyName -ErrorAction SilentlyContinue
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
[ordered]@{
    admin   = $admin
    enabled = ($null -ne $current)
    rate_mbps = if ($current) { [math]::Round($current.ThrottleRateAction / 1e6, 2) } else { 0 }
    in_default_store = ($null -ne $legacy)
    task    = if ($task) { "$($task.State)" } else { '' }
} | ConvertTo-Json -Depth 6 -Compress
"""

CAP_ON_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$policyName = 'LatencyCap'
$taskName = 'LatencyCap'
$bits = [uint64](__MBPS__ * 1000000)

# Clear both stores: a stale policy in the default store is inert but confusing.
Remove-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetQosPolicy -Name $policyName -Confirm:$false -ErrorAction SilentlyContinue

# Whole machine, ActiveStore. Deliberately no -AppPathNameMatchCondition.
New-NetQosPolicy -Name $policyName -ThrottleRateActionBitsPerSecond $bits `
    -PolicyStore ActiveStore -Confirm:$false | Out-Null

$verify = Get-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -ErrorAction SilentlyContinue
if (-not $verify) { throw 'policy was not created' }

# ActiveStore is volatile, so re-apply at startup.
$scriptPath = 'C:\Windows\Temp\latency-cap.ps1'
$bootstrap = @"
New-NetQosPolicy -Name '$policyName' -ThrottleRateActionBitsPerSecond $bits -PolicyStore ActiveStore -Confirm:`$false | Out-Null
"@
Set-Content -Path $scriptPath -Value $bootstrap -Encoding ASCII
Register-ScheduledTask -TaskName $taskName -Force -RunLevel Highest -User 'SYSTEM' `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $scriptPath") `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) -ErrorAction SilentlyContinue | Out-Null

# Prove it, do not assume it: sample the uplink's own byte counter.
__UPLINK_HELPER__
$nic = Get-UplinkNic
$prev = (Get-NetAdapterStatistics -Name $nic).SentBytes
$peaks = @()
for ($i = 0; $i -lt __VERIFY__; $i++) {
    Start-Sleep -Seconds 1
    $now = (Get-NetAdapterStatistics -Name $nic).SentBytes
    $peaks += (($now - $prev) * 8 / 1MB)
    $prev = $now
}
[ordered]@{
    rate_mbps = [math]::Round($verify.ThrottleRateAction / 1e6, 2)
    nic = $nic
    samples = $peaks.Count
    avg_mbps = [math]::Round((($peaks | Measure-Object -Average).Average), 2)
    max_mbps = [math]::Round((($peaks | Measure-Object -Maximum).Maximum), 2)
} | ConvertTo-Json -Depth 6 -Compress
"""

CAP_OFF_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$policyName = 'LatencyCap'
$taskName = 'LatencyCap'
$removed = $false
if (Get-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -ErrorAction SilentlyContinue) {
    Remove-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -Confirm:$false
    $removed = $true
}
if (Get-NetQosPolicy -Name $policyName -ErrorAction SilentlyContinue) {
    Remove-NetQosPolicy -Name $policyName -Confirm:$false
    $removed = $true
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    $removed = $true
}
Remove-Item 'C:\Windows\Temp\latency-cap.ps1' -Force -ErrorAction SilentlyContinue
$still = Get-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -ErrorAction SilentlyContinue
[ordered]@{ removed = $removed; still_enabled = ($null -ne $still) } | ConvertTo-Json -Depth 6 -Compress
"""

PRIO_ON_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$apps = __APPS__
$policyBase = 'LatencyPrio'
$taskName = 'LatencyPrio'

foreach ($p in @(Get-NetQosPolicy -Name "$policyBase*" -PolicyStore ActiveStore -ErrorAction SilentlyContinue)) {
    Remove-NetQosPolicy -Name $p.Name -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
}
$created = @()
foreach ($app in $apps) {
    $safe = $app -replace '\.exe$', ''
    try {
        New-NetQosPolicy -Name "$policyBase-$safe" -AppPathNameMatchCondition $app `
            -DSCPAction 46 -PolicyStore ActiveStore -Confirm:$false | Out-Null
        $created += $app
    } catch { }
}
# Re-apply at startup: ActiveStore is volatile.
$scriptPath = 'C:\Windows\Temp\latency-prio.ps1'
$bootstrap = ''
foreach ($app in $apps) {
    $safe = $app -replace '\.exe$', ''
    $bootstrap += "New-NetQosPolicy -Name '$policyBase-$safe' -AppPathNameMatchCondition '$app' -DSCPAction 46 -PolicyStore ActiveStore -Confirm:`$false | Out-Null`r`n"
}
Set-Content -Path $scriptPath -Value $bootstrap -Encoding ASCII
Register-ScheduledTask -TaskName $taskName -Force -RunLevel Highest -User 'SYSTEM' `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $scriptPath") `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) -ErrorAction SilentlyContinue | Out-Null

$rows = @(Get-NetQosPolicy -Name "$policyBase*" -PolicyStore ActiveStore -ErrorAction SilentlyContinue)
[ordered]@{ created = $created; policies = $rows.Count } | ConvertTo-Json -Depth 6 -Compress
"""

PRIO_STATUS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$admin = __ADMIN__
$rows = @(Get-NetQosPolicy -Name 'LatencyPrio*' -PolicyStore ActiveStore -ErrorAction SilentlyContinue)
$task = Get-ScheduledTask -TaskName 'LatencyPrio' -ErrorAction SilentlyContinue
[ordered]@{
    admin = $admin
    policies = @($rows | ForEach-Object { [ordered]@{ name = $_.Name; app = $_.AppPathNameMatchCondition; dscp = $_.DSCPAction } })
    task = if ($task) { "$($task.State)" } else { '' }
} | ConvertTo-Json -Depth 6 -Compress
"""

PRIO_OFF_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$found = $false
foreach ($p in @(Get-NetQosPolicy -Name 'LatencyPrio*' -PolicyStore ActiveStore -ErrorAction SilentlyContinue)) {
    Remove-NetQosPolicy -Name $p.Name -PolicyStore ActiveStore -Confirm:$false
    $found = $true
}
if (Get-ScheduledTask -TaskName 'LatencyPrio' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'LatencyPrio' -Confirm:$false
    $found = $true
}
Remove-Item 'C:\Windows\Temp\latency-prio.ps1' -Force -ErrorAction SilentlyContinue
[ordered]@{ removed = $found } | ConvertTo-Json -Depth 6 -Compress
"""



def as_list(value):
    """PowerShell 5.1 serialises a one-element array as a bare object."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def json_from(out):
    for line in reversed(out.strip().splitlines()):
        text = line.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except ValueError:
                continue
    raise Failure("no JSON in output: %s" % out.strip()[:300])


def where_to_run(peer):
    """Return (runner, describe). Both shapes accept a PowerShell script."""
    if peer:
        return (lambda script, timeout=240: peer_powershell(peer, script, timeout=timeout)), "peer " + peer
    return (lambda script, timeout=240: local_powershell(script, timeout=timeout)[1]), "this machine"


def parse_rate(text):
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([mM])?(?:bps)?\s*$", text or "")
    if not match:
        raise Failure("rate must look like 8 or 8Mbps, got %r" % text)
    return float(match.group(1))


def admin_powershell():
    return "True" if False else None  # replaced below; kept for clarity


def is_admin(runner):
    try:
        out = runner(ADMIN_PROBE, timeout=60)
    except Failure:
        return False
    return "true" in out.strip().lower()


def elevation_hint(kind, app=None):
    hint = os.path.join(SKILLS, "latency", "latency")
    if kind == "prio":
        return "powershell -Command \"Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','& \\\"%s\\\" prio --app %s on'\"" % (hint, app)
    return "powershell -Command \"Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','& \\\"%s\\\" cap on %s'\"" % (hint, "8")


def cmd_cap(args):
    runner, where = where_to_run(args.peer)

    if args.action == "status":
        out = runner(CAP_STATUS_SCRIPT.replace("__ADMIN__", "$true"))
        data = json_from(out)
        admin = is_admin(runner)
        print("shaping on %s" % where)
        print("  policy store: ActiveStore (the only store that is enforced)")
        if not admin:
            print("  note: this check is not elevated, so a working setup can look empty.")
            print("        Re-run from an elevated shell to trust a negative answer.")
        if data.get("enabled"):
            print("  enabled : %.2f Mbps, whole machine" % data.get("rate_mbps", 0))
        else:
            print("  enabled : no")
        if data.get("in_default_store"):
            print("  warning : a same-named policy exists in the default store, which is")
            print("            inert. 'cap off' clears it.")
        print("  startup : %s" % (data.get("task") or "no task (lost on reboot)"))
        return 0

    if not is_admin(runner):
        print("shaping needs administrator rights on %s." % where)
        print("")
        print("run this in an elevated PowerShell there:")
        print("  & \"%s\" cap on 8" % os.path.join(SKILLS, "latency", "latency"))
        return 2

    if args.action == "off":
        data = json_from(runner(CAP_OFF_SCRIPT))
        if data.get("removed"):
            print("shaping removed on %s (policy and startup task cleared)." % where)
        else:
            print("nothing to remove on %s." % where)
        if data.get("still_enabled"):
            print("warning: a policy is still present after removal.")
        return 0

    # on
    rate = parse_rate(args.rate or "8")
    script = (CAP_ON_SCRIPT
              .replace("__MBPS__", ("%g" % rate))
              .replace("__UPLINK_HELPER__", UPLINK_HELPER)
              .replace("__VERIFY__", str(args.verify)))
    out = runner(script, timeout=args.verify + 180)
    data = json_from(out)
    print("shaping enabled on %s" % where)
    print("  policy : %.2f Mbps, whole machine, ActiveStore" % data.get("rate_mbps", 0))
    print("  startup: scheduled task registered (ActiveStore is volatile)")
    print("")
    print("  verifying against the adapter's own byte counter, %ds..." % data.get("samples", 0))
    print("  egress: avg %.2f Mbps   max %.2f Mbps   (limit %.2f)"
          % (data.get("avg_mbps", 0), data.get("max_mbps", 0), data.get("rate_mbps", 0)))
    if data.get("max_mbps", 0) <= data.get("rate_mbps", 0) * 1.15:
        print("  verdict: the cap is enforced.")
    else:
        print("  verdict: measured peak is above the limit -- the cap is NOT enforced.")
    print("")
    print("  a cap only helps if it sits below the real uplink. If the uplink is")
    print("  faster than this number, agents simply get slower. Measure first:")
    print("    latency probe --peer %s --target 1.1.1.1 --seconds 30" % (args.peer or "<peer>"))
    return 0


def cmd_prio(args):
    runner, where = where_to_run(args.peer)

    if args.action == "status":
        data = json_from(runner(PRIO_STATUS_SCRIPT.replace("__ADMIN__", "$true")))
        print("game priority on %s" % where)
        if not is_admin(runner):
            print("  note: not elevated, so an empty list may be wrong.")
        policies = as_list(data.get("policies"))
        if not policies:
            print("  policies: none")
        for p in policies:
            print("  policies: %-26s %s -> DSCP %s" % (p.get("name"), p.get("app"), p.get("dscp")))
        print("  startup : %s" % (data.get("task") or "no task (lost on reboot)"))
        print("")
        print("  DSCP 46 only reaches the Wi-Fi airtime and routers that honour it.")
        print("  A home router usually does not, so this is not a substitute for cap.")
        return 0

    if not is_admin(runner):
        print("marking priority needs administrator rights on %s." % where)
        print("")
        print("run this in an elevated PowerShell there:")
        print("  & \"%s\" prio --app %s on" % (os.path.join(SKILLS, "latency", "latency"), args.app))
        return 2

    if args.action == "off":
        data = json_from(runner(PRIO_OFF_SCRIPT))
        print("priority cleared on %s." % where if data.get("removed") else "nothing to clear on %s." % where)
        return 0

    apps = [a.strip() for a in (args.app or "").split(",") if a.strip()]
    if not apps:
        raise Failure("--app is required, e.g. --app Overwatch.exe or --app a.exe,b.exe")
    quoted = ",".join("'%s'" % a.replace("'", "''") for a in apps)
    data = json_from(runner(PRIO_ON_SCRIPT.replace("__APPS__", "@(%s)" % quoted)))
    print("priority enabled on %s" % where)
    print("  marked : %s" % ", ".join(data.get("created") or []))
    print("  dscp   : 46 (EF)")
    print("  startup: scheduled task registered")
    return 0


# --- entry point ---------------------------------------------------------------


def main(argv):
    parser = argparse.ArgumentParser(
        prog="latency",
        description="Locate where latency is lost, then shape it in front of the bottleneck.")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="is a proxy or tunnel in this flow's path?")
    doctor.add_argument("--app", help="process name to inspect, e.g. Overwatch.exe")
    doctor.set_defaults(func=cmd_doctor)

    hops = sub.add_parser("hops", help="which hop starts spiking?")
    hops.add_argument("--dest", required=True, help="host or IP to trace and measure")
    hops.add_argument("--count", type=int, default=20, help="probes per hop (default 20)")
    hops.add_argument("--timeout", type=int, default=1200, help="per-probe timeout ms")
    hops.add_argument("--max-ttl", type=int, default=12, help="stop after this many hops")
    hops.add_argument("--peer", help="measure from another remembered machine")
    hops.set_defaults(func=cmd_hops)

    top = sub.add_parser("top", help="which host is eating the uplink?")
    top.add_argument("--peer", help="machine whose mihomo to read")
    top.add_argument("--seconds", type=int, default=20)
    top.add_argument("--limit", type=int, default=12)
    top.set_defaults(func=cmd_top)

    probe = sub.add_parser("probe", help="egress and RTT on one clock, one machine")
    probe.add_argument("--peer", help="sample this machine instead of the local one")
    probe.add_argument("--target", default="223.5.5.5", help="RTT target (default 223.5.5.5)")
    probe.add_argument("--seconds", type=int, default=60)
    probe.add_argument("--interval", type=int, default=150, help="ms between samples")
    probe.add_argument("--timeout", type=int, default=1200, help="ping timeout ms")
    probe.set_defaults(func=cmd_probe)

    cap = sub.add_parser("cap", help="Windows QoS uplink cap (whole machine)")
    cap.add_argument("action", choices=["on", "off", "status"])
    cap.add_argument("rate", nargs="?", help="Mbps when turning on, e.g. 8")
    cap.add_argument("--peer", help="machine to shape")
    cap.add_argument("--verify", type=int, default=20, help="verification seconds")
    cap.set_defaults(func=cmd_cap)

    prio = sub.add_parser("prio", help="DSCP 46 for a game's packets")
    prio.add_argument("action", choices=["on", "off", "status"])
    prio.add_argument("--app", help="comma separated exe names")
    prio.add_argument("--peer", help="machine to mark on")
    prio.set_defaults(func=cmd_prio)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except Failure as error:
        print("latency: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
