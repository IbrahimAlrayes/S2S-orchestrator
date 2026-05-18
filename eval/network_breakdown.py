"""Network-vs-compute latency breakdown for Nusuk STT/TTS and Groq LLM.

Goal: answer "is the slow STT/TTS time mostly network or actual server compute?"

Strategy:
  - aiohttp TraceConfig captures per-phase wall times: DNS, TCP+TLS connect,
    request body send, server first-byte, response complete.
  - For STT, Nusuk returns `processing_time_seconds` in the JSON body — we can
    subtract that from wall to get pure network.
  - For TTS we have no server-reported number, so we infer: TTFA minus the cold
    connect + minus a same-connection ping of similar payload size approximates
    server processing. Best we can do without server cooperation.
  - Each endpoint is exercised cold (fresh TCP) and warm (keepalive) to isolate
    the handshake cost.

Usage:
    .venv/bin/python eval/network_breakdown.py [--rounds N] [eval/testdata/chunk_0005.wav]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import statistics
import struct
import sys
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import certifi

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def make_trace() -> tuple[aiohttp.TraceConfig, dict]:
    timings: dict[str, float] = {}

    async def _stamp(name: str) -> None:
        timings[name] = time.perf_counter()

    cfg = aiohttp.TraceConfig()

    async def on_request_start(session, ctx, params):
        timings.clear()
        await _stamp("request_start")

    async def on_dns_resolvehost_start(session, ctx, params):
        await _stamp("dns_start")

    async def on_dns_resolvehost_end(session, ctx, params):
        await _stamp("dns_end")

    async def on_connection_create_start(session, ctx, params):
        await _stamp("conn_start")

    async def on_connection_create_end(session, ctx, params):
        await _stamp("conn_end")

    async def on_connection_reuseconn(session, ctx, params):
        await _stamp("conn_reused")

    async def on_request_chunk_sent(session, ctx, params):
        timings["request_sent"] = time.perf_counter()

    async def on_response_chunk_received(session, ctx, params):
        if "first_byte" not in timings:
            timings["first_byte"] = time.perf_counter()

    async def on_request_end(session, ctx, params):
        await _stamp("request_end")

    cfg.on_request_start.append(on_request_start)
    cfg.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    cfg.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    cfg.on_connection_create_start.append(on_connection_create_start)
    cfg.on_connection_create_end.append(on_connection_create_end)
    cfg.on_connection_reuseconn.append(on_connection_reuseconn)
    cfg.on_request_chunk_sent.append(on_request_chunk_sent)
    cfg.on_response_chunk_received.append(on_response_chunk_received)
    cfg.on_request_end.append(on_request_end)
    return cfg, timings


def fmt_phase(t: dict[str, float]) -> dict[str, float]:
    """Convert raw timestamps into stage durations in milliseconds."""
    base = t.get("request_start")
    if base is None:
        return {}

    def ms(a: str, b: str) -> float | None:
        if a not in t or b not in t:
            return None
        return round((t[b] - t[a]) * 1000.0, 1)

    out = {
        "dns_ms": ms("dns_start", "dns_end"),
        "tcp_tls_ms": ms("conn_start", "conn_end"),
        "send_ms": ms("conn_end" if "conn_end" in t else "request_start", "request_sent")
        if "request_sent" in t
        else None,
        "ttfb_ms": ms("request_start", "first_byte"),
        "total_ms": ms("request_start", "request_end"),
    }
    if "conn_reused" in t:
        out["conn_reused"] = True
    return out


async def tcp_ping(host: str, port: int = 443, n: int = 5) -> dict[str, float]:
    """Pure TCP-only RTT (no TLS, no HTTP). Approximates raw network round trip."""
    samples = []
    loop = asyncio.get_running_loop()
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            sock = await loop.run_in_executor(
                None, lambda: socket.create_connection((host, port), timeout=5)
            )
            samples.append((time.perf_counter() - t0) * 1000.0)
            sock.close()
        except OSError as exc:
            samples.append(float("nan"))
            print(f"tcp_ping {host}:{port} failed: {exc}", file=sys.stderr)
        await asyncio.sleep(0.05)
    clean = [s for s in samples if s == s]  # NaN filter
    if not clean:
        return {"n": 0}
    return {
        "n": len(clean),
        "min_ms": round(min(clean), 1),
        "p50_ms": round(statistics.median(clean), 1),
        "mean_ms": round(statistics.mean(clean), 1),
        "max_ms": round(max(clean), 1),
    }


async def get_jwt(session: aiohttp.ClientSession, env: dict[str, str]) -> str:
    base = env["CUSTOM_STT_URL"].rstrip("/")
    async with session.post(
        f"{base}/auth/token",
        json={
            "client_id": env["CUSTOM_LLM_CLIENT_ID"],
            "client_secret": env["CUSTOM_LLM_CLIENT_SECRET"],
            "user_id": env["CUSTOM_LLM_CLIENT_ID"],
        },
    ) as r:
        r.raise_for_status()
        return (await r.json())["access_token"]


async def measure_auth(env: dict[str, str], rounds: int) -> dict:
    """Auth endpoint = small POST with no compute; pure-ish network baseline."""
    cold_phases, warm_phases = [], []
    cfg, t = make_trace()
    timeout = aiohttp.ClientTimeout(total=15)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    # Warm: single connector, repeated calls — first one is cold, rest warm.
    connector = aiohttp.TCPConnector(limit=1, ssl=ssl_ctx)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trace_configs=[cfg]) as s:
        for i in range(rounds):
            await get_jwt(s, env)
            phases = fmt_phase(t)
            (cold_phases if i == 0 else warm_phases).append(phases)
    return {"cold": cold_phases, "warm": warm_phases}


async def measure_stt(env: dict[str, str], wav: Path, rounds: int) -> dict:
    pcm, sr, ch, audio_dur = _normalize_wav(wav)
    wav_bytes = _pcm_to_wav(pcm, sr, ch)
    cold_phases, warm_phases = [], []
    server_times: list[float] = []
    timeout = aiohttp.ClientTimeout(total=60)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    cfg, t = make_trace()
    connector = aiohttp.TCPConnector(limit=1, ssl=ssl_ctx)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trace_configs=[cfg]) as s:
        jwt = await get_jwt(s, env)
        url = env["CUSTOM_STT_URL"].rstrip("/") + "/transcribe"
        for i in range(rounds):
            form = aiohttp.FormData()
            form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
            t0 = time.perf_counter()
            async with s.post(url, data=form, headers={"Authorization": f"Bearer {jwt}"}) as r:
                r.raise_for_status()
                body = await r.json()
            wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            server_s = float(body.get("processing_time_seconds") or 0.0)
            server_times.append(server_s * 1000.0)
            phases = fmt_phase(t)
            phases["wall_ms"] = wall_ms
            phases["server_ms"] = round(server_s * 1000.0, 1)
            phases["network_ms"] = round(wall_ms - server_s * 1000.0, 1)
            phases["upload_kb"] = round(len(wav_bytes) / 1024.0, 1)
            (cold_phases if i == 0 else warm_phases).append(phases)
    return {
        "audio_duration_s": round(audio_dur, 2),
        "cold": cold_phases,
        "warm": warm_phases,
    }


async def measure_tts(env: dict[str, str], reply_text: str, rounds: int) -> dict:
    cold_phases, warm_phases = [], []
    timeout = aiohttp.ClientTimeout(total=60)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    cfg, t = make_trace()
    connector = aiohttp.TCPConnector(limit=1, ssl=ssl_ctx)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trace_configs=[cfg]) as s:
        jwt = await get_jwt(s, env)
        url = env["CUSTOM_TTS_URL"].rstrip("/") + "/synthesize"
        for i in range(rounds):
            t0 = time.perf_counter()
            ttfa = None
            total_bytes = 0
            async with s.post(url, json={"text": reply_text}, headers={"Authorization": f"Bearer {jwt}"}) as r:
                r.raise_for_status()
                async for chunk in r.content.iter_any():
                    if chunk:
                        if ttfa is None:
                            ttfa = (time.perf_counter() - t0) * 1000.0
                        total_bytes += len(chunk)
            wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            phases = fmt_phase(t)
            phases["wall_ms"] = wall_ms
            phases["ttfa_ms"] = round(ttfa or 0.0, 1)
            phases["audio_kb"] = round(total_bytes / 1024.0, 1)
            (cold_phases if i == 0 else warm_phases).append(phases)
    return {"text_chars": len(reply_text), "cold": cold_phases, "warm": warm_phases}


async def measure_groq(env: dict[str, str], rounds: int) -> dict:
    cold_phases, warm_phases = [], []
    timeout = aiohttp.ClientTimeout(total=30)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    cfg, t = make_trace()
    connector = aiohttp.TCPConnector(limit=1, ssl=ssl_ctx)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, trace_configs=[cfg]) as s:
        url = env["CUSTOM_LLM_URL"].rstrip("/") + "/chat/completions"
        for i in range(rounds):
            payload = {
                "model": env["CUSTOM_LLM_MODEL"],
                "messages": [
                    {"role": "system", "content": "أجب بإيجاز."},
                    {"role": "user", "content": "ما عاصمة السعودية؟"},
                ],
                "temperature": 0.2,
                "max_tokens": 128,
                "stream": True,
            }
            if env.get("CUSTOM_LLM_REASONING_EFFORT"):
                payload["reasoning_effort"] = env["CUSTOM_LLM_REASONING_EFFORT"]
            headers = {"Authorization": f"Bearer {env['GROQ_API_KEY']}", "Content-Type": "application/json"}
            t0 = time.perf_counter()
            ttft = None
            async with s.post(url, json=payload, headers=headers) as r:
                r.raise_for_status()
                async for raw in r.content:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    if delta.get("content") and ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000.0
            wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            phases = fmt_phase(t)
            phases["wall_ms"] = wall_ms
            phases["ttft_ms"] = round(ttft or 0.0, 1)
            (cold_phases if i == 0 else warm_phases).append(phases)
    return {"cold": cold_phases, "warm": warm_phases}


def _normalize_wav(path: Path) -> tuple[bytes, int, int, float]:
    with open(path, "rb") as f:
        f.seek(20)
        fmt_code = struct.unpack("<H", f.read(2))[0]
    if fmt_code == 1:
        with wave.open(str(path), "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            pcm = w.readframes(w.getnframes())
            duration = w.getnframes() / float(sr)
            return pcm, sr, ch, duration
    import soundfile

    data, sr = soundfile.read(str(path), dtype="int16")
    ch = 1 if data.ndim == 1 else data.shape[1]
    duration = len(data) / float(sr)
    return data.tobytes(), sr, ch, duration


def _pcm_to_wav(pcm: bytes, sr: int, ch: int) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return round(statistics.mean(vals), 1)


def summarize(label: str, blob: dict, extra_keys: list[str]) -> None:
    print(f"\n=== {label} ===")
    if blob.get("audio_duration_s"):
        print(f"  audio_duration: {blob['audio_duration_s']}s")
    if blob.get("text_chars"):
        print(f"  reply_chars: {blob['text_chars']}")
    cold = blob["cold"][0] if blob["cold"] else {}
    warm = blob["warm"]
    if cold:
        print("  cold (fresh TCP+TLS):")
        for k in ["dns_ms", "tcp_tls_ms", "send_ms", "ttfb_ms", "wall_ms"] + extra_keys:
            v = cold.get(k)
            if v is not None:
                print(f"    {k:>12}: {v}")
    if warm:
        print(f"  warm (keepalive, n={len(warm)}):")
        for k in ["send_ms", "ttfb_ms", "wall_ms"] + extra_keys:
            v = _avg(warm, k)
            if v is not None:
                print(f"    {k:>12}: {v}")


async def main(args: argparse.Namespace) -> None:
    env = read_env(ENV_PATH)
    nusuk_host = urlparse(env["CUSTOM_STT_URL"]).hostname
    groq_host = urlparse(env["CUSTOM_LLM_URL"]).hostname

    print("=== TCP-only RTT (5 samples each) ===")
    nusuk_rtt = await tcp_ping(nusuk_host)
    groq_rtt = await tcp_ping(groq_host)
    print(f"  {nusuk_host}: {nusuk_rtt}")
    print(f"  {groq_host}: {groq_rtt}")

    print("\n=== Auth POST (small, near-zero compute) ===")
    auth = await measure_auth(env, args.rounds)
    summarize(f"Nusuk auth POST → {nusuk_host}/auth/token", auth, [])

    wav_path = args.wav
    print(f"\n=== STT POST (audio = {wav_path.name}) ===")
    stt = await measure_stt(env, wav_path, args.rounds)
    summarize(f"Nusuk STT POST → {nusuk_host}/transcribe", stt, ["upload_kb", "server_ms", "network_ms"])

    reply = "السعودية بلد عربي. عاصمتها الرياض. تقع في غرب آسيا."
    print(f"\n=== TTS POST ===")
    tts = await measure_tts(env, reply, args.rounds)
    summarize(f"Nusuk TTS POST → {nusuk_host}/synthesize", tts, ["audio_kb", "ttfa_ms"])

    print(f"\n=== LLM stream (Groq) ===")
    llm = await measure_groq(env, args.rounds)
    summarize(f"Groq /chat/completions → {groq_host}", llm, ["ttft_ms"])

    print("\n=== interpretation ===")
    if stt["warm"]:
        warm_stt = stt["warm"]
        avg_wall = _avg(warm_stt, "wall_ms")
        avg_server = _avg(warm_stt, "server_ms")
        avg_net = _avg(warm_stt, "network_ms")
        if avg_wall and avg_server is not None:
            print(
                f"  STT warm: wall={avg_wall}ms = server_compute={avg_server}ms"
                f" + network={avg_net}ms ({round(100*avg_net/avg_wall,1)}% network)"
            )
    if tts["warm"]:
        warm_tts = tts["warm"]
        avg_wall = _avg(warm_tts, "wall_ms")
        avg_ttfa = _avg(warm_tts, "ttfa_ms")
        avg_ttfb = _avg(warm_tts, "ttfb_ms")
        if avg_wall and avg_ttfa is not None and nusuk_rtt.get("p50_ms"):
            est_server_first = max(0.0, avg_ttfa - 1.0 * nusuk_rtt["p50_ms"])  # one-way ≈ RTT/2 each direction
            print(
                f"  TTS warm: TTFA={avg_ttfa}ms; rough split ≈ "
                f"server_first_chunk≈{round(est_server_first,1)}ms + "
                f"network_RTT={nusuk_rtt['p50_ms']}ms"
            )
            print(f"  TTS warm: wall_total={avg_wall}ms (full audio body received)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", type=Path, default=Path(__file__).parent / "testdata" / "chunk_0005.wav")
    ap.add_argument("--rounds", type=int, default=5, help="calls per endpoint (1st cold, rest warm)")
    asyncio.run(main(ap.parse_args()))
