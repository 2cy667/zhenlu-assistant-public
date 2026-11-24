import { useEffect, useRef, useState } from "react";
import type { SessionDetail, TranscriptSegment } from "../types";
import { sourcePlayable } from "../realtime";
import { assertAuthenticated } from "../auth";

export function RealtimeAudio({ workspace }: { workspace: SessionDetail }) {
  const player = useRef<HTMLAudioElement>(null),
    latest = useRef(workspace);
  latest.current = workspace;
  const [label, setLabel] = useState("选择原文来源后回听"),
    [error, setError] = useState("");
  useEffect(() => {
    const audio = player.current!;
    let queue: Array<{ url: string; start: number; end: number }> = [],
      index = 0,
      token = 0,
      frame = 0,
      loading = false;
    const stop = () => {
      token++;
      queue = [];
      loading = false;
      audio.pause();
      cancelAnimationFrame(frame);
      audio.onloadedmetadata = null;
    };
    function load() {
      const part = queue[index];
      if (!part) return;
      loading = true;
      audio.pause();
      const current = ++token;
      audio.onloadedmetadata = () => {
        if (current !== token) return;
        try {
          assertAuthenticated();
        } catch {
          stop();
          return;
        }
        if (part.end > audio.duration + 0.1) {
          stop();
          setError("来源时间超出片段长度，请核对原文。");
          return;
        }
        loading = false;
        audio.currentTime = part.start;
        void audio.play().catch(() => setError("请点击播放器播放所选来源。"));
      };
      audio.src = part.url;
      audio.load();
    }
    function advance() {
      if (loading || !queue.length) return;
      audio.pause();
      cancelAnimationFrame(frame);
      if (index + 1 < queue.length) {
        index++;
        load();
      } else {
        audio.currentTime = queue[index].end;
        queue = [];
      }
    }
    function tick() {
      if (
        !audio.paused &&
        queue.length &&
        audio.currentTime >= queue[index].end - 0.02
      ) {
        advance();
        return;
      }
      if (!audio.paused) frame = requestAnimationFrame(tick);
    }
    const play = () => {
      try {
        assertAuthenticated();
      } catch {
        stop();
        return;
      }
      cancelAnimationFrame(frame);
      tick();
    };
    const seek = () => {
      if (queue.length && !loading) {
        const p = queue[index];
        if (
          audio.currentTime < p.start - 0.1 ||
          audio.currentTime > p.end + 0.1
        ) {
          queue = [];
          setLabel("当前音频片段");
        }
      }
    };
    const failed = () => {
      stop();
      setError("片段加载失败，请检查连接后重新点击来源。");
    };
    const source = (event: Event) => {
      const d = (
        event as CustomEvent<{ sessionId: string; source: TranscriptSegment }>
      ).detail;
      if (d.sessionId !== latest.current.id) return;
      try {
        assertAuthenticated();
      } catch {
        stop();
        return;
      }
      if (!sourcePlayable(latest.current, d.source)) {
        setError("当前来源暂无可用的可靠音频区间。");
        return;
      }
      stop();
      setError("");
      queue = d.source.source_ranges!.map((r) => ({
        url: latest.current.realtime!.chunks.find((c) => c.id === r.chunk_id)!
          .audio_url!,
        start: r.start_ms / 1000,
        end: r.end_ms / 1000,
      }));
      index = 0;
      // Authenticated media stays on the same origin; never send cookies to a supplied remote host.
      if (
        queue.some(
          (p) => new URL(p.url, location.href).origin !== location.origin,
        )
      ) {
        stop();
        setError("音频地址必须与当前服务同源。");
        return;
      }
      setLabel(`来源回听 · ${queue.length} 个片段 · 到点暂停`);
      load();
    };
    audio.addEventListener("play", play);
    audio.addEventListener("ended", advance);
    audio.addEventListener("seeking", seek);
    audio.addEventListener("error", failed);
    window.addEventListener("zhenlu:play-source", source);
    window.addEventListener("zhenlu:unauthorized", stop);
    return () => {
      stop();
      audio.removeEventListener("play", play);
      audio.removeEventListener("ended", advance);
      audio.removeEventListener("seeking", seek);
      audio.removeEventListener("error", failed);
      audio.removeAttribute("src");
      audio.load();
      window.removeEventListener("zhenlu:play-source", source);
      window.removeEventListener("zhenlu:unauthorized", stop);
    };
  }, [workspace.id]);
  return (
    <section className="card audio-card audio-card-ready">
      <h2>来源回听</h2>
      <p className="small-note">{label}</p>
      <audio ref={player} controls preload="metadata" />
      <label className="small-note">
        播放速度{" "}
        <select
          aria-label="播放速度"
          defaultValue="1"
          onChange={(e) => {
            if (player.current)
              player.current.playbackRate = Number(e.target.value);
          }}
        >
          <option value="0.75">0.75×</option>
          <option value="1">1×</option>
          <option value="1.5">1.5×</option>
          <option value="2">2×</option>
        </select>
      </label>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
