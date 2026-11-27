import math

def render_subtitles(segments, format):
    if format not in ('srt','vtt') or not segments: raise ValueError('No timestamped transcript')
    def stamp(seconds):
        ms=round(seconds*1000)
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}{"," if format=="srt" else "."}{ms%1000:03}'
    lines=['WEBVTT\n'] if format=='vtt' else []
    for index,s in enumerate(segments,1):
        if not math.isfinite(s.start) or not math.isfinite(s.end) or not 0<=s.start<s.end: raise ValueError('Invalid timestamps')
        lines.append(f'{index}\n{stamp(s.start)} --> {stamp(s.end)}\n{s.text.strip()}\n')
    return '\n'.join(lines)
