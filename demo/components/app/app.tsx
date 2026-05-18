'use client';

import type { CSSProperties } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { TokenSourceFetchOptions } from 'livekit-client';
import { AudioPresets, Room, TokenSource } from 'livekit-client';
import { useSession, useSessionContext, useSessionMessages } from '@livekit/components-react';
import { WarningIcon } from '@phosphor-icons/react/dist/ssr';
import type { AppConfig } from '@/app-config';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import {
  DebugPanel,
  type RequestTrace,
  createTraceId,
  summarizeText,
} from '@/components/debug/DebugPanel';
import { Toaster } from '@/components/ui/sonner';
import { useDebugMode } from '@/hooks/useDebug';
import { getSandboxTokenSource } from '@/lib/utils';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });

  return null;
}

function extractText(content: unknown): string {
  if (typeof content === 'string') {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .flatMap((item) => {
        if (typeof item === 'string') {
          return item.trim();
        }
        if (item && typeof item === 'object' && 'text' in item && typeof item.text === 'string') {
          return item.text.trim();
        }
        return '';
      })
      .filter(Boolean)
      .join(' ');
  }
  return '';
}

function messageTimestampValue(value: unknown): number {
  if (typeof value === 'number') {
    return value;
  }
  if (value instanceof Date) {
    return value.getTime();
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return Date.now();
}

function buildTokenRequestBody(options: TokenSourceFetchOptions): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (options.agentName || options.agentMetadata) {
    body.room_config = {
      agents: [
        {
          ...(options.agentName ? { agent_name: options.agentName } : {}),
          ...(options.agentMetadata ? { metadata: options.agentMetadata } : {}),
        },
      ],
    };
  }
  return body;
}

function TranscriptPanel() {
  const session = useSessionContext();
  const { messages } = useSessionMessages(session);

  if (messages.length === 0) {
    return (
      <div className="rounded-3xl border border-white/10 bg-white/5 p-6 text-sm text-white/70">
        بانتظار أول رسالة من الجلسة.
      </div>
    );
  }

  return (
    <div className="max-h-[420px] space-y-3 overflow-y-auto rounded-3xl border border-white/10 bg-white/5 p-4">
      {messages.map((message, index) => {
        const text = extractText(message.message);
        const isUser = message.from?.isLocal;
        return (
          <div
            key={`${message.timestamp}-${index}`}
            className={`rounded-2xl px-4 py-3 text-sm leading-6 ${isUser ? 'ml-8 bg-cyan-500/15 text-cyan-50' : 'mr-8 bg-white/10 text-white'}`}
          >
            <div className="mb-1 text-[11px] font-semibold tracking-[0.18em] text-white/50 uppercase">
              {isUser ? 'User' : 'Assistant'}
            </div>
            <div>{text || '...'}</div>
          </div>
        );
      })}
    </div>
  );
}

