"""DeepFilterNet3 noise cancellation as a LiveKit FrameProcessor.

Apache-2.0 self-hosted denoiser. ~10 ms algorithmic latency (20 ms STFT
window − 10 ms hop, df_lookahead=0). ~2 ms compute per 10 ms frame on a
single-threaded x86 server CPU (paper RTF 0.19).

Wired into room_io.AudioInputOptions.noise_cancellation in agent.py.
Toggle via AGENT_NOISE_CANCELLATION=true|false. Default off — pulling
PyTorch + DeepFilterNet into the agent image is ~600 MB bloat, so we
only pay that when explicitly enabled.

Caveats
-------
* DeepFilterNet3 is 48 kHz native. We resample src_sr↔48 kHz per call
  via df.io.resample (sinc_fast). At 16 kHz input that's ~0.5 ms each
  direction on server CPU.
* enhance() resets the model's LSTM hidden state per call. At LiveKit
  frame boundaries (50 ms by our room_io config = 5 × 10 ms DF frames)
  there can be a sub-frame boundary artifact. Inaudible for ASR.
* The first call after init_df() loads model weights (~50–200 ms,
  one-time per session).
"""
from __future__ import annotations

import logging

import numpy as np
from livekit import rtc

logger = logging.getLogger("nusuk-agent.denoiser")

_TARGET_SR = 48000  # DeepFilterNet3 native rate

# Sliding-window context. DeepFilterNet's df.enhance.enhance() is a one-shot
# offline function — it estimates the noise floor from whatever buffer it's
# given. When called per 50 ms LiveKit frame, the model can't tell speech
# from noise (50 ms isn't enough context) and over-suppresses by 30-50% across
# all voice bands, leaving ASR with garbage. Measured against a real Arabic
# speech WAV: per-frame voice-mid ratio 0.53× vs whole-buffer 0.94×.
#
# Fix: keep a rolling history buffer, run enhance() on the full history each
# call (giving the model rich context to estimate noise), but only emit the
# LAST `new` samples (the current frame's slot, now denoised with context).
# Output is time-aligned with input — zero added algorithmic latency.
_CONTEXT_MS = 250  # buffer length the model sees per call


class DeepFilterDenoiser(rtc.FrameProcessor[rtc.AudioFrame]):
    """LiveKit FrameProcessor wrapping DeepFilterNet3 in a sliding-window
    streaming mode.

    One instance per AgentSession. Owns its own torch model, DF state, and
    history buffer so sessions don't share state.
    """

    def __init__(self) -> None:
        # Lazy import — keeps agent boot lightweight when denoiser is off.
        from df.enhance import enhance, init_df
        from df.io import resample as df_resample
        import torch

        self._torch = torch
        self._enhance = enhance
        self._resample = df_resample

        # init_df returns (model, df_state, suffix). Older docstrings claim a
        # 4-tuple — verified against 0.5.6 wheel and it's 3.
        self._model, self._df_state, _ = init_df()
        self._model.eval()

        # Rolling history buffer for sliding-window enhancement. We always
        # feed the model `_CONTEXT_MS` of audio (current frame plus prior
        # history) so it has enough context to build a proper noise model,
        # but only return the last frame's worth of samples — output is
        # time-aligned with input, no added algorithmic latency.
        # Allocated lazily per session at the input sample rate.
        self._history_f32: np.ndarray | None = None

        self._enabled = True

        logger.info(
            "denoiser_initialized model=deepfilternet3 native_sr=%d",
            _TARGET_SR,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def _process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if not self._enabled:
            return frame

        pcm_i16 = np.frombuffer(frame.data, dtype=np.int16)
        n_ch = frame.num_channels
        src_sr = frame.sample_rate

        # int16 LE → float32 in [-1, 1]
        pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
        if n_ch > 1:
            # DeepFilterNet expects mono; downmix here, re-broadcast on the way out.
            pcm_f32 = pcm_f32.reshape(-1, n_ch).mean(axis=1)

        new_n = pcm_f32.shape[0]
        # Lazy-init the history buffer to (_CONTEXT_MS) of zeros at the current
        # session's sample rate. Allocated once per session.
        history_samples = src_sr * _CONTEXT_MS // 1000
        if self._history_f32 is None or self._history_f32.shape[0] != history_samples:
            self._history_f32 = np.zeros(history_samples, dtype=np.float32)

        # Slide the window: drop the oldest `new_n` samples, append the new frame.
        # Result is always exactly `history_samples` long.
        self._history_f32 = np.concatenate(
            [self._history_f32[new_n:], pcm_f32]
        )

        tensor = self._torch.from_numpy(self._history_f32).unsqueeze(0)  # (1, samples)
        if src_sr != _TARGET_SR:
            tensor = self._resample(tensor, src_sr, _TARGET_SR)

        enhanced = self._enhance(self._model, self._df_state, tensor)  # (1, samples)

        if src_sr != _TARGET_SR:
            enhanced = self._resample(enhanced, _TARGET_SR, src_sr)

        out_mono = enhanced.squeeze(0).detach().cpu().numpy()
        # Take only the last `new_n` samples — the slot corresponding to
        # `pcm_f32` we just appended. The earlier samples were context.
        target_samples = len(pcm_i16) // n_ch
        if out_mono.shape[0] >= target_samples:
            out_mono = out_mono[-target_samples:]
        else:
            # Should never happen, but be safe — pad with the original signal.
            pad = target_samples - out_mono.shape[0]
            out_mono = np.concatenate([pcm_f32[-pad:], out_mono])

        if n_ch > 1:
            out = np.repeat(out_mono[:, None], n_ch, axis=1).reshape(-1)
        else:
            out = out_mono

        out_i16 = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)

        return rtc.AudioFrame(
            data=out_i16.tobytes(),
            sample_rate=src_sr,
            num_channels=n_ch,
            samples_per_channel=target_samples,
        )

    def _close(self) -> None:
        logger.info("denoiser_closed")
        self._model = None
        self._df_state = None
        self._history_f32 = None
