"""Quick per-stage latency benchmark against the live Nusuk + Groq endpoints.

Bypasses run_pipeline_eval.py (which is hardcoded for the local_api flavor) and
hits the same URLs/auth the agent uses today: Nusuk JWT for STT/TTS, Groq Bearer
for LLM. Streams LLM and TTS responses to measure TTFT / TTFA — the metrics that
actually matter for end-to-end voice latency.

Usage:
    .venv/bin/python eval/quick_speed.py eval/testdata/chunk_0005.wav
    .venv/bin/python eval/quick_speed.py eval/testdata/chunk_000{0..9}.wav
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import struct
import sys
import time
import wave
from pathlib import Path

import ssl

import aiohttp
import certifi
import numpy as np

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


def normalize_wav(path: Path) -> tuple[bytes, int, int, float]:
    with open(path, "rb") as f:
        f.seek(20)
        fmt_code = struct.unpack("<H", f.read(2))[0]
    if fmt_code == 1:
        with wave.open(str(path), "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            pcm = w.readframes(w.getnframes())
            duration = w.getnframes() / float(sr)
            return pcm, sr, ch, duration
    import soundfile  # lazy

    data, sr = soundfile.read(str(path), dtype="int16")
    ch = 1 if data.ndim == 1 else data.shape[1]
    duration = len(data) / float(sr)
    return data.tobytes(), sr, ch, duration


def pcm_to_wav(pcm: bytes, sr: int, ch: int) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


async def get_nusuk_token(session: aiohttp.ClientSession, env: dict[str, str]) -> str:
    base = env["CUSTOM_STT_URL"].rstrip("/")
    cid = env["CUSTOM_LLM_CLIENT_ID"]
    secret = env["CUSTOM_LLM_CLIENT_SECRET"]
    async with session.post(
        f"{base}/auth/token",
        json={"client_id": cid, "client_secret": secret, "user_id": cid},
    ) as r:
        r.raise_for_status()
        body = await r.json()
        return body["access_token"]


async def stt(session: aiohttp.ClientSession, env: dict[str, str], jwt: str, wav: bytes) -> tuple[str, float]:
    url = env["CUSTOM_STT_URL"].rstrip("/") + "/transcribe"
    form = aiohttp.FormData()
    form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
    t0 = time.perf_counter()
    async with session.post(url, data=form, headers={"Authorization": f"Bearer {jwt}"}) as r:
        r.raise_for_status()
        body = await r.json()
    dt = time.perf_counter() - t0
    text = body.get("transcription_text") or body.get("text") or ""
    return text, dt


async def llm(session: aiohttp.ClientSession, env: dict[str, str], user_text: str) -> tuple[str, float, float]:
    url = env["CUSTOM_LLM_URL"].rstrip("/") + "/chat/completions"
    system = (ROOT / "agent" / "system_prompt_rag.txt").read_text(encoding="utf-8") if (ROOT / "agent" / "system_prompt_rag.txt").exists() else env.get("AGENT_SYSTEM_PROMPT", "")
    prefix = env.get("CUSTOM_LLM_QUERY_PREFIX", "")
    payload = {
        "model": env["CUSTOM_LLM_MODEL"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": (prefix + "\n\n" + user_text).strip()},
        ],
        "temperature": float(env.get("CUSTOM_LLM_TEMPERATURE", "0.2")),
        "max_tokens": int(env.get("CUSTOM_LLM_MAX_TOKENS", "768")),
        "stream": True,
    }
    if env.get("CUSTOM_LLM_REASONING_EFFORT"):
        payload["reasoning_effort"] = env["CUSTOM_LLM_REASONING_EFFORT"]
    headers = {"Authorization": f"Bearer {env['GROQ_API_KEY']}", "Content-Type": "application/json"}

    t0 = time.perf_counter()
    ttft = None
    parts: list[str] = []
    async with session.post(url, json=payload, headers=headers) as r:
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
            content = delta.get("content")
            if content:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                parts.append(content)
    total = time.perf_counter() - t0
    return "".join(parts).strip(), (ttft or total), total


async def tts(session: aiohttp.ClientSession, env: dict[str, str], jwt: str, text: str) -> tuple[float, float, int]:
    url = env["CUSTOM_TTS_URL"].rstrip("/") + "/synthesize"
    headers = {"Authorization": f"Bearer {jwt}"}
    t0 = time.perf_counter()
    ttfa = None
    total_bytes = 0
    async with session.post(url, json={"text": text}, headers=headers) as r:
        r.raise_for_status()
        async for chunk in r.content.iter_any():
            if chunk:
                if ttfa is None:
                    ttfa = time.perf_counter() - t0
                total_bytes += len(chunk)
    total = time.perf_counter() - t0
    return (ttfa or total), total, total_bytes


async def run_one(session: aiohttp.ClientSession, env: dict[str, str], jwt: str, path: Path) -> dict:
    pcm, sr, ch, audio_dur = normalize_wav(path)
    wav = pcm_to_wav(pcm, sr, ch)
    out: dict = {"file": path.name, "audio_s": round(audio_dur, 2)}
    try:
        transcript, stt_s = await stt(session, env, jwt, wav)
        out["stt_s"] = round(stt_s, 3)
        out["transcript"] = transcript[:80]
        if not transcript:
            out["error"] = "empty_transcript"
            return out
        reply, ttft, llm_total = await llm(session, env, transcript)
        out["llm_ttft_s"] = round(ttft, 3)
        out["llm_total_s"] = round(llm_total, 3)
        out["reply_chars"] = len(reply)
        if not reply:
            out["error"] = "empty_reply"
            return out
        ttfa, tts_total, audio_bytes = await tts(session, env, jwt, reply)
        out["tts_ttfa_s"] = round(ttfa, 3)
        out["tts_total_s"] = round(tts_total, 3)
        out["tts_audio_kb"] = round(audio_bytes / 1024.0, 1)
        out["e2e_first_audio_s"] = round(stt_s + ttft + ttfa, 3)
        out["e2e_total_s"] = round(stt_s + llm_total + tts_total, 3)
    except aiohttp.ClientResponseError as exc:
        out["error"] = f"http {exc.status}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def summarize(rows: list[dict]) -> dict:
    keys = ["stt_s", "llm_ttft_s", "llm_total_s", "tts_ttfa_s", "tts_total_s", "e2e_first_audio_s", "e2e_total_s"]
    summary = {}
    for k in keys:
        vals = [r[k] for r in rows if k in r]
        if vals:
            summary[k] = {
                "n": len(vals),
                "min": round(min(vals), 3),
                "p50": round(statistics.median(vals), 3),
                "mean": round(statistics.mean(vals), 3),
                "max": round(max(vals), 3),
            }
    return summary


async def main(files: list[Path]) -> None:
    env = read_env(ENV_PATH)
    timeout = aiohttp.ClientTimeout(total=60)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(limit=1, ssl=ssl_ctx)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        jwt = await get_nusuk_token(session, env)
        rows = []
        for p in files:
            print(f"=== {p.name} ===", file=sys.stderr, flush=True)
            row = await run_one(session, env, jwt, p)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    print("\n=== summary ===", flush=True)
    print(json.dumps(summarize(rows), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args()
    asyncio.run(main(args.files))