function SimpleDemoScreen({
  appConfig,
  debugMode,
  traces,
  onToggleDebug,
  onClearDebug,
  onAppendTrace,
  onUpdateTrace,
}: {
  appConfig: AppConfig;
  debugMode: boolean;
  traces: RequestTrace[];
  onToggleDebug: () => void;
  onClearDebug: () => void;
  onAppendTrace: (trace: RequestTrace) => void;
  onUpdateTrace: (traceId: string, patch: Partial<RequestTrace>) => void;
}) {
  const { isConnected, start, end, connectionState } = useSessionContext();
  const { messages } = useSessionMessages();
  const processedMessageIdsRef = useRef<Set<string>>(new Set());
  const pendingTranscriptTimesRef = useRef<number[]>([]);

  useEffect(() => {
    for (const message of messages) {
      if (processedMessageIdsRef.current.has(message.id)) {
        continue;
      }
      processedMessageIdsRef.current.add(message.id);

      const text = extractText(message.message);
      const timestamp = messageTimestampValue(message.timestamp);
      if (message.from?.isLocal) {
        pendingTranscriptTimesRef.current.push(timestamp);
        onAppendTrace({
          id: createTraceId(),
          label: 'User Transcript',
          startedAt: timestamp,
          endedAt: timestamp,
          durationMs: 0,
          payloadSummary: 'LiveKit transcription event',
          resultSummary: text ? summarizeText(text) : 'empty transcript',
        });
        continue;
      }

      const transcriptStartedAt = pendingTranscriptTimesRef.current.shift();
      onAppendTrace({
        id: createTraceId(),
        label: 'Assistant Reply',
        startedAt: timestamp,
        endedAt: timestamp,
        durationMs: 0,
        payloadSummary: 'LiveKit assistant message',
        resultSummary: text ? summarizeText(text) : 'empty reply',
        stepTimings:
          transcriptStartedAt != null
            ? [
                {
                  label: 'transcript->reply',
                  durationMs: Math.max(timestamp - transcriptStartedAt, 0),
                },
              ]
            : undefined,
      });
    }
  }, [messages, onAppendTrace]);

  const handleStart = useCallback(async () => {
    const traceId = createTraceId();
    const startedAt = Date.now();
    onAppendTrace({
      id: traceId,
      label: 'Connect Session',
      startedAt,
      payloadSummary: 'LiveKit session start',
      resultSummary: `state: ${connectionState}`,
    });

    try {
      await start();
      const endedAt = Date.now();
      onUpdateTrace(traceId, {
        endedAt,
        durationMs: endedAt - startedAt,
        resultSummary: 'state: connected',
      });
    } catch (error) {
      const endedAt = Date.now();
      onUpdateTrace(traceId, {
        endedAt,
        durationMs: endedAt - startedAt,
        error: error instanceof Error ? error.message : 'Session start failed',
      });
    }
  }, [connectionState, onAppendTrace, onUpdateTrace, start]);

  return (
    <main className="min-h-screen bg-neutral-950 px-6 py-20 text-white">
      <div className="mx-auto flex max-w-6xl flex-col gap-8">
        <DebugPanel
          debugMode={debugMode}
          traces={traces}
          onToggle={onToggleDebug}
          onClear={onClearDebug}
        />

        <div className="flex flex-col gap-8 lg:flex-row">
          <section className="flex-1 rounded-[32px] border border-white/10 bg-white/5 p-8 shadow-2xl shadow-black/30 backdrop-blur">
            <div className="mb-8">
              <div className="mb-3 inline-flex items-center rounded-full border border-cyan-400/20 bg-cyan-400/10 px-3 py-1 text-[11px] font-semibold tracking-[0.2em] text-cyan-200 uppercase">
                Realtime Speech Demo
              </div>
              <h1 className="text-3xl font-semibold tracking-tight">{appConfig.pageTitle}</h1>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div className="text-[11px] tracking-[0.18em] text-white/50 uppercase">STT</div>
                <div className="mt-2 text-lg font-semibold">Local ASR</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div className="text-[11px] tracking-[0.18em] text-white/50 uppercase">LLM</div>
                <div className="mt-2 text-lg font-semibold">Nusuk</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div className="text-[11px] tracking-[0.18em] text-white/50 uppercase">TTS</div>
                <div className="mt-2 text-lg font-semibold">Local TTS</div>
              </div>
            </div>

            <div className="mt-10 rounded-[28px] border border-white/10 bg-black/30 p-8 text-center">
              <div className="mx-auto mb-5 flex h-20 w-20 items-center justify-center rounded-full border border-cyan-400/20 bg-cyan-400/10">
                <div className="flex items-end gap-1">
                  <span className="h-7 w-1.5 rounded-full bg-cyan-300" />
                  <span className="h-12 w-1.5 rounded-full bg-white" />
                  <span className="h-9 w-1.5 rounded-full bg-cyan-300" />
                  <span className="h-6 w-1.5 rounded-full bg-white" />
                </div>
              </div>

              <div className="mb-2 text-sm font-medium text-white/70">
                {isConnected
                  ? 'الجلسة متصلة. يمكنك التحدث الآن.'
                  : 'اضغط الزر لبدء المحادثة الصوتية.'}
              </div>
              <div className="mb-8 text-xs tracking-[0.18em] text-white/40 uppercase">
                Status: {isConnected ? 'Connected' : connectionState}
              </div>

              {!isConnected ? (
                <button
                  type="button"
                  onClick={() => void handleStart()}
                  className="cursor-pointer rounded-full bg-cyan-400 px-8 py-4 font-mono text-sm font-bold tracking-[0.2em] text-black uppercase transition hover:bg-cyan-300"
                >
                  Start Conversation
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void end()}
                  className="cursor-pointer rounded-full border border-white/15 bg-white/10 px-8 py-4 font-mono text-sm font-bold tracking-[0.2em] text-white uppercase transition hover:bg-white/15"
                >
                  End Session
                </button>
              )}
            </div>
          </section>

          <section className="w-full rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-2xl shadow-black/30 backdrop-blur lg:max-w-xl">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <div className="text-[11px] tracking-[0.18em] text-white/50 uppercase">
                  Live Transcript
                </div>
                <h2 className="mt-2 text-xl font-semibold">Conversation Feed</h2>
              </div>
              <div
                className={`rounded-full px-3 py-1 text-[11px] font-semibold tracking-[0.18em] uppercase ${isConnected ? 'bg-emerald-400/15 text-emerald-200' : 'bg-white/10 text-white/60'}`}
              >
                {isConnected ? 'Live' : 'Idle'}
              </div>
            </div>

            <TranscriptPanel />
          </section>
        </div>
      </div>
    </main>
  );
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  const [debugMode, setDebugMode] = useState(false);
  const [traces, setTraces] = useState<RequestTrace[]>([]);

  const appendTrace = useCallback((trace: RequestTrace) => {
    setTraces((prev) => [trace, ...prev]);
  }, []);

  const updateTrace = useCallback((traceId: string, patch: Partial<RequestTrace>) => {
    setTraces((prev) =>
      prev.map((trace) => (trace.id === traceId ? { ...trace, ...patch } : trace))
    );
  }, []);

  const clearTraces = useCallback(() => {
    setTraces([]);
  }, []);

  const tokenSource = useMemo(() => {
    if (typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string') {
      return getSandboxTokenSource(appConfig);
    }

    return TokenSource.custom(async (options) => {
      const traceId = createTraceId();
      const startedAt = Date.now();
      appendTrace({
        id: traceId,
        label: 'Token Request',
        startedAt,
        payloadSummary: 'POST /api/token',
      });

      try {
        const response = await fetch('/api/token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildTokenRequestBody(options)),
        });
        if (!response.ok) {
          throw new Error(`Token request failed (${response.status}): ${await response.text()}`);
        }

        const payload = (await response.json()) as {
          serverUrl: string;
          roomName: string;
          participantName: string;
          participantToken: string;
        };
        const endedAt = Date.now();
        setTraces((prev) =>
          prev.map((trace) =>
            trace.id === traceId
              ? {
                  ...trace,
                  endedAt,
                  durationMs: endedAt - startedAt,
                  resultSummary: `room: ${payload.roomName}`,
                }
              : trace
          )
        );
        return payload;
      } catch (error) {
        const endedAt = Date.now();
        setTraces((prev) =>
          prev.map((trace) =>
            trace.id === traceId
              ? {
                  ...trace,
                  endedAt,
                  durationMs: endedAt - startedAt,
                  error: error instanceof Error ? error.message : 'Token request failed',
                }
              : trace
          )
        );
        throw error;
      }
    });
  }, [appConfig, appendTrace]);

  // Construct our own Room so the audio chain matches what the agent expects.
  // Without this, the browser publishes 48 kHz audio with WebRTC NS + AGC on,
  // so DeepFilterNet3 in the agent receives already-processed audio and the
  // manager hears stacked NS rather than what our agent actually does.
  //
  // - echoCancellation stays ON: prevents agent TTS from bleeding back through
  //   the user's mic into ASR.
  // - noiseSuppression / autoGainControl / voiceIsolation OFF: DeepFilterNet3
  //   in the agent is the only NS in the path.
  // - 16 kHz mono matches Silero VAD + Nusuk ASR native rate; avoids resamples.
  // - AudioPresets.speech = 24 kbps Opus narrowband; matches the 16 kHz frame
  //   and is the lowest-latency preset.
  const room = useMemo(
    () =>
      new Room({
        audioCaptureDefaults: {
          echoCancellation: true,
          noiseSuppression: false,
          autoGainControl: false,
          voiceIsolation: false,
          channelCount: 1,
          sampleRate: 16_000,
        },
        publishDefaults: {
          audioPreset: AudioPresets.speech,
          dtx: true,
          red: true,
        },
      }),
    []
  );

  const session = useSession(tokenSource, {
    room,
    ...(appConfig.agentName ? { agentName: appConfig.agentName } : {}),
  });

  return (
    <AgentSessionProvider session={session}>
      <AppSetup />
      <SimpleDemoScreen
        appConfig={appConfig}
        debugMode={debugMode}
        traces={traces}
        onToggleDebug={() => setDebugMode((prev) => !prev)}
        onClearDebug={clearTraces}
        onAppendTrace={appendTrace}
        onUpdateTrace={updateTrace}
      />
      <Toaster
        icons={{
          warning: <WarningIcon weight="bold" />,
        }}
        position="top-center"
        className="toaster group"
        style={
          {
            '--normal-bg': 'var(--popover)',
            '--normal-text': 'var(--popover-foreground)',
            '--normal-border': 'var(--border)',
          } as CSSProperties
        }
      />
    </AgentSessionProvider>
  );
}
