#!/usr/bin/env python3
"""网球练球视频按击球声切回合。只依赖 ffmpeg/ffprobe 和 numpy。

子命令:
  probe   <视频...>                         看分辨率/码率，提示是不是被聊天软件压缩过
  analyze <视频...> --work DIR [参数]        找击球声、串成回合，写 DIR/<名字>.segments.json
  sheets  <视频> --segments J --work DIR     每个回合开头/结尾各拼一张帧图，给人或模型核对
  cut     <视频> --segments J --out DIR      按回合剪成独立 mp4，写片段清单
  align   <旧视频> <新视频>                  用声音算两版视频的时间偏移（换高清原片时用）
"""
import argparse, json, os, subprocess, sys, wave
import numpy as np

SR = 22050
HOP_MS = 10


def run(cmd):
    # -nostdin 必须加：在循环里调用 ffmpeg 时，它会吃掉标准输入，打乱后续读取
    subprocess.run(cmd, check=True)


def ffprobe_json(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", path],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def extract_wav(video, wav):
    if not os.path.exists(wav):
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
             "-ac", "1", "-ar", str(SR), wav])
    w = wave.open(wav)
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return x


def onsets(x, k=2.5, min_gap=0.25):
    """击球声 = 比前后约 2 秒背景高出 k 倍的短促脆响。返回秒数数组和总时长。"""
    x = np.diff(x, prepend=0)                      # 粗高通，突出瞬态
    hop = SR * HOP_MS // 1000
    n = len(x) // hop
    e = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1))
    win = 200                                      # 2 秒背景窗
    pad = np.pad(e, (win, win), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(pad, 2 * win)[:n]
    bg = np.median(view, axis=1)
    r = e / (bg + 1e-6)
    mg = int(min_gap * 1000 / HOP_MS)
    pk, last = [], -10 ** 9
    for i in range(1, n - 1):
        if r[i] > k and r[i] >= r[i - 1] and r[i] >= r[i + 1]:
            if i - last >= mg:
                pk.append(i); last = i
            elif r[i] > r[last]:
                pk[-1] = i; last = i
    return np.array(pk) * hop / SR, n * hop / SR   # 按采样点换算，避免 220 点≠10ms 的累计漂移


def group(t, dur, gap, min_hits, pre, post):
    segs, cur = [], [t[0]] if len(t) else []
    for a, b in zip(t[:-1], t[1:]):
        if b - a <= gap:
            cur.append(b)
        else:
            segs.append(cur); cur = [b]
    if cur:
        segs.append(cur)
    out = []
    for s in segs:
        if len(s) < min_hits:
            continue
        out.append({"start": round(max(0.0, s[0] - pre), 2),
                    "end": round(min(dur, s[-1] + post), 2),
                    "hits": len(s),
                    "first_hit": round(float(s[0]), 2),
                    "last_hit": round(float(s[-1]), 2)})
    return out


def strip(t, dur):
    c = np.bincount(t.astype(int), minlength=int(dur) + 1)
    lines = []
    for s in range(0, int(dur) + 1, 60):
        lines.append(f"{s:4d}s " + "".join(str(min(v, 9)) if v else "." for v in c[s:s + 60]))
    return "\n".join(lines)


def mmss(sec):
    sec = int(sec)
    return f"{sec // 60}m{sec % 60:02d}s"


def cmd_probe(a):
    for v in a.videos:
        j = ffprobe_json(v)
        vs = next(s for s in j["streams"] if s["codec_type"] == "video")
        w, h = int(vs["width"]), int(vs["height"])
        br = int(j["format"].get("bit_rate", 0))
        rot = next((sd.get("rotation") for sd in vs.get("side_data_list", []) if "rotation" in sd), 0)
        warn = ""
        if min(w, h) < 720 or br < 2_000_000:
            warn = "  ⚠ 疑似被聊天软件压缩（短边<720 或码率<2Mbps），先找原片"
        print(f"{os.path.basename(v)}: {w}x{h} 旋转{rot} {vs.get('r_frame_rate')}帧 "
              f"{br / 1e6:.1f}Mbps 时长{float(j['format']['duration']):.1f}s{warn}")


def cmd_analyze(a):
    os.makedirs(a.work, exist_ok=True)
    for v in a.videos:
        stem = os.path.splitext(os.path.basename(v))[0]
        x = extract_wav(v, os.path.join(a.work, stem + ".wav"))
        t, dur = onsets(x, k=a.k)
        segs = group(t, dur, a.gap, a.min_hits, a.pre, a.post)
        path = os.path.join(a.work, stem + ".segments.json")
        json.dump({"video": os.path.abspath(v), "params": {k: v for k, v in vars(a).items() if k in ("k", "gap", "min_hits", "pre", "post")},
                   "duration": dur, "onsets": [round(float(z), 2) for z in t],
                   "segments": segs}, open(path, "w"), ensure_ascii=False, indent=1)
        print(f"== {stem}  时长{dur:.1f}s  击球声{len(t)}次  回合{len(segs)}个  -> {path}")
        print("每秒击球声数量（. 表示没有）:")
        print(strip(t, dur))
        for i, s in enumerate(segs, 1):
            print(f"{i:2d}  {s['start']:7.1f} -> {s['end']:7.1f}  {s['end'] - s['start']:5.1f}s  击球声{s['hits']}")


def cmd_sheets(a):
    j = json.load(open(a.segments))
    os.makedirs(a.work, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.video))[0]
    made = []
    for i, s in enumerate(j["segments"], 1):
        for tag, st in (("head", s["start"] - 3), ("tail", s["end"] - 5)):
            st = max(0.0, st)
            out = os.path.join(a.work, f"{stem}_{i:02d}_{tag}.jpg")
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{st:.2f}", "-t", "10",
                 "-i", a.video, "-vf", "fps=2,scale=136:-2,tile=10x2", "-frames:v", "1", out])
            made.append(out)
    print("\n".join(made))
    print("每张图 = 从该时间点起 10 秒、每 0.5 秒一帧，从左到右、从上到下。")


