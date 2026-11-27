import pytest
from app.schemas import TranscriptSegment
from app.subtitles import render_subtitles

def test_srt_vtt_preserve_fractional_timing_and_order():
    rows=[TranscriptSegment(start=1.234,end=3.456,text='测试字幕')]
    assert '00:00:01,234 --> 00:00:03,456' in render_subtitles(rows,'srt')
    assert render_subtitles(rows,'vtt').startswith('WEBVTT\n\n')
    assert '00:00:01.234 --> 00:00:03.456' in render_subtitles(rows,'vtt')

def test_empty_or_invalid_segments_are_not_fake_subtitles():
    for rows in [[],[TranscriptSegment(start=3,end=2,text='invalid')]]:
        with pytest.raises(ValueError):render_subtitles(rows,'srt')
