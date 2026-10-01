"""Tests for the K-pop comeback trail original tool.

YouTube is not called here. Synthetic videos exercise the tool's own
classification, filtering and lifecycle reconstruction logic.
"""

import tools.originals.comeback_trail.tool as tool_module
from tools import TOOL_MAP, TOOLS


def video(
    video_id,
    title,
    channel_title,
    published_at="2024-01-01T00:00:00Z",
):
    return {
        "video_id": video_id,
        "title": title,
        "channel_title": channel_title,
        "published_at": published_at,
        "url": f"https://youtube.com/watch?v={video_id}",
    }


def test_tool_is_registered_as_an_original_tool():
    assert "trace_kpop_comeback_era" in TOOL_MAP
    assert "trace_kpop_comeback_era" in [
        tool["function"]["name"]
        for tool in TOOLS
    ]


def test_build_trail_reconstructs_kpop_lifecycle():
    videos = [
        video(
            "1",
            "aespa 'Drama' MV Teaser",
            "SMTOWN",
            "2023-11-08T00:00:00Z",
        ),
        video(
            "2",
            "aespa 'Drama' MV",
            "SMTOWN",
            "2023-11-10T00:00:00Z",
        ),
        video(
            "3",
            "aespa - Drama | Show! MusicCore",
            "MBCkpop",
            "2023-11-11T00:00:00Z",
        ),
        video(
            "4",
            "aespa 'Drama' Dance Practice",
            "aespa",
            "2023-11-16T00:00:00Z",
        ),
        video(
            "5",
            "aespa 'Drama' MV Behind The Scenes",
            "aespa",
            "2023-11-20T00:00:00Z",
        ),
    ]

    result = tool_module.build_trail(
        videos,
        artist="aespa",
        release_title="Drama",
    )

    assert result["missing_phases"] == []
    assert len(result["phases"]["pre_release"]) == 1
    assert len(result["phases"]["release"]) == 1
    assert len(result["phases"]["promotion"]) == 1
    assert len(result["phases"]["choreography"]) == 1
    assert len(result["phases"]["era_behind"]) == 1


def test_anchor_song_can_join_parent_era():
    videos = [
        video(
            "1",
            "aespa 'Armageddon' MV",
            "SMTOWN",
        ),
        video(
            "2",
            "aespa 'Licorice' Universe",
            "aespa",
        ),
    ]

    result = tool_module.build_trail(
        videos,
        artist="aespa",
        release_title="Armageddon",
        anchor_song="Licorice",
    )

    anchor_items = [
        item
        for item in result["videos"]
        if item["matches_anchor_song"]
    ]

    assert len(anchor_items) == 1
    assert anchor_items[0]["title"] == "aespa 'Licorice' Universe"
    assert anchor_items[0]["phase"] == "release"


def test_lyric_and_fancam_noise_is_filtered():
    videos = [
        video(
            "1",
            "aespa Drama Lyrics Color Coded",
            "Random Channel",
        ),
        video(
            "2",
            "aespa Drama fancam",
            "SBSKPOP X INKIGAYO",
        ),
    ]

    result = tool_module.build_trail(
        videos,
        artist="aespa",
        release_title="Drama",
    )

    assert result["matched_count"] == 0


def test_unrecognized_channel_is_filtered():
    videos = [
        video(
            "1",
            "aespa 'Drama' MV",
            "Random Fan Uploads",
        )
    ]

    result = tool_module.build_trail(
        videos,
        artist="aespa",
        release_title="Drama",
    )

    assert result["matched_count"] == 0


def test_same_logic_generalizes_beyond_aespa():
    videos = [
        video(
            "1",
            "LE SSERAFIM 'EASY' MV Teaser",
            "HYBE LABELS",
        ),
        video(
            "2",
            "LE SSERAFIM 'Smart' Special Performance Video",
            "LE SSERAFIM",
        ),
        video(
            "3",
            "LE SSERAFIM - EASY | Show! MusicCore",
            "MBCkpop",
        ),
    ]

    result = tool_module.build_trail(
        videos,
        artist="LE SSERAFIM",
        release_title="EASY",
        anchor_song="Smart",
    )

    assert result["matched_count"] == 3
    assert result["phases"]["pre_release"]
    assert result["phases"]["release"]
    assert result["phases"]["promotion"]