def cmd_cut(a):
    j = json.load(open(a.segments))
    os.makedirs(a.out, exist_ok=True)
    lst = os.path.join(a.out, "片段清单.txt")
    with open(lst, "a") as f:
        f.write(f"## 视频{a.label} = {os.path.basename(a.video)}\n")
        for i, s in enumerate(j["segments"], 1):
            name = f"视频{a.label}_{i:02d}_{mmss(s['start'])}-{mmss(s['end'])}.mp4"
            dur = round(s["end"] - s["start"], 3)
            # 精确起点要重编码；原片关键帧间隔常有 2~3 秒，直接拷贝会偏
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{s['start']}", "-t", f"{dur}",
                 "-i", a.video, "-c:v", "libx264", "-preset", "medium", "-crf", str(a.crf),
                 "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", os.path.join(a.out, name)])
            f.write(f"{name}  原片 {s['start']}s -> {s['end']}s  时长 {dur}s  击球声 {s['hits']} 次\n")
            print("done", name, flush=True)
    print("清单:", lst)


def env10ms(x):
    x = np.abs(np.diff(x)); h = SR * HOP_MS // 1000; n = len(x) // h
    return x[:n * h].reshape(n, h).mean(1)


def cmd_align(a):
    tmp = a.work or os.path.dirname(os.path.abspath(a.old))
    xa = env10ms(extract_wav(a.old, os.path.join(tmp, "_align_old.wav")))
    xb = env10ms(extract_wav(a.new, os.path.join(tmp, "_align_new.wav")))
    n = min(len(xa), len(xb)); xa = xa[:n] - xa[:n].mean(); xb = xb[:n] - xb[:n].mean()
    lag = max(range(-300, 301), key=lambda L: float(np.dot(xa[max(0, L):n + min(0, L)], xb[max(0, -L):n - max(0, L)])))
    print(f"新视频相对旧视频的偏移: {lag * (SR * HOP_MS // 1000) * 1000 / SR:.0f} ms（0 表示可直接沿用旧切点）")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("probe"); q.add_argument("videos", nargs="+"); q.set_defaults(fn=cmd_probe)
    q = sp.add_parser("analyze"); q.add_argument("videos", nargs="+"); q.add_argument("--work", required=True)
    q.add_argument("--k", type=float, default=2.5, help="击球声阈值，越大越严")
    q.add_argument("--gap", type=float, default=6.0, help="两声间隔超过这么多秒才算回合结束")
    q.add_argument("--min-hits", type=int, default=6, help="少于这么多声的串当噪声丢掉")
    q.add_argument("--pre", type=float, default=1.0, help="回合前多留几秒")
    q.add_argument("--post", type=float, default=1.5, help="回合后多留几秒")
    q.set_defaults(fn=cmd_analyze)
    q = sp.add_parser("sheets"); q.add_argument("video"); q.add_argument("--segments", required=True)
    q.add_argument("--work", required=True); q.set_defaults(fn=cmd_sheets)
    q = sp.add_parser("cut"); q.add_argument("video"); q.add_argument("--segments", required=True)
    q.add_argument("--out", required=True); q.add_argument("--label", default="A")
    q.add_argument("--crf", type=int, default=17); q.set_defaults(fn=cmd_cut)
    q = sp.add_parser("align"); q.add_argument("old"); q.add_argument("new"); q.add_argument("--work")
    q.set_defaults(fn=cmd_align)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